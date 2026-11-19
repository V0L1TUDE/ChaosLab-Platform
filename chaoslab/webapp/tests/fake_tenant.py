"""
A fake isolated tenant cluster for deployment tests (no kubectl needed).

It answers exactly the calls k8s_deploy / tenant_isolation make, and models every isolation control as a switch so
each one can be broken on purpose: admin credentials, the RuntimeClass and its handler, tenant nodes (control-plane,
untainted, not Ready), the admission policies (missing, wrong digest, not Deny) and admission enforcement itself
(the canary dry run accepted), plus pod placement after deploy.
"""

import json
import os
import tempfile

import yaml

from webapp import config, tenant_isolation

NODE = "tenant-1"


class FakeTenantCluster:
    def __init__(self):
        self.namespaces = {}            # name -> {"labels": {...}, "docs": [...]}
        self.calls = []
        self.never_ready = False        # ImagePullBackOff
        # isolation switches
        self.admin = False              # credentials are cluster-admin
        self.can_create_namespaces = True
        self.runtime_class = True
        self.handler = "runsc"
        self.rc_node_selector = True
        self.nodes = [{"name": NODE, "control_plane": False, "tainted": True, "ready": True}]
        self.policies = True
        self.policy_digest = None       # None = matches this server's configuration
        self.binding_actions = ["Deny"]
        self.admission_enforcing = True
        self.pod_runtime_class = None   # None = what was requested; "" = dropped (e.g. by a mutating webhook)
        self.pod_node = NODE
        self.fail_apply = None          # "namespace" | "workloads"

    # ------------------------------------------------------------------ the kubectl stand-in
    def kubectl(self, args, input_text=None, timeout=30):
        self.calls.append(list(args))
        key, value = tenant_isolation.node_label()
        if args[:2] == ["auth", "can-i"]:
            return self._can_i(args[2:])
        if args[:1] == ["apply"] and "--dry-run=server" in args:
            if self.admission_enforcing:
                return False, ('The deployments "chaoslab-admission-canary" is invalid: ValidatingAdmissionPolicy '
                               f"'{tenant_isolation.WORKLOAD_POLICY}' with binding '{tenant_isolation.WORKLOAD_POLICY}' "
                               "denied request: ChaosLab tenant policy: pods must use the sandboxed RuntimeClass.")
            return True, "deployment.apps/chaoslab-admission-canary created (server dry run)"
        if args[:2] == ["apply", "-f"]:
            docs = list(yaml.safe_load_all(input_text))
            step = "namespace" if any(d["kind"] == "Namespace" for d in docs) else "workloads"
            if self.fail_apply == step:
                return False, f"error applying {step}"
            for doc in docs:
                if doc["kind"] == "Namespace":
                    self.namespaces[doc["metadata"]["name"]] = {"labels": doc["metadata"]["labels"], "docs": []}
                else:
                    self.namespaces[doc["metadata"]["namespace"]]["docs"].append(doc)
            return True, "applied"
        if args[:2] == ["get", "runtimeclass"]:
            if not self.runtime_class:
                return False, 'Error from server (NotFound): runtimeclasses.node.k8s.io "gvisor" not found'
            rc = {"metadata": {"name": args[2]}, "handler": self.handler}
            if self.rc_node_selector:
                rc["scheduling"] = {"nodeSelector": {key: value}}
            return True, json.dumps(rc)
        if args[:2] == ["get", "nodes"]:
            items = []
            for n in self.nodes:
                labels = {key: value}
                if n["control_plane"]:
                    labels["node-role.kubernetes.io/control-plane"] = "true"
                items.append({"metadata": {"name": n["name"], "labels": labels},
                              "spec": {"taints": [{"key": key, "value": value, "effect": "NoSchedule"}] if n["tainted"] else []},
                              "status": {"conditions": [{"type": "Ready", "status": "True" if n["ready"] else "False"}]}})
            return True, json.dumps({"items": items})
        if args[:2] == ["get", "validatingadmissionpolicy"]:
            if not self.policies:
                return False, "Error from server (NotFound)"
            return True, json.dumps({"metadata": {"name": args[2], "annotations": {
                tenant_isolation.DIGEST_ANNOTATION: self.policy_digest or tenant_isolation.policy_digest()}},
                "spec": {"failurePolicy": "Fail"}})
        if args[:2] == ["get", "validatingadmissionpolicybinding"]:
            if not self.policies:
                return False, "Error from server (NotFound)"
            return True, json.dumps({"spec": {"policyName": args[2], "validationActions": self.binding_actions}})
        if args[:2] == ["get", "deployments"] and "-n" in args:
            ns = args[args.index("-n") + 1]
            items = [{"metadata": {"name": d["metadata"]["name"]},
                      "status": {"readyReplicas": 0 if self.never_ready else d["spec"]["replicas"]}}
                     for d in self.namespaces.get(ns, {"docs": []})["docs"] if d["kind"] == "Deployment"]
            return True, json.dumps({"items": items})
        if args[:2] == ["get", "pods"]:
            ns = args[args.index("-n") + 1]
            if self.never_ready:
                return True, json.dumps({"items": [{"metadata": {"name": "web-1"}, "spec": {}, "status": {
                    "containerStatuses": [{"name": "c", "state": {"waiting": {"reason": "ImagePullBackOff",
                                                                               "message": "pull access denied"}}}]}}]})
            pods = []
            for d in self.namespaces.get(ns, {"docs": []})["docs"]:
                if d["kind"] == "Deployment":
                    spec = json.loads(json.dumps(d["spec"]["template"]["spec"]))
                    if self.pod_runtime_class is not None:
                        spec["runtimeClassName"] = self.pod_runtime_class
                    spec["nodeName"] = self.pod_node
                    pods.append({"metadata": {"name": d["metadata"]["name"] + "-abc"}, "spec": spec, "status": {}})
            return True, json.dumps({"items": pods})
        if args[:2] == ["get", "namespace"]:
            ns = self.namespaces.get(args[2])
            if not ns:
                return False, 'Error from server (NotFound): namespaces "x" not found'
            return True, json.dumps({"metadata": {"labels": ns["labels"]}})
        if args[:2] == ["delete", "namespace"]:
            self.namespaces.pop(args[2], None)
            return True, "deleted"
        return False, f"unexpected kubectl call: {args}"

    def _can_i(self, rest):
        if self.admin:
            return True, "yes"
        forbidden = [a for a, _ in tenant_isolation.FORBIDDEN_CAN_I]
        if rest in forbidden:
            # real kubectl: "no" on stdout with exit code 1 (our wrapper then returns stderr), plus a warning on
            # stderr for cluster-scoped resources. Observed on k3s v1.31.
            if "--all-namespaces" not in rest:
                return False, f"Warning: resource '{rest[1].split('.')[0]}' is not namespace scoped\n"
            return False, ""
        if rest[:2] == ["create", "namespaces"] and not self.can_create_namespaces:
            return False, ""
        if rest in [a for a, _ in tenant_isolation.REQUIRED_CAN_I]:
            return True, "yes"
        return False, f"unexpected can-i {rest}"

    def query_cluster(self):
        out = {}
        for name, ns in self.namespaces.items():
            out[name] = [{"deployment": d["metadata"]["name"], "app_label": d["spec"]["selector"]["matchLabels"].get("app"),
                          "replicas": d["spec"]["replicas"], "ready": d["spec"]["replicas"]}
                         for d in ns["docs"] if d["kind"] == "Deployment"]
        return out


_KUBECONFIG = os.path.join(tempfile.mkdtemp(prefix="chaoslab-tenant-"), "tenant.kubeconfig")
with open(_KUBECONFIG, "w") as _f:
    _f.write("apiVersion: v1\nkind: Config\n")      # never used: kubectl is replaced in tests

TENANT_SETTINGS = ("DEPLOY_ENABLED", "TENANT_KUBECONFIG", "TENANT_RUNTIME_CLASS", "TENANT_NODE_LABEL",
                   "TENANT_ISOLATION_VERIFIED")


def configure_isolated():
    """Operator configuration of a fully isolated tenant runtime. Returns the previous values for restore()."""
    saved = {k: getattr(config, k) for k in TENANT_SETTINGS}
    config.DEPLOY_ENABLED = True
    config.TENANT_KUBECONFIG = _KUBECONFIG
    config.TENANT_RUNTIME_CLASS = "gvisor"
    config.TENANT_NODE_LABEL = "chaoslab.io/tenant-pool=true"
    config.TENANT_ISOLATION_VERIFIED = tenant_isolation.POLICY_VERSION
    tenant_isolation.reset_cache()
    return saved


def restore(saved):
    for k, v in saved.items():
        setattr(config, k, v)
    tenant_isolation.reset_cache()
