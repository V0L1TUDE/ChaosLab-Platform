"""
Kubernetes manifest validation for uploaded applications (strict sandbox)
=========================================================================
Uploaded .yaml / .zip bundles are parsed IN MEMORY (never extracted to disk) and validated against an
explicit allow-list: only Deployment, Service (ClusterIP) and ConfigMap are accepted, and only a whitelisted
subset of their fields. Anything else (privileged containers, host access, RBAC, Secrets, PVCs, NodePort/
LoadBalancer, init containers, unknown fields ...) is rejected with a precise message. The result is a
sanitized document list that is later applied into an isolated per-application namespace, so what runs is
exactly what was validated. Nothing here talks to the cluster.
"""

import copy
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import archive_guard, config

MAX_FILES = 50
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_DOCS = 100
MAX_RESOURCES = 20
MAX_CONTAINERS = 5
MAX_CONFIGMAP_BYTES = 256 * 1024
MAX_CPU_MILLIS = 1000                  # per container limit
MAX_MEMORY_BYTES = 1024 ** 3           # per container limit (1Gi)
RESERVED_PREFIXES = ("chaoslab.io/", "pod-security.kubernetes.io/", "kubernetes.io/", "k8s.io/")
RESERVED_DOMAINS = ("chaoslab.io", "kubernetes.io", "k8s.io")       # and their subdomains...
ALLOWED_DOMAIN_LABELS = ("app.kubernetes.io/",)                      # ...except the recommended app labels
RESERVED_NAME_PREFIXES = ("chaoslab-", "kube-")                  # platform guard rails, kube-root-ca.crt, ...
RESERVED_NAMES = {"default"}
MAX_YAML_DEPTH = 32
PARSE_BUDGET_SECONDS = 5.0
MAX_ENV = 100
MAX_ARGS = 100
MAX_ARG_CHARS = 4096
MAX_EMPTYDIR_BYTES = 256 * 1024 ** 2
MAX_GRACE_SECONDS = 60
MAX_SERVICE_PORTS = 10
MAX_UID = 2 ** 31 - 1
ENV_NAME = re.compile(r"^[-._a-zA-Z][-._a-zA-Z0-9]{0,127}$")

DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
LABEL_KEY = re.compile(r"^([a-z0-9.-]{1,253}/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
LABEL_VAL = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")

ANY = object()   # "any value" leaf in a schema; leaf semantics are checked separately

_PROBE = {"httpGet": {"path": ANY, "port": ANY, "scheme": ANY}, "tcpSocket": {"port": ANY},
          "initialDelaySeconds": ANY, "periodSeconds": ANY, "timeoutSeconds": ANY,
          "failureThreshold": ANY, "successThreshold": ANY}
_CONTAINER = {
    "name": ANY, "image": ANY, "imagePullPolicy": ANY, "command": ANY, "args": ANY,
    "env": [{"name": ANY, "value": ANY, "valueFrom": {"configMapKeyRef": {"name": ANY, "key": ANY, "optional": ANY}}}],
    "ports": [{"containerPort": ANY, "name": ANY, "protocol": ANY}],
    "resources": {"requests": ANY, "limits": ANY},
    "readinessProbe": _PROBE, "livenessProbe": _PROBE, "startupProbe": _PROBE,
    "volumeMounts": [{"name": ANY, "mountPath": ANY, "readOnly": ANY, "subPath": ANY}],
    "securityContext": {"runAsUser": ANY, "runAsGroup": ANY, "runAsNonRoot": ANY,
                        "readOnlyRootFilesystem": ANY, "allowPrivilegeEscalation": ANY},
}
SCHEMAS: Dict[str, Dict[str, Any]] = {
    "Deployment": {
        "apiVersion": ANY, "kind": ANY,
        "metadata": {"name": ANY, "namespace": ANY, "labels": ANY, "annotations": ANY},
        "spec": {
            "replicas": ANY, "selector": {"matchLabels": ANY},
            "template": {"metadata": {"labels": ANY},
                         "spec": {"containers": [_CONTAINER], "terminationGracePeriodSeconds": ANY,
                                  "volumes": [{"name": ANY, "emptyDir": {"sizeLimit": ANY},
                                               "configMap": {"name": ANY, "items": [{"key": ANY, "path": ANY}],
                                                             "defaultMode": ANY}}]}},
        },
    },
    "Service": {
        "apiVersion": ANY, "kind": ANY,
        "metadata": {"name": ANY, "namespace": ANY, "labels": ANY, "annotations": ANY},
        "spec": {"type": ANY, "selector": ANY,
                 "ports": [{"name": ANY, "port": ANY, "targetPort": ANY, "protocol": ANY}]},
    },
    "ConfigMap": {
        "apiVersion": ANY, "kind": ANY,
        "metadata": {"name": ANY, "namespace": ANY, "labels": ANY, "annotations": ANY},
        "data": ANY,
    },
}
API_VERSIONS = {"Deployment": "apps/v1", "Service": "v1", "ConfigMap": "v1"}


# ----------------------------------------------------------------------------- parsing
def _decode(data: bytes, name: str, errors: List[str]) -> Optional[str]:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{name}: not valid UTF-8 text.")
        return None


def extract_texts(ext: str, data: bytes, errors: List[str]) -> List[Tuple[str, str]]:
    """(source name, yaml text) pairs from a .yaml/.yml file or the .yaml/.yml members of a .zip."""
    if ext in ("yaml", "yml"):
        if len(data) > MAX_FILE_BYTES:
            errors.append(f"File exceeds {MAX_FILE_BYTES // 1024} KB for a manifest.")
            return []
        text = _decode(data, "manifest", errors)
        return [("manifest", text)] if text is not None else []
    out: List[Tuple[str, str]] = []
    total = 0
    budget = archive_guard.Budget(max_bytes=MAX_TOTAL_BYTES + MAX_FILE_BYTES)
    try:
        zf, infos = archive_guard.zip_entries(data)
        with zf:
            members = [i for i in infos if not i.is_dir() and i.filename.lower().endswith((".yaml", ".yml"))]
            if len(members) > MAX_FILES:
                errors.append(f"Too many manifest files in the archive (max {MAX_FILES}).")
                return []
            for info in members:
                if info.file_size > MAX_FILE_BYTES:
                    errors.append(f"{info.filename}: larger than {MAX_FILE_BYTES // 1024} KB.")
                    continue
                if total + info.file_size > MAX_TOTAL_BYTES:
                    errors.append("Archive manifests exceed the total size limit.")
                    return []
                # bounded read: header sizes can lie, so stop at the per-file limit whatever the header says
                raw = archive_guard.read_zip_member(zf, info, budget, MAX_FILE_BYTES)
                total += len(raw)
                if total > MAX_TOTAL_BYTES:
                    errors.append("Archive manifests exceed the total size limit.")
                    return []
                text = _decode(raw, info.filename, errors)
                if text is not None:
                    out.append((info.filename, text))
    except archive_guard.ArchiveRejected as e:
        errors.append(str(e))
        return []
    return out


def has_manifest_candidates(ext: str, data: bytes) -> bool:
    """True for .yaml/.yml uploads and zips that contain at least one yaml file."""
    if ext in ("yaml", "yml"):
        return True
    if ext != "zip":
        return False
    try:
        zf, infos = archive_guard.zip_entries(data)
    except archive_guard.ArchiveRejected:
        return False
    with zf:
        return any(not i.is_dir() and i.filename.lower().endswith((".yaml", ".yml")) for i in infos)


def _yaml_precheck(text: str, deadline: float) -> Optional[str]:
    """Stream the YAML events once (iterative, no object construction): reject aliases/anchors ("billion laughs"),
    nesting deeper than MAX_YAML_DEPTH (the composer is recursive) and slow inputs. -> reason or None."""
    depth = 0
    for i, ev in enumerate(yaml.parse(text, Loader=yaml.SafeLoader)):
        if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None):
            return "YAML aliases/anchors are not allowed."
        if isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if depth > MAX_YAML_DEPTH:
                return f"YAML is nested deeper than {MAX_YAML_DEPTH} levels."
        elif isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1
        if i % 512 == 0 and time.monotonic() > deadline:
            return "manifest took too long to parse."
    return None


