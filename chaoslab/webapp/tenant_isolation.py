"""
Tenant isolation for uploaded workloads (fail closed)
=====================================================
Namespaces, quotas and NetworkPolicies separate tenants from each other, but every container on a node shares
that node's kernel. Untrusted workloads therefore only run when ALL of these are in place, and the web app checks
them before every deployment (`preflight()`); if any check fails or cannot be performed, deployment is refused:

  1. Credentials   a dedicated tenant kubeconfig (CHAOSLAB_TENANT_KUBECONFIG) for a least-privilege ServiceAccount.
                   Refused if it can do everything (cluster-admin), manage RBAC, read Secrets cluster-wide, exec into
                   pods or modify nodes.
  2. Runtime       a RuntimeClass (CHAOSLAB_TENANT_RUNTIME_CLASS) whose handler is a sandboxed runtime (gVisor
                   `runsc` or Kata Containers) and that schedules only onto the tenant node pool.
  3. Nodes         a dedicated tenant node pool (CHAOSLAB_TENANT_NODE_LABEL): at least one Ready node, none of them a
                   control-plane node, every one tainted so that no other workload lands there.
  4. Admission     ChaosLab's two ValidatingAdmissionPolicies, bound with validationActions [Deny] and rendered from
                   THIS server's configuration (a digest annotation must match): the workload policy (runtime class,
                   tenant nodes, non-root, seccomp, no escalation, drop ALL capabilities, no host namespaces / ports /
                   paths, no service-account token, limits set, approved registries) and the deployer-scope policy
                   (the tenant credentials can only touch cl-app-* namespaces that ChaosLab labelled).
  5. Attestation   CHAOSLAB_TENANT_ISOLATION_VERIFIED equals POLICY_VERSION: the operator ran
                   chaoslab/deploy/tenant/verify_isolation.py, which checks what the API cannot show (the sandbox
                   kernel really runs, NetworkPolicy is really enforced).

During each deployment k8s_deploy additionally proves that admission is live (a deliberately non-compliant
server-side dry run must be rejected by the policy) and, once pods are Ready, that each pod runs with the sandbox
RuntimeClass on a tenant node. All cluster access goes through k8s_deploy.kubectl (replaceable in tests).
"""

import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import config

POLICY_VERSION = "2026-11-19.1"
WORKLOAD_POLICY = "chaoslab-tenant-workloads"
SCOPE_POLICY = "chaoslab-deployer-scope"
DEPLOYER_NAMESPACE = "chaoslab-system"
DEPLOYER_SA = "chaoslab-deployer"
DEPLOYER_USER = f"system:serviceaccount:{DEPLOYER_NAMESPACE}:{DEPLOYER_SA}"
MANAGED_LABEL = "chaoslab.io/managed-by"
MANAGED_VALUE = "chaoslab-online"
NS_PREFIX = "cl-app-"
DIGEST_ANNOTATION = "chaoslab.io/policy-digest"
VERSION_ANNOTATION = "chaoslab.io/policy-version"
SANDBOX_HANDLERS = ("runsc", "gvisor", "kata")          # gVisor, or any Kata handler (kata, kata-qemu, kata-clh, ...)
CONTROL_PLANE_LABELS = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")

# What the tenant credentials must NOT be allowed to do (kubectl auth can-i ...).
FORBIDDEN_CAN_I = [
    (["*", "*", "--all-namespaces"], "act as cluster-admin (every verb on every resource)"),
    (["create", "clusterrolebindings"], "create ClusterRoleBindings"),
    (["create", "rolebindings", "--all-namespaces"], "create RoleBindings"),
    (["get", "secrets", "--all-namespaces"], "read Secrets"),
    (["create", "pods/exec", "--all-namespaces"], "exec into pods"),
    (["patch", "nodes"], "modify nodes"),
    (["create", "validatingadmissionpolicies.admissionregistration.k8s.io"], "change admission policies"),
    (["delete", "validatingadmissionpolicybindings.admissionregistration.k8s.io"], "remove admission policy bindings"),
    (["create", "runtimeclasses.node.k8s.io"], "create RuntimeClasses"),
]
# What it needs to be able to do.
REQUIRED_CAN_I = [
    (["create", "namespaces"], "create namespaces"),
    (["delete", "namespaces"], "delete namespaces"),
    (["create", "deployments.apps", "--all-namespaces"], "create Deployments"),
    (["create", "networkpolicies.networking.k8s.io", "--all-namespaces"], "create NetworkPolicies"),
    (["create", "resourcequotas", "--all-namespaces"], "create ResourceQuotas"),
]

