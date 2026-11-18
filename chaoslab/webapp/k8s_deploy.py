"""
Cluster deployment of validated uploaded applications (strict sandbox)
======================================================================
Uploaded workloads run ONLY on the isolated tenant cluster / node pool (see tenant_isolation.py), with the
dedicated tenant credentials (CHAOSLAB_TENANT_KUBECONFIG). Each application gets its OWN namespace `cl-app-<id>`
(the name is derived from the numeric id, never from user input) containing:

  * Pod Security Admission `enforce=restricted`       (built-in second layer behind manifests.py)
  * ResourceQuota + LimitRange                        (bounded CPU / memory / ephemeral storage / pod count)
  * a default-deny NetworkPolicy that allows only in-namespace traffic and cluster DNS
  * the sanitized workloads, hardened at render time whatever the user wrote: sandboxed RuntimeClass, tenant node
    pool (nodeSelector + toleration), no service-account token, no service links, runAsNonRoot, seccomp
    RuntimeDefault, no privilege escalation, drop ALL capabilities, explicit CPU/memory limits, fully qualified
    images that are always pulled

Deploy order: preflight -> namespace + guard rails -> admission canary (a non-compliant server-side dry run MUST be
rejected by ChaosLab's policy) -> workloads -> Ready -> placement check (sandbox RuntimeClass, tenant node). Any
failure tears the namespace down. Teardown only ever deletes a namespace that carries ChaosLab's managed label AND
the matching app id. All cluster access goes through `kubectl()` so tests can replace it.
"""

import copy
import json
import subprocess
import time
from typing import Any, Dict, List, Tuple

import yaml

from . import config

MANAGED_LABEL = "chaoslab.io/managed-by"
MANAGED_VALUE = "chaoslab-online"
APP_ID_LABEL = "chaoslab.io/app-id"
USER_ID_LABEL = "chaoslab.io/user-id"
NS_PREFIX = "cl-app-"
PLATFORM_KINDS = ("Namespace", "ResourceQuota", "LimitRange", "NetworkPolicy")
DEFAULT_LIMITS = {"cpu": "250m", "memory": "256Mi"}
DEFAULT_REQUESTS = {"cpu": "50m", "memory": "64Mi"}
CANARY_NAME = "chaoslab-admission-canary"


def namespace_for(app_id: int) -> str:
    return f"{NS_PREFIX}{int(app_id)}"