def parse_documents(texts: List[Tuple[str, str]], errors: List[str]) -> List[Tuple[str, Dict[str, Any]]]:
    docs: List[Tuple[str, Dict[str, Any]]] = []
    deadline = time.monotonic() + PARSE_BUDGET_SECONDS
    for name, text in texts:
        try:
            reason = _yaml_precheck(text, deadline)
            if reason:
                errors.append(f"{name}: {reason}")
                if "too long" in reason:
                    break
                continue
            for doc in yaml.safe_load_all(text):
                if doc is None:
                    continue
                if not isinstance(doc, dict):
                    errors.append(f"{name}: each YAML document must be a mapping.")
                    continue
                if doc.get("kind") == "List" and isinstance(doc.get("items"), list):
                    docs.extend((name, i) for i in doc["items"] if isinstance(i, dict))
                else:
                    docs.append((name, doc))
        except yaml.YAMLError as e:
            errors.append(f"{name}: invalid YAML ({(str(e).splitlines() or ['parse error'])[0][:120]}).")
        except (RecursionError, MemoryError, ValueError, TypeError, OverflowError):
            errors.append(f"{name}: the YAML could not be parsed safely.")
        if len(docs) > MAX_DOCS:
            errors.append(f"Too many YAML documents (max {MAX_DOCS}).")
            break
    return docs


# ----------------------------------------------------------------------------- schema + semantics
def _check_schema(obj: Any, schema: Any, path: str, errors: List[str]) -> None:
    if schema is ANY:
        return
    if isinstance(schema, list):
        if not isinstance(obj, list):
            errors.append(f"{path}: must be a list.")
            return
        for i, item in enumerate(obj):
            _check_schema(item, schema[0], f"{path}[{i}]", errors)
        return
    if not isinstance(obj, dict):
        errors.append(f"{path}: must be an object.")
        return
    for key, value in obj.items():
        if key not in schema:
            errors.append(f"{path}.{key}: field is not allowed in the ChaosLab sandbox.")
        else:
            _check_schema(value, schema[key], f"{path}.{key}", errors)


def _cpu_millis(v: Any) -> int:
    s = str(v)
    return int(float(s[:-1])) if s.endswith("m") else int(float(s) * 1000)


def _mem_bytes(v: Any) -> int:
    s = str(v)
    for unit, mult in (("Ki", 1024), ("Mi", 1024 ** 2), ("Gi", 1024 ** 3), ("K", 1000), ("M", 1000 ** 2), ("G", 1000 ** 3)):
        if s.endswith(unit):
            return int(float(s[:-len(unit)]) * mult)
    return int(float(s))


def parse_image(image: str) -> Tuple[str, str, Optional[str], Optional[str]]:
    """-> (registry, repository, tag, digest)"""
    digest = None
    if "@" in image:
        image, digest = image.split("@", 1)
    tag = None
    if ":" in image.rsplit("/", 1)[-1]:
        image, tag = image.rsplit(":", 1)
    parts = image.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repo = parts[0], "/".join(parts[1:])
    else:
        registry, repo = "docker.io", image
    if registry == "index.docker.io":
        registry = "docker.io"
    return registry, repo, tag, digest