_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def _kubectl(args: List[str], input_text: str = None, timeout: int = 20) -> Tuple[bool, str]:
    from . import k8s_deploy                      # resolved at call time so tests can replace k8s_deploy.kubectl
    return k8s_deploy.kubectl(args, input_text=input_text, timeout=timeout)


def node_label() -> Tuple[str, str]:
    key, _, value = config.TENANT_NODE_LABEL.partition("=")
    return key.strip(), value.strip()


def runtime_class_name() -> str:
    return config.TENANT_RUNTIME_CLASS or "gvisor"


def tenant_registries() -> List[str]:
    return list(config.ALLOWED_REGISTRIES)


def policy_digest() -> str:
    """Identifies the configuration the cluster's policies must have been rendered from."""
    key, value = node_label()
    material = json.dumps({"v": POLICY_VERSION, "runtime": runtime_class_name(), "node": [key, value],
                           "registries": sorted(tenant_registries())}, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


# ============================================================================ policy rendering
def _cel_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_policies(runtime_class: Optional[str] = None) -> List[Dict[str, Any]]:
    """The two ValidatingAdmissionPolicies and their Deny bindings, rendered from this server's configuration."""
    runtime_class = runtime_class or runtime_class_name()
    key, value = node_label()
    registries = "[" + ", ".join(_cel_str(r.rstrip("/") + "/") for r in tenant_registries()) + "]"
    ann = {VERSION_ANNOTATION: POLICY_VERSION, DIGEST_ANNOTATION: policy_digest()}
    s = "variables.spec"
    each = f"{s}.containers.all(c, "

    def v(expr: str, msg: str) -> Dict[str, str]:
        return {"expression": expr, "message": f"ChaosLab tenant policy: {msg}"}

    workload_rules = [
        v(f"has({s}.runtimeClassName) && {s}.runtimeClassName == {_cel_str(runtime_class)}",
          f"pods must use the sandboxed RuntimeClass '{runtime_class}'."),
        v(f"has({s}.nodeSelector) && {_cel_str(key)} in {s}.nodeSelector && {s}.nodeSelector[{_cel_str(key)}] == {_cel_str(value)}",
          f"pods must be pinned to the tenant node pool ({key}={value})."),
        v(f"!has({s}.nodeName)", "pods may not choose a node directly."),
        v(f"!(has({s}.hostNetwork) && {s}.hostNetwork) && !(has({s}.hostPID) && {s}.hostPID) && "
          f"!(has({s}.hostIPC) && {s}.hostIPC)", "host namespaces (network, PID, IPC) are not allowed."),
        v(f"!has({s}.volumes) || {s}.volumes.all(vol, has(vol.emptyDir) || has(vol.configMap))",
          "only emptyDir and configMap volumes are allowed (no hostPath, secrets, PVCs or projected tokens)."),
        v(f"has({s}.automountServiceAccountToken) && {s}.automountServiceAccountToken == false",
          "service-account tokens must not be mounted."),
        v(f"(!has({s}.initContainers) || size({s}.initContainers) == 0) && "
          f"(!has({s}.ephemeralContainers) || size({s}.ephemeralContainers) == 0)",
          "init and ephemeral containers are not allowed."),
        v(f"has({s}.securityContext) && has({s}.securityContext.seccompProfile) && "
          f"{s}.securityContext.seccompProfile.type == 'RuntimeDefault'", "the pod seccomp profile must be RuntimeDefault."),
        v(f"!has({s}.securityContext) || !has({s}.securityContext.runAsUser) || {s}.securityContext.runAsUser != 0",
          "pods must not run as root (runAsUser 0)."),
        v(f"!has({s}.securityContext) || !has({s}.securityContext.sysctls) || size({s}.securityContext.sysctls) == 0",
          "sysctls are not allowed."),
        v(each + "has(c.securityContext) && has(c.securityContext.runAsNonRoot) && c.securityContext.runAsNonRoot == true"
          " && (!has(c.securityContext.runAsUser) || c.securityContext.runAsUser != 0))",
          "every container must set runAsNonRoot: true and must not run as user 0."),
        v(each + "has(c.securityContext) && has(c.securityContext.allowPrivilegeEscalation) && "
          "c.securityContext.allowPrivilegeEscalation == false && "
          "(!has(c.securityContext.privileged) || c.securityContext.privileged == false))",
          "containers must not be privileged and must set allowPrivilegeEscalation: false."),
        v(each + "has(c.securityContext) && has(c.securityContext.capabilities) && "
          "has(c.securityContext.capabilities.drop) && 'ALL' in c.securityContext.capabilities.drop && "
          "(!has(c.securityContext.capabilities.add) || size(c.securityContext.capabilities.add) == 0))",
          "containers must drop ALL capabilities and add none."),
        v(each + "!has(c.securityContext) || !has(c.securityContext.seccompProfile) || "
          "c.securityContext.seccompProfile.type == 'RuntimeDefault')", "container seccomp profiles must be RuntimeDefault."),
        v(each + "has(c.resources) && has(c.resources.limits) && 'cpu' in c.resources.limits && "
          "'memory' in c.resources.limits)", "every container must set CPU and memory limits."),
        v(each + "!has(c.ports) || c.ports.all(p, !has(p.hostPort) || p.hostPort == 0))", "hostPort is not allowed."),
        v(each + f"{registries}.exists(r, c.image.startsWith(r)))",
          "images must come from an approved registry (fully qualified, e.g. docker.io/library/nginx:1.27)."),
    ]
    workload = {
        "apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": WORKLOAD_POLICY, "annotations": ann},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {"resourceRules": [
                {"apiGroups": [""], "apiVersions": ["v1"], "resources": ["pods"], "operations": ["CREATE", "UPDATE"]},
                {"apiGroups": ["apps"], "apiVersions": ["v1"], "resources": ["deployments", "replicasets"],
                 "operations": ["CREATE", "UPDATE"]},
            ]},
            "variables": [{"name": "spec", "expression": "object.kind == 'Pod' ? object.spec : object.spec.template.spec"}],
            "validations": workload_rules,
        },
    }
    managed = (f"has(namespaceObject.metadata.labels) && {_cel_str(MANAGED_LABEL)} in namespaceObject.metadata.labels && "
               f"namespaceObject.metadata.labels[{_cel_str(MANAGED_LABEL)}] == {_cel_str(MANAGED_VALUE)}")
    ns_labels_ok = (f"has(object.metadata.labels) && {_cel_str(MANAGED_LABEL)} in object.metadata.labels && "
                    f"object.metadata.labels[{_cel_str(MANAGED_LABEL)}] == {_cel_str(MANAGED_VALUE)} && "
                    "'pod-security.kubernetes.io/enforce' in object.metadata.labels && "
                    "object.metadata.labels['pod-security.kubernetes.io/enforce'] == 'restricted'")
    old_managed = (f"has(oldObject.metadata.labels) && {_cel_str(MANAGED_LABEL)} in oldObject.metadata.labels && "
                   f"oldObject.metadata.labels[{_cel_str(MANAGED_LABEL)}] == {_cel_str(MANAGED_VALUE)}")
    scope = {
        "apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": SCOPE_POLICY, "annotations": ann},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {"resourceRules": [{"apiGroups": ["*"], "apiVersions": ["*"], "resources": ["*"],
                                                    "operations": ["CREATE", "UPDATE", "DELETE"]}]},
            "matchConditions": [{"name": "only-the-chaoslab-deployer",
                                 "expression": f"request.userInfo.username == {_cel_str(DEPLOYER_USER)}"},
                                # `kubectl auth can-i` (SelfSubjectAccessReview) and token reviews are cluster-scoped
                                # "creates" that write nothing; the preflight depends on them.
                                {"name": "not-access-reviews",
                                 "expression": "!(request.resource.group in "
                                               "['authorization.k8s.io', 'authentication.k8s.io'])"}],
            "validations": [
                v(f"request.kind.kind == 'Namespace' || (request.namespace != '' && namespaceObject != null && {managed})",
                  "the ChaosLab deployer may only change objects inside namespaces that ChaosLab manages."),
                v(f"request.kind.kind != 'Namespace' || request.name.startsWith({_cel_str(NS_PREFIX)})",
                  f"the ChaosLab deployer may only manage namespaces named {NS_PREFIX}<id>."),
                v(f"request.kind.kind != 'Namespace' || request.operation == 'DELETE' || ({ns_labels_ok})",
                  "ChaosLab namespaces must keep the managed-by label and Pod Security 'restricted'."),
                v(f"request.kind.kind != 'Namespace' || request.operation != 'DELETE' || ({old_managed})",
                  "the ChaosLab deployer may only delete namespaces that ChaosLab manages."),
            ],
        },
    }
    bindings = [
        {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
         "metadata": {"name": WORKLOAD_POLICY, "annotations": ann},
         "spec": {"policyName": WORKLOAD_POLICY, "validationActions": ["Deny"],
                  "matchResources": {"namespaceSelector": {"matchLabels": {MANAGED_LABEL: MANAGED_VALUE}}}}},
        {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
         "metadata": {"name": SCOPE_POLICY, "annotations": ann},
         "spec": {"policyName": SCOPE_POLICY, "validationActions": ["Deny"]}},
    ]
    return [workload, scope] + bindings