def kubectl(args: List[str], input_text: str = None, timeout: int = 30) -> Tuple[bool, str]:
    """Run kubectl; returns (ok, stdout-or-stderr). Replaceable in tests."""
    if not config.TENANT_KUBECONFIG:
        # Never fall back to the server's own kubeconfig (which may be an admin or the demo cluster).
        return False, "No tenant cluster is configured (CHAOSLAB_TENANT_KUBECONFIG)."
    base = ["kubectl", "--kubeconfig", config.TENANT_KUBECONFIG]
    try:
        res = subprocess.run([*base, *args], input=input_text, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "kubectl was not found on the server."
    except subprocess.TimeoutExpired:
        return False, f"kubectl timed out after {timeout}s."
    except Exception as e:
        return False, str(e)
    return res.returncode == 0, (res.stdout if res.returncode == 0 else res.stderr).strip()


def qualified_image(image: str) -> str:
    """nginx:1.27 -> docker.io/library/nginx:1.27, so the admission policy can match registry prefixes exactly."""
    from .manifests import parse_image
    registry, repo, tag, digest = parse_image(image)
    if registry == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    return f"{registry}/{repo}" + (f":{tag}" if tag else "") + (f"@{digest}" if digest else "")


def _le(kind: str, a: Any, b: Any) -> bool:
    from .manifests import _cpu_millis, _mem_bytes
    fn = _cpu_millis if kind == "cpu" else _mem_bytes
    try:
        return fn(a) <= fn(b)
    except (TypeError, ValueError):
        return False


def harden_pod(pod: Dict[str, Any]) -> None:
    """Platform-owned pod settings. Applied AFTER the user's (validated) values, so nothing can override them."""
    from .tenant_isolation import pod_placement
    for k in ("hostNetwork", "hostPID", "hostIPC", "nodeName", "serviceAccountName", "serviceAccount",
              "initContainers", "ephemeralContainers", "priorityClassName", "schedulerName", "hostAliases",
              "shareProcessNamespace", "hostUsers", "overhead", "affinity"):
        pod.pop(k, None)
    pod.update(pod_placement())
    pod["automountServiceAccountToken"] = False
    pod["enableServiceLinks"] = False
    pod["securityContext"] = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
    for c in pod["containers"]:
        user_sc = c.get("securityContext") or {}
        sc = {k: user_sc[k] for k in ("runAsUser", "runAsGroup", "readOnlyRootFilesystem") if k in user_sc}
        sc.update({"runAsNonRoot": True, "allowPrivilegeEscalation": False, "privileged": False,
                   "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}})
        c["securityContext"] = sc
        c["image"] = qualified_image(c["image"])
        c["imagePullPolicy"] = "Always"           # never run an image that merely happens to be cached on the node
        res = c.setdefault("resources", {})
        limits, requests = res.setdefault("limits", {}), res.setdefault("requests", {})
        for kind in ("cpu", "memory"):
            if kind not in limits:                  # explicit limits always (the admission policy requires them)
                limits[kind] = requests[kind] if kind in requests and not _le(kind, requests[kind], DEFAULT_LIMITS[kind]) \
                    else DEFAULT_LIMITS[kind]
            if kind not in requests:
                requests[kind] = DEFAULT_REQUESTS[kind] if _le(kind, DEFAULT_REQUESTS[kind], limits[kind]) else limits[kind]
        for port in c.get("ports") or []:
            port.pop("hostPort", None)
            port.pop("hostIP", None)


def platform_objects(app_id: int, user_id: int) -> List[Dict[str, Any]]:
    """Namespace + guard rails. Owned by the platform: user manifests can never contain these kinds."""
    ns = namespace_for(app_id)
    platform_labels = {MANAGED_LABEL: MANAGED_VALUE, APP_ID_LABEL: str(int(app_id)), USER_ID_LABEL: str(int(user_id))}
    psa = {f"pod-security.kubernetes.io/{mode}": "restricted" for mode in ("enforce", "audit", "warn")}
    psa.update({f"pod-security.kubernetes.io/{mode}-version": "latest" for mode in ("enforce", "audit", "warn")})
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {**platform_labels, **psa}}},
        {"apiVersion": "v1", "kind": "ResourceQuota",
         "metadata": {"name": "chaoslab-quota", "namespace": ns, "labels": platform_labels},
         "spec": {"hard": {"pods": "10", "requests.cpu": "2", "requests.memory": "2Gi", "limits.cpu": "4",
                           "limits.memory": "4Gi", "services": "10", "configmaps": "20",
                           "count/deployments.apps": "10", "count/replicasets.apps": "30",
                           "services.loadbalancers": "0", "services.nodeports": "0",
                           "persistentvolumeclaims": "0", "secrets": "5",
                           "requests.ephemeral-storage": "2Gi", "limits.ephemeral-storage": "4Gi"}}},
        {"apiVersion": "v1", "kind": "LimitRange",
         "metadata": {"name": "chaoslab-limits", "namespace": ns, "labels": platform_labels},
         "spec": {"limits": [{"type": "Container", "default": {**DEFAULT_LIMITS, "ephemeral-storage": "512Mi"},
                              "defaultRequest": {**DEFAULT_REQUESTS, "ephemeral-storage": "64Mi"},
                              "max": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "1Gi"}}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "chaoslab-default", "namespace": ns, "labels": platform_labels},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                  "ingress": [{"from": [{"podSelector": {}}]}],
                  "egress": [{"to": [{"podSelector": {}}]},
                             {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                      "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                              "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}},
    ]


def workload_objects(app_id: int, user_id: int, resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The user's sanitized resources, placed in the app namespace and hardened."""
    ns = namespace_for(app_id)
    platform_labels = {MANAGED_LABEL: MANAGED_VALUE, APP_ID_LABEL: str(int(app_id)), USER_ID_LABEL: str(int(user_id))}
    out: List[Dict[str, Any]] = []
    for res in resources:
        if res.get("kind") not in ("Deployment", "Service", "ConfigMap"):   # defence in depth behind manifests.py
            raise ValueError(f"{res.get('kind')} objects cannot be deployed from an upload.")
        doc = copy.deepcopy(res)
        doc["metadata"]["namespace"] = ns
        doc["metadata"]["labels"] = {**doc["metadata"].get("labels", {}), **platform_labels}
        doc["metadata"].pop("annotations", None)
        if doc["kind"] == "Deployment":
            tmpl = doc["spec"]["template"]
            tmpl["metadata"]["labels"] = {**tmpl["metadata"].get("labels", {}), **platform_labels}
            tmpl["metadata"].pop("annotations", None)
            harden_pod(tmpl["spec"])
        out.append(doc)
    return out


def render_bundle(app_id: int, user_id: int, resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Namespace + guard rails + the sanitized, hardened resources, ready for `kubectl apply`."""
    return platform_objects(app_id, user_id) + workload_objects(app_id, user_id, resources)


def admission_canary(ns: str) -> Dict[str, Any]:
    """A deliberately non-compliant Deployment (root, privileged, host network, hostPath, no sandbox runtime)."""
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": CANARY_NAME, "namespace": ns},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": CANARY_NAME}},
                     "template": {"metadata": {"labels": {"app": CANARY_NAME}},
                                  "spec": {"hostNetwork": True,
                                           "volumes": [{"name": "host", "hostPath": {"path": "/"}}],
                                           "containers": [{"name": "c", "image": "docker.io/library/busybox:1.36",
                                                           "securityContext": {"privileged": True, "runAsUser": 0}}]}}}}


def verify_admission(ns: str) -> Tuple[bool, str]:
    """Prove that ChaosLab's admission policy is enforcing in this namespace: the canary dry run must be DENIED by it.
    (Server-side dry run: nothing is created.)"""
    from .tenant_isolation import WORKLOAD_POLICY
    ok, out = kubectl(["apply", "--dry-run=server", "-f", "-"], input_text=yaml.safe_dump(admission_canary(ns)),
                      timeout=30)
    if ok:
        return False, ("Admission control is NOT enforcing: a privileged, root, host-network test Deployment was "
                       "accepted in the sandbox namespace. Deployment refused.")
    if WORKLOAD_POLICY not in out and "ChaosLab tenant policy" not in out:
        return False, f"Could not confirm admission enforcement (unexpected response: {out[:200]})."
    return True, "admission policy enforcing"


def verify_placement(ns: str) -> Tuple[bool, str]:
    """Every pod must run with the sandbox RuntimeClass, on a tenant node, without a service-account token."""
    from .tenant_isolation import preflight
    ok, out = kubectl(["get", "pods", "-n", ns, "-o", "json"], timeout=20)
    if not ok:
        return False, f"Could not verify pod placement: {out[:200]}"
    try:
        pods = json.loads(out).get("items", [])
    except ValueError:
        return False, "Could not verify pod placement (unparseable response)."
    tenant_nodes = set(preflight().get("tenant_nodes") or [])
    if not pods:
        return False, "No pods found to verify."
    for pod in pods:
        name, spec = pod["metadata"]["name"], pod.get("spec") or {}
        if spec.get("runtimeClassName") != config.TENANT_RUNTIME_CLASS:
            return False, f"Pod {name} is not using the sandboxed RuntimeClass."
        if spec.get("nodeName") not in tenant_nodes:
            return False, f"Pod {name} is not running on a tenant node ({spec.get('nodeName')})."
        if spec.get("automountServiceAccountToken") is not False:
            return False, f"Pod {name} mounts a service-account token."
    return True, f"{len(pods)} pod(s) verified on the sandboxed runtime"


def _wait_ready(ns: str, expected: Dict[str, int], timeout: int) -> Tuple[bool, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, out = kubectl(["get", "deployments", "-n", ns, "-o", "json"], timeout=20)
        if ok:
            try:
                ready = {d["metadata"]["name"]: d.get("status", {}).get("readyReplicas", 0) or 0
                         for d in json.loads(out).get("items", [])}
                if expected and all(ready.get(n, 0) >= r for n, r in expected.items()):
                    return True, "ready"
            except (ValueError, KeyError):
                pass
        time.sleep(2)
    return False, "Timed out waiting for workloads to become Ready. " + _diagnose(ns)


def _diagnose(ns: str) -> str:
    """Short, actionable reason from pod container states (ImagePullBackOff, CrashLoopBackOff, ...)."""
    ok, out = kubectl(["get", "pods", "-n", ns, "-o", "json"], timeout=20)
    reasons: List[str] = []
    if ok:
        try:
            for pod in json.loads(out).get("items", []):
                for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                    waiting = (cs.get("state") or {}).get("waiting") or {}
                    if waiting.get("reason"):
                        reasons.append(f"{pod['metadata']['name']}/{cs['name']}: {waiting['reason']}"
                                       + (f" ({waiting['message'][:120]})" if waiting.get("message") else ""))
        except (ValueError, KeyError):
            pass
    return "; ".join(reasons[:4]) or "No pod diagnostics available."


def deploy(app_id: int, user_id: int, resources: List[Dict[str, Any]], timeout: int) -> Tuple[bool, str]:
    """Apply the sandboxed bundle and wait until every Deployment is Ready. Cleans up after itself on failure."""
    from .tenant_isolation import preflight
    gate = preflight(force=True)                 # re-checked here: the cluster may have changed since the request
    if not gate["ready"]:
        return False, f"Isolated tenant runtime unavailable: {gate['reason']}"
    ns = namespace_for(app_id)
    try:
        workloads = workload_objects(app_id, user_id, resources)
    except (ValueError, KeyError, TypeError) as e:
        return False, f"Invalid resources: {e}"

    steps = [
        ("namespace", lambda: kubectl(["apply", "-f", "-"], timeout=60,
                                      input_text=yaml.safe_dump_all(platform_objects(app_id, user_id)))),
        ("admission", lambda: verify_admission(ns)),
        ("workloads", lambda: kubectl(["apply", "-f", "-"], input_text=yaml.safe_dump_all(workloads), timeout=90)),
    ]
    for step, fn in steps:
        ok, out = fn()
        if not ok:
            undeploy(app_id)
            return False, out if step == "admission" else f"kubectl apply ({step}) failed: {out[:400]}"
    expected = {r["metadata"]["name"]: r["spec"].get("replicas", 1) for r in resources if r["kind"] == "Deployment"}
    ok, detail = _wait_ready(ns, expected, timeout)
    if ok:
        ok, detail = verify_placement(ns)
    if not ok:
        undeploy(app_id)
        return False, detail
    return True, "deployed"


def undeploy(app_id: int) -> Tuple[bool, str]:
    """Delete the app's namespace, but only if it is verifiably a ChaosLab-managed namespace for this app."""
    ns = namespace_for(app_id)
    ok, out = kubectl(["get", "namespace", ns, "-o", "json"], timeout=20)
    if not ok:
        if "NotFound" in out or "not found" in out.lower():
            return True, "namespace already absent"
        return False, f"Could not inspect namespace: {out[:200]}"
    try:
        labels = json.loads(out)["metadata"].get("labels", {})
    except (ValueError, KeyError):
        return False, "Could not parse namespace metadata."
    if labels.get(MANAGED_LABEL) != MANAGED_VALUE or labels.get(APP_ID_LABEL) != str(int(app_id)):
        return False, f"Refusing to delete namespace '{ns}': it is not managed by ChaosLab for this application."
    ok, out = kubectl(["delete", "namespace", ns, "--wait=false", "--ignore-not-found=true"], timeout=60)
    return ok, "deleting" if ok else f"kubectl delete failed: {out[:200]}"