def _check_image(image: Any, where: str, errors: List[str]) -> None:
    if not isinstance(image, str) or not image or len(image) > 300 or re.search(r"\s", image):
        errors.append(f"{where}: image must be a non-empty string.")
        return
    registry, repo, tag, digest = parse_image(image)
    if registry not in config.ALLOWED_REGISTRIES:
        errors.append(f"{where}: registry '{registry}' is not allowed. Allowed: {', '.join(config.ALLOWED_REGISTRIES)}.")
    if not digest and (not tag or tag == "latest"):
        errors.append(f"{where}: image '{image}' must pin an explicit tag (not 'latest') or a digest.")


def _reserved_key(key: str) -> bool:
    """Platform-owned label domains (chaoslab.io, kubernetes.io, k8s.io and subdomains such as
    pod-security.kubernetes.io or node-role.kubernetes.io), except app.kubernetes.io/*."""
    if key.startswith(ALLOWED_DOMAIN_LABELS) or "/" not in key:
        return key.startswith(RESERVED_PREFIXES)
    domain = key.split("/", 1)[0]
    return any(domain == d or domain.endswith("." + d) for d in RESERVED_DOMAINS)


def _labels_ok(labels: Any, where: str, errors: List[str]) -> Dict[str, str]:
    if labels is None:
        return {}
    if not isinstance(labels, dict):
        errors.append(f"{where}: labels must be a mapping.")
        return {}
    clean: Dict[str, str] = {}
    for k, v in labels.items():
        k, v = str(k), "" if v is None else str(v)
        if not LABEL_KEY.match(k) or not LABEL_VAL.match(v):
            errors.append(f"{where}: invalid label '{k}: {v}'.")
        elif _reserved_key(k):
            errors.append(f"{where}: label key '{k}' uses a reserved prefix.")
        else:
            clean[k] = v
    return clean