def render_runtime_class(handler: str = "runsc") -> Dict[str, Any]:
    key, value = node_label()
    return {"apiVersion": "node.k8s.io/v1", "kind": "RuntimeClass",
            "metadata": {"name": runtime_class_name()}, "handler": handler,
            "scheduling": {"nodeSelector": {key: value},
                           "tolerations": [{"key": key, "operator": "Equal", "value": value, "effect": "NoSchedule"}]}}


def pod_placement() -> Dict[str, Any]:
    """Fields every tenant pod gets at render time (the admission policy requires them)."""
    key, value = node_label()
    return {"runtimeClassName": config.TENANT_RUNTIME_CLASS, "nodeSelector": {key: value},
            "tolerations": [{"key": key, "operator": "Equal", "value": value, "effect": "NoSchedule"}]}


# ============================================================================ preflight
def _check(checks: List[Dict[str, Any]], name: str, ok: bool, detail: str) -> bool:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    return ok


def _can_i(args: List[str]) -> Optional[bool]:
    """True / False, or None when the question could not be answered (treated as a failure by the caller)."""
    ok, out = _kubectl(["auth", "can-i", *args], timeout=15)
    # kubectl adds e.g. "Warning: resource 'nodes' is not namespace scoped" on stderr for cluster-scoped resources.
    lines = [l.strip() for l in out.splitlines() if l.strip() and not l.strip().lower().startswith("warning:")]
    answer = lines[-1].lower() if lines else ""
    if ok and answer.startswith("yes"):
        return True
    # kubectl answers "no" with exit code 1 (our wrapper then returns stderr, usually empty). Anything else
    # (connection errors, unknown resources) is "could not tell". A misread can never open the gate: the
    # REQUIRED checks fail in the same situations, so preflight stays closed.
    if answer.startswith("no") or (not ok and answer == ""):
        return False
    return None


