"""
Cluster deployment of validated uploaded applications (strict sandbox)
======================================================================
Each uploaded application gets its OWN namespace `cl-app-<id>` (the name is derived from the numeric id,
never from user input) containing:

  * Pod Security Admission label `enforce=baseline`  (cluster-side second layer behind manifests.py)
  * ResourceQuota + LimitRange                        (bounded CPU / memory / pod count)
  * a default-deny NetworkPolicy that allows only in-namespace traffic and DNS (enforced when the cluster's
    CNI supports NetworkPolicy)
  * the sanitized workloads, hardened at apply time (no service-account token, no service links,
    seccomp RuntimeDefault, no privilege escalation)

Teardown only ever deletes a namespace that carries ChaosLab's managed label AND the matching app id.
All cluster access goes through `kubectl()` so tests can replace it.
"""

import copy
import json
import subprocess
import time
from typing import Any, Dict, List, Tuple

import yaml

MANAGED_LABEL = "chaoslab.io/managed-by"
MANAGED_VALUE = "chaoslab-online"
APP_ID_LABEL = "chaoslab.io/app-id"
USER_ID_LABEL = "chaoslab.io/user-id"
NS_PREFIX = "cl-app-"


def namespace_for(app_id: int) -> str:
    return f"{NS_PREFIX}{int(app_id)}"


def kubectl(args: List[str], input_text: str = None, timeout: int = 30) -> Tuple[bool, str]:
    """Run kubectl; returns (ok, stdout-or-stderr). Replaceable in tests."""
    try:
        res = subprocess.run(["kubectl", *args], input=input_text, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "kubectl was not found on the server."
    except subprocess.TimeoutExpired:
        return False, f"kubectl timed out after {timeout}s."
    except Exception as e:
        return False, str(e)
    return res.returncode == 0, (res.stdout if res.returncode == 0 else res.stderr).strip()


def render_bundle(app_id: int, user_id: int, resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Namespace + guard rails + the sanitized resources, ready for `kubectl apply`."""
    ns = namespace_for(app_id)
    platform_labels = {MANAGED_LABEL: MANAGED_VALUE, APP_ID_LABEL: str(int(app_id)), USER_ID_LABEL: str(int(user_id))}
    bundle: List[Dict[str, Any]] = [
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": {"name": ns, "labels": {**platform_labels,
                                             "pod-security.kubernetes.io/enforce": "baseline",
                                             "pod-security.kubernetes.io/enforce-version": "latest"}}},
        {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "chaoslab-quota", "namespace": ns},
         "spec": {"hard": {"pods": "10", "requests.cpu": "2", "requests.memory": "2Gi", "limits.cpu": "4",
                           "limits.memory": "4Gi", "services": "10", "configmaps": "20",
                           "count/deployments.apps": "10"}}},
        {"apiVersion": "v1", "kind": "LimitRange", "metadata": {"name": "chaoslab-limits", "namespace": ns},
         "spec": {"limits": [{"type": "Container", "default": {"cpu": "250m", "memory": "256Mi"},
                              "defaultRequest": {"cpu": "50m", "memory": "64Mi"},
                              "max": {"cpu": "1", "memory": "1Gi"}}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "chaoslab-default", "namespace": ns},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                  "ingress": [{"from": [{"podSelector": {}}]}],
                  "egress": [{"to": [{"podSelector": {}}]},
                             {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                              "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}},
    ]
    for res in resources:
        doc = copy.deepcopy(res)
        doc["metadata"]["namespace"] = ns
        doc["metadata"]["labels"] = {**doc["metadata"].get("labels", {}), **platform_labels}
        if doc["kind"] == "Deployment":
            tmpl = doc["spec"]["template"]
            tmpl["metadata"]["labels"] = {**tmpl["metadata"].get("labels", {}), **platform_labels}
            pod = tmpl["spec"]
            pod["automountServiceAccountToken"] = False
            pod["enableServiceLinks"] = False
            pod["securityContext"] = {"seccompProfile": {"type": "RuntimeDefault"}}
            for c in pod["containers"]:
                c.setdefault("imagePullPolicy", "IfNotPresent")
                c.setdefault("securityContext", {}).setdefault("allowPrivilegeEscalation", False)
        bundle.append(doc)
    return bundle


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
    ns = namespace_for(app_id)
    bundle = render_bundle(app_id, user_id, resources)
    ok, out = kubectl(["apply", "-f", "-"], input_text=yaml.safe_dump_all(bundle), timeout=90)
    if not ok:
        undeploy(app_id)
        return False, f"kubectl apply failed: {out[:400]}"
    expected = {r["metadata"]["name"]: r["spec"].get("replicas", 1) for r in resources if r["kind"] == "Deployment"}
    ok, detail = _wait_ready(ns, expected, timeout)
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