def _int_in(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _validate_security_context(sc: Dict[str, Any], where: str, errors: List[str]) -> None:
    """Only hardening is accepted from the user; the platform forces the rest (non-root, no escalation,
    drop ALL capabilities, seccomp RuntimeDefault) at deploy time."""
    if "runAsUser" in sc and not _int_in(sc["runAsUser"], 1, MAX_UID):
        errors.append(f"{where}.runAsUser: must be a non-root user id (1-{MAX_UID}); containers never run as root.")
    if "runAsGroup" in sc and not _int_in(sc["runAsGroup"], 0, MAX_UID):
        errors.append(f"{where}.runAsGroup: must be 0-{MAX_UID}.")
    if "runAsNonRoot" in sc and sc["runAsNonRoot"] is not True:
        errors.append(f"{where}.runAsNonRoot: must be true (containers never run as root).")
    if "allowPrivilegeEscalation" in sc and sc["allowPrivilegeEscalation"] is not False:
        errors.append(f"{where}.allowPrivilegeEscalation: must be false.")
    if "readOnlyRootFilesystem" in sc and not isinstance(sc["readOnlyRootFilesystem"], bool):
        errors.append(f"{where}.readOnlyRootFilesystem: must be true or false.")


def _validate_probe(p: Any, where: str, errors: List[str]) -> None:
    if p is None:
        return
    for k in ("initialDelaySeconds", "periodSeconds", "timeoutSeconds", "failureThreshold", "successThreshold"):
        if k in p and not _int_in(p[k], 0 if k == "initialDelaySeconds" else 1, 600):
            errors.append(f"{where}.{k}: must be an integer up to 600.")
    kinds = [k for k in ("httpGet", "tcpSocket") if k in p]
    if len(kinds) != 1:
        errors.append(f"{where}: needs exactly one of httpGet or tcpSocket (exec probes are not allowed).")
        return
    h = p[kinds[0]] or {}
    port = h.get("port")
    if not (_int_in(port, 1, 65535) or (isinstance(port, str) and DNS_LABEL.match(port))):
        errors.append(f"{where}.{kinds[0]}.port: must be a port number or name.")
    if kinds[0] == "httpGet":
        if h.get("scheme", "HTTP") not in ("HTTP", "HTTPS"):
            errors.append(f"{where}.httpGet.scheme: must be HTTP or HTTPS.")
        if "path" in h and not (isinstance(h["path"], str) and h["path"].startswith("/") and len(h["path"]) <= 200):
            errors.append(f"{where}.httpGet.path: must be an absolute path.")


def _validate_container(c: Dict[str, Any], where: str, volumes: Dict[str, str], configmaps: set,
                        errors: List[str]) -> None:
    name = c.get("name")
    if not isinstance(name, str) or not DNS_LABEL.match(name):
        errors.append(f"{where}.name: must be a DNS label (lowercase letters, digits, '-').")
    _check_image(c.get("image"), f"{where}.image", errors)
    if c.get("imagePullPolicy") not in (None, "IfNotPresent", "Always"):
        # "Never" would run whatever image is cached on the node; tenant images are always pulled at deploy time.
        errors.append(f"{where}.imagePullPolicy: must be IfNotPresent or Always (images are always pulled from "
                      "the registry in the sandbox).")
    for key in ("command", "args"):
        if key in c and not (isinstance(c[key], list) and len(c[key]) <= MAX_ARGS
                             and all(isinstance(x, str) and len(x) <= MAX_ARG_CHARS for x in c[key])):
            errors.append(f"{where}.{key}: must be a list of at most {MAX_ARGS} strings.")
    if len(c.get("env") or []) > MAX_ENV:
        errors.append(f"{where}.env: at most {MAX_ENV} variables.")
    for i, e in enumerate((c.get("env") or [])[:MAX_ENV]):
        w = f"{where}.env[{i}]"
        if not isinstance(e.get("name"), str) or not ENV_NAME.match(e["name"]):
            errors.append(f"{w}.name: required, letters/digits/'_'/'-'/'.' (max 128).")
        if "value" in e and not isinstance(e["value"], str):
            errors.append(f"{w}.value: must be a string (quote numbers and booleans).")
        ref = ((e.get("valueFrom") or {}).get("configMapKeyRef") or {})
        if "valueFrom" in e and ref.get("name") not in configmaps:
            errors.append(f"{w}: ConfigMap '{ref.get('name')}' is not part of this upload.")
    for i, p in enumerate(c.get("ports") or []):
        if not _int_in(p.get("containerPort"), 1, 65535):
            errors.append(f"{where}.ports[{i}].containerPort: must be 1-65535.")
        if p.get("protocol", "TCP") not in ("TCP", "UDP"):
            errors.append(f"{where}.ports[{i}].protocol: must be TCP or UDP.")
    res = c.get("resources") or {}
    for kind, limit_fn, cap, label in (("cpu", _cpu_millis, MAX_CPU_MILLIS, "CPU"), ("memory", _mem_bytes, MAX_MEMORY_BYTES, "memory")):
        vals = {}
        for section in ("requests", "limits"):
            if kind in (res.get(section) or {}):
                try:
                    vals[section] = limit_fn(res[section][kind])
                except (ValueError, TypeError):
                    errors.append(f"{where}.resources.{section}.{kind}: invalid quantity.")
        if vals.get("limits", 0) > cap:
            errors.append(f"{where}.resources.limits.{kind}: {label} limit exceeds the sandbox maximum.")
        if "requests" in vals and "limits" in vals and vals["requests"] > vals["limits"]:
            errors.append(f"{where}.resources: {kind} request is larger than its limit.")
    for section in ("requests", "limits"):
        extra = set((res.get(section) or {}).keys()) - {"cpu", "memory"}
        if extra:
            errors.append(f"{where}.resources.{section}: only cpu and memory are allowed ({', '.join(sorted(extra))}).")
    _validate_security_context(c.get("securityContext") or {}, f"{where}.securityContext", errors)
    for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
        _validate_probe(c.get(probe), f"{where}.{probe}", errors)
    for i, m in enumerate(c.get("volumeMounts") or []):
        if m.get("name") not in volumes:
            errors.append(f"{where}.volumeMounts[{i}]: volume '{m.get('name')}' is not defined.")
        mp = m.get("mountPath")
        if not isinstance(mp, str) or not mp.startswith("/") or ".." in mp.split("/"):
            errors.append(f"{where}.volumeMounts[{i}].mountPath: must be an absolute path.")


def _validate_deployment(doc: Dict[str, Any], configmaps: set, errors: List[str], warnings: List[str]) -> None:
    name = doc["metadata"]["name"]
    where = f"Deployment/{name}"
    spec = doc.get("spec") or {}
    replicas = spec.get("replicas", 1)
    if not _int_in(replicas, 1, config.MAX_REPLICAS):
        errors.append(f"{where}.spec.replicas: must be between 1 and {config.MAX_REPLICAS}.")
    tmpl = spec.get("template") or {}
    pod = tmpl.get("spec") or {}
    if "terminationGracePeriodSeconds" in pod and not _int_in(pod["terminationGracePeriodSeconds"], 0, MAX_GRACE_SECONDS):
        errors.append(f"{where}.terminationGracePeriodSeconds: must be 0-{MAX_GRACE_SECONDS}.")
    containers = pod.get("containers")
    if not isinstance(containers, list) or not 1 <= len(containers) <= MAX_CONTAINERS:
        errors.append(f"{where}: needs between 1 and {MAX_CONTAINERS} containers.")
        containers = []
    volumes: Dict[str, str] = {}
    for i, v in enumerate(pod.get("volumes") or []):
        vname = v.get("name")
        if not isinstance(vname, str) or not DNS_LABEL.match(vname) or vname in volumes:
            errors.append(f"{where}.volumes[{i}]: needs a unique DNS-label name.")
            continue
        kinds = [k for k in ("emptyDir", "configMap") if k in v]
        if len(kinds) != 1:
            errors.append(f"{where}.volumes[{i}]: must be exactly one of emptyDir or configMap (hostPath, secrets, PVCs etc. are not allowed).")
            continue
        volumes[vname] = kinds[0]
        if kinds[0] == "emptyDir":
            size = (v["emptyDir"] or {}).get("sizeLimit")
            try:
                ok_size = size is None or 0 < _mem_bytes(size) <= MAX_EMPTYDIR_BYTES
            except (ValueError, TypeError):
                ok_size = False
            if not ok_size:
                errors.append(f"{where}.volumes[{i}].emptyDir.sizeLimit: up to {MAX_EMPTYDIR_BYTES // 1024 ** 2}Mi "
                              "(medium: Memory is not allowed).")
        if kinds[0] == "configMap":
            cm = v["configMap"] or {}
            if cm.get("name") not in configmaps:
                errors.append(f"{where}.volumes[{i}]: ConfigMap '{cm.get('name')}' is not part of this upload.")
            if "defaultMode" in cm and not _int_in(cm["defaultMode"], 0, 0o755):
                errors.append(f"{where}.volumes[{i}].configMap.defaultMode: must be 0-0755.")
            for j, it in enumerate(cm.get("items") or []):
                pth = it.get("path")
                if not isinstance(it.get("key"), str) or not isinstance(pth, str) or not pth or pth.startswith("/") \
                        or ".." in pth.split("/"):
                    errors.append(f"{where}.volumes[{i}].configMap.items[{j}]: needs a key and a relative path.")
    seen = set()
    for i, c in enumerate(containers):
        if isinstance(c, dict):
            if c.get("name") in seen:
                errors.append(f"{where}.containers[{i}]: duplicate container name.")
            seen.add(c.get("name"))
            _validate_container(c, f"{where}.containers[{i}]", volumes, configmaps, errors)
    # The ChaosLab engine selects target pods by the `app` label.
    match = (spec.get("selector") or {}).get("matchLabels")
    labels = _labels_ok((tmpl.get("metadata") or {}).get("labels"), f"{where}.template.labels", errors)
    if not isinstance(match, dict) or not match:
        errors.append(f"{where}.spec.selector.matchLabels: required.")
        return
    match = _labels_ok(match, f"{where}.selector", errors)
    if "app" not in match and "app" not in labels:
        match["app"], labels["app"] = name, name
        warnings.append(f"{where}: added label app={name} (ChaosLab targets services by the 'app' label).")
    elif "app" not in match:
        match["app"] = labels["app"]
    elif "app" not in labels:
        labels["app"] = match["app"]
    if any(labels.get(k) != v for k, v in match.items()):
        errors.append(f"{where}: spec.selector.matchLabels must match the pod template labels.")
    doc["spec"]["selector"]["matchLabels"] = match
    doc["spec"].setdefault("template", {}).setdefault("metadata", {})["labels"] = labels


def _validate_service(doc: Dict[str, Any], errors: List[str]) -> None:
    where = f"Service/{doc['metadata']['name']}"
    spec = doc.get("spec") or {}
    if spec.get("type", "ClusterIP") != "ClusterIP":
        errors.append(f"{where}.spec.type: only ClusterIP is allowed (no NodePort/LoadBalancer).")
    sel = spec.get("selector")
    if not isinstance(sel, dict) or not sel:
        errors.append(f"{where}.spec.selector: required.")
    else:
        spec["selector"] = _labels_ok(sel, f"{where}.selector", errors)
    ports = spec.get("ports")
    if not isinstance(ports, list) or not 1 <= len(ports) <= MAX_SERVICE_PORTS:
        errors.append(f"{where}.spec.ports: between 1 and {MAX_SERVICE_PORTS} ports are required.")
        return
    for i, p in enumerate(ports):
        if not _int_in(p.get("port"), 1, 65535):
            errors.append(f"{where}.spec.ports[{i}].port: must be 1-65535.")
        tp = p.get("targetPort", p.get("port"))
        if not (_int_in(tp, 1, 65535) or (isinstance(tp, str) and DNS_LABEL.match(tp))):
            errors.append(f"{where}.spec.ports[{i}].targetPort: must be a port number or a port name.")
        if p.get("protocol", "TCP") not in ("TCP", "UDP"):
            errors.append(f"{where}.spec.ports[{i}].protocol: must be TCP or UDP.")


def _validate_configmap(doc: Dict[str, Any], errors: List[str]) -> None:
    where = f"ConfigMap/{doc['metadata']['name']}"
    data = doc.get("data") or {}
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        errors.append(f"{where}.data: must map string keys to string values.")
    elif sum(len(k) + len(v) for k, v in data.items()) > MAX_CONFIGMAP_BYTES:
        errors.append(f"{where}.data: larger than {MAX_CONFIGMAP_BYTES // 1024} KB.")


def _summarize_workload(doc: Dict[str, Any]) -> Dict[str, Any]:
    pod = doc["spec"]["template"]["spec"]
    containers = pod.get("containers", [])
    ports = sorted({p["containerPort"] for c in containers for p in (c.get("ports") or []) if "containerPort" in p})
    return {"name": doc["metadata"]["name"], "app": doc["spec"]["selector"]["matchLabels"].get("app"),
            "replicas": doc["spec"].get("replicas", 1),
            "images": [c.get("image") for c in containers], "ports": ports}


# ----------------------------------------------------------------------------- entry point
def analyze(ext: str, data: bytes) -> Dict[str, Any]:
    """
    Validate an uploaded manifest bundle. Returns
    {"valid": bool, "errors": [...], "warnings": [...], "resources": [sanitized docs], "workloads": [...]}.
    """
    errors: List[str] = []
    warnings: List[str] = []
    result: Dict[str, Any] = {"valid": False, "errors": errors, "warnings": warnings, "resources": [], "workloads": []}

    try:
        docs = parse_documents(extract_texts(ext, data, errors), errors)
    except (archive_guard.ArchiveRejected, RecursionError, MemoryError) as e:
        errors.append(str(e) if isinstance(e, archive_guard.ArchiveRejected) else "The upload could not be parsed safely.")
        docs = []
    if not docs and not errors:
        errors.append("No Kubernetes resources found. Upload Deployment/Service/ConfigMap manifests.")
    if len(docs) > MAX_RESOURCES:
        errors.append(f"Too many resources ({len(docs)}); the sandbox allows {MAX_RESOURCES}.")
        docs = docs[:MAX_RESOURCES]

    resources: List[Dict[str, Any]] = []
    seen: set = set()
    for src, raw in docs:
        kind, meta = raw.get("kind"), raw.get("metadata")
        if kind not in SCHEMAS:
            errors.append(f"{src}: kind '{kind}' is not allowed. Allowed kinds: Deployment, Service, ConfigMap.")
            continue
        if raw.get("apiVersion") != API_VERSIONS[kind]:
            errors.append(f"{src}: {kind} must use apiVersion {API_VERSIONS[kind]}.")
            continue
        name = (meta or {}).get("name") if isinstance(meta, dict) else None
        if not isinstance(name, str) or not DNS_LABEL.match(name):
            errors.append(f"{src}: {kind} needs a metadata.name that is a DNS label (lowercase letters, digits, '-').")
            continue
        if name.startswith(RESERVED_NAME_PREFIXES) or name in RESERVED_NAMES:
            errors.append(f"{kind}/{name}: names starting with {' or '.join(RESERVED_NAME_PREFIXES)} (and "
                          f"{', '.join(sorted(RESERVED_NAMES))}) are reserved for the platform.")
            continue
        n_before = len(errors)
        _check_schema(raw, SCHEMAS[kind], f"{kind}/{name}", errors)
        if len(errors) > n_before:
            continue
        if (kind, name) in seen:
            errors.append(f"{kind}/{name}: defined more than once.")
            continue
        seen.add((kind, name))
        doc = copy.deepcopy(raw)
        ns = doc["metadata"].pop("namespace", None)
        if ns:
            warnings.append(f"{kind}/{name}: metadata.namespace '{ns}' ignored; resources are deployed into an isolated namespace.")
        doc["metadata"]["labels"] = _labels_ok(doc["metadata"].get("labels"), f"{kind}/{name}.labels", errors)
        doc["metadata"].pop("annotations", None)
        resources.append(doc)

    configmaps = {d["metadata"]["name"] for d in resources if d["kind"] == "ConfigMap"}
    for d in resources:
        if d["kind"] == "Deployment":
            _validate_deployment(d, configmaps, errors, warnings)
        elif d["kind"] == "Service":
            _validate_service(d, errors)
        else:
            _validate_configmap(d, errors)

    deployments = [d for d in resources if d["kind"] == "Deployment"]
    if not deployments and not errors:
        errors.append("The upload must contain at least one Deployment.")
    apps = [d["spec"]["selector"]["matchLabels"].get("app") for d in deployments if "matchLabels" in d.get("spec", {}).get("selector", {})]
    for a in {x for x in apps if apps.count(x) > 1}:
        errors.append(f"More than one Deployment uses app label '{a}'; each service needs a unique 'app' label.")
    pod_labels = [d["spec"]["template"]["metadata"].get("labels", {}) for d in deployments]
    for d in resources:
        if d["kind"] == "Service" and isinstance(d["spec"].get("selector"), dict):
            if not any(all(l.get(k) == v for k, v in d["spec"]["selector"].items()) for l in pod_labels):
                warnings.append(f"Service/{d['metadata']['name']}: selector matches no Deployment in this upload.")

    if not errors:
        result["resources"] = resources
        result["workloads"] = [_summarize_workload(d) for d in deployments]
        result["valid"] = True
    return result