def _get_json(args: List[str]) -> Tuple[Optional[Dict[str, Any]], str]:
    ok, out = _kubectl(["get", *args, "-o", "json"], timeout=20)
    if not ok:
        return None, out[:200]
    try:
        return json.loads(out), ""
    except ValueError:
        return None, "unparseable response"


def _config_checks(checks: List[Dict[str, Any]]) -> bool:
    ok = _check(checks, "deploy_enabled", config.DEPLOY_ENABLED,
                "CHAOSLAB_DEPLOY_ENABLED=true" if config.DEPLOY_ENABLED else
                "Deploying uploaded applications is disabled on this server (CHAOSLAB_DEPLOY_ENABLED=false).")
    kc = config.TENANT_KUBECONFIG
    ok &= _check(checks, "tenant_kubeconfig", bool(kc) and os.path.isfile(kc),
                 "tenant kubeconfig present" if kc and os.path.isfile(kc) else
                 "CHAOSLAB_TENANT_KUBECONFIG is not set or the file does not exist (the server's own kubectl "
                 "credentials are never used for uploaded workloads).")
    ok &= _check(checks, "runtime_class_configured", bool(config.TENANT_RUNTIME_CLASS),
                 f"RuntimeClass '{config.TENANT_RUNTIME_CLASS}'" if config.TENANT_RUNTIME_CLASS else
                 "CHAOSLAB_TENANT_RUNTIME_CLASS is not set (a gVisor or Kata RuntimeClass is required).")
    key, value = node_label()
    ok &= _check(checks, "node_label_configured", bool(key and value),
                 f"tenant node pool {key}={value}" if key and value else "CHAOSLAB_TENANT_NODE_LABEL must be key=value.")
    ok &= _check(checks, "operator_attestation", config.TENANT_ISOLATION_VERIFIED == POLICY_VERSION,
                 "verify_isolation.py attested for this policy version" if config.TENANT_ISOLATION_VERIFIED == POLICY_VERSION
                 else f"CHAOSLAB_TENANT_ISOLATION_VERIFIED must be '{POLICY_VERSION}' after "
                      "chaoslab/deploy/tenant/verify_isolation.py passes against the tenant cluster.")
    return bool(ok)


def _identity_checks(checks: List[Dict[str, Any]]) -> bool:
    ok = True
    for args, what in FORBIDDEN_CAN_I:
        ans = _can_i(args)
        ok &= _check(checks, f"cannot:{what}", ans is False,
                     f"tenant credentials cannot {what}" if ans is False else
                     (f"tenant credentials CAN {what}: use the least-privilege chaoslab-deployer ServiceAccount, "
                      "never cluster-admin." if ans else f"could not verify that the credentials cannot {what}."))
    for args, what in REQUIRED_CAN_I:
        ans = _can_i(args)
        ok &= _check(checks, f"can:{what}", ans is True,
                     f"tenant credentials can {what}" if ans else f"tenant credentials cannot {what} (apply deployer-rbac.yaml).")
    return bool(ok)


def _runtime_checks(checks: List[Dict[str, Any]]) -> bool:
    rc, err = _get_json(["runtimeclass", config.TENANT_RUNTIME_CLASS])
    if not rc:
        return _check(checks, "runtime_class", False, f"RuntimeClass '{config.TENANT_RUNTIME_CLASS}' not found ({err}).")
    handler = str(rc.get("handler", ""))
    ok = _check(checks, "runtime_handler", handler.startswith(SANDBOX_HANDLERS),
                f"handler '{handler}' is a sandboxed runtime" if handler.startswith(SANDBOX_HANDLERS) else
                f"handler '{handler}' is not a sandboxed runtime (expected gVisor runsc or Kata).")
    key, value = node_label()
    sel = ((rc.get("scheduling") or {}).get("nodeSelector") or {})
    ok &= _check(checks, "runtime_scheduling", sel.get(key) == value,
                 "RuntimeClass schedules onto the tenant pool" if sel.get(key) == value else
                 f"RuntimeClass scheduling.nodeSelector must include {key}={value}.")
    return bool(ok)


def _node_checks(checks: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    key, value = node_label()
    nodes, err = _get_json(["nodes", "-l", f"{key}={value}"])
    if nodes is None:
        return _check(checks, "tenant_nodes", False, f"could not list tenant nodes ({err})."), []
    items = nodes.get("items", [])
    ready, names, problems = 0, [], []
    for n in items:
        name = n["metadata"]["name"]
        labels = n["metadata"].get("labels", {}) or {}
        names.append(name)
        if any(l in labels for l in CONTROL_PLANE_LABELS):
            problems.append(f"{name} is a control-plane node")
        taints = (n.get("spec") or {}).get("taints") or []
        if not any(t.get("key") == key and t.get("value") == value and t.get("effect") in ("NoSchedule", "NoExecute")
                   for t in taints):
            problems.append(f"{name} lacks the taint {key}={value}:NoSchedule")
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in (n.get("status") or {}).get("conditions", [])):
            ready += 1
    ok = _check(checks, "tenant_nodes", ready > 0 and not problems,
                f"{ready} Ready tenant node(s), none control-plane, all tainted" if ready and not problems else
                ("no Ready tenant nodes labelled " + f"{key}={value}" if not ready else "; ".join(problems[:4])))
    return ok, names


def _admission_checks(checks: List[Dict[str, Any]]) -> bool:
    ok = True
    digest = policy_digest()
    for name in (WORKLOAD_POLICY, SCOPE_POLICY):
        pol, err = _get_json(["validatingadmissionpolicy", name])
        ann = ((pol or {}).get("metadata") or {}).get("annotations") or {}
        good = bool(pol) and ann.get(DIGEST_ANNOTATION) == digest and (pol.get("spec") or {}).get("failurePolicy", "Fail") == "Fail"
        ok &= _check(checks, f"policy:{name}", good,
                     f"policy {name} installed for this configuration" if good else
                     (f"policy {name} not found ({err})." if not pol else
                      f"policy {name} was rendered for a different configuration (digest {ann.get(DIGEST_ANNOTATION)}, "
                      f"expected {digest}): re-apply `python -m webapp.tenant_isolation render-policy`."))
        b, err = _get_json(["validatingadmissionpolicybinding", name])
        actions = ((b or {}).get("spec") or {}).get("validationActions") or []
        bound = bool(b) and "Deny" in actions and (b.get("spec") or {}).get("policyName") == name
        ok &= _check(checks, f"binding:{name}", bound,
                     f"binding {name} denies violations" if bound else f"binding {name} missing or not in Deny mode.")
    return bool(ok)


def preflight(force: bool = False) -> Dict[str, Any]:
    """-> {"ready": bool, "reason": str, "checks": [...], "tenant_nodes": [...]} (cached ~30 s)."""
    now = time.time()
    if not force and _cache["value"] and now - _cache["at"] < 30:
        return _cache["value"]
    checks: List[Dict[str, Any]] = []
    nodes: List[str] = []
    ready = _config_checks(checks)
    if ready:                                                  # only talk to the cluster once the config is complete
        ready = _identity_checks(checks)
        ready = _runtime_checks(checks) and ready
        node_ok, nodes = _node_checks(checks)
        ready = node_ok and ready
        ready = _admission_checks(checks) and ready
    failed = [c["detail"] for c in checks if not c["ok"]]
    value = {"ready": not failed, "reason": "Isolated tenant runtime verified." if not failed else failed[0],
             "failed": failed, "checks": checks, "tenant_nodes": nodes, "policy_version": POLICY_VERSION}
    _cache.update(at=now, value=value)
    return value


def reset_cache() -> None:
    _cache.update(at=0.0, value=None)


# ============================================================================ CLI (operator)
def main(argv: List[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "render-policy":
        sys.stdout.write(yaml.safe_dump_all(render_policies(), sort_keys=False))
        return 0
    if cmd == "render-runtimeclass":
        handler = argv[1] if len(argv) > 1 else "runsc"
        sys.stdout.write(yaml.safe_dump(render_runtime_class(handler), sort_keys=False))
        return 0
    if cmd == "preflight":
        result = preflight(force=True)
        for c in result["checks"]:
            print(("  ok    " if c["ok"] else "  FAIL  ") + c["detail"])
        print("READY: uploaded workloads may be deployed." if result["ready"] else "NOT READY: deployment stays disabled.")
        return 0 if result["ready"] else 1
    print("usage: python -m webapp.tenant_isolation render-policy | render-runtimeclass [runsc|kata] | preflight")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
