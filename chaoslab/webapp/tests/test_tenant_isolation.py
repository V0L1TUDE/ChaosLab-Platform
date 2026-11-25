"""Fail-closed tenant isolation: preflight, workload policy, admission/placement proofs, cleanup and authorization.
All against fake_tenant.FakeTenantCluster: these are CODE-LEVEL tests; live-cluster verification is
chaoslab/deploy/tenant/verify_isolation.py."""

import copy
import os
import time
import unittest

import yaml

import harness
from harness import Client
from fake_tenant import FakeTenantCluster, configure_isolated, restore
from test_manifests import dep, svc

from webapp import catalog, config, db, deployments, k8s_deploy, manifests, tenant_isolation  # noqa: E402

DEPLOY_DIR = os.path.join(config.CHAOSLAB_DIR, "deploy", "tenant")


def manifest_bytes(*docs):
    return "\n---\n".join(yaml.safe_dump(d) for d in docs).encode()


class Base(unittest.TestCase):
    def setUp(self):
        harness.start()
        with db.session() as conn:
            conn.execute("UPDATE applications SET status = 'validated', namespace = NULL, deployed_at = NULL "
                         "WHERE status IN ('deploying', 'deployed', 'deploy_failed')")
        self.fake = FakeTenantCluster()
        self.saved = (k8s_deploy.kubectl, catalog._query_cluster, config.DEPLOY_TIMEOUT_SECONDS)
        self.saved_tenant = configure_isolated()
        k8s_deploy.kubectl = self.fake.kubectl
        catalog._query_cluster = self.fake.query_cluster
        config.DEPLOY_TIMEOUT_SECONDS = 1
        self.c = Client()
        self.user, *_ = self.c.register()

    def tearDown(self):
        k8s_deploy.kubectl, catalog._query_cluster, config.DEPLOY_TIMEOUT_SECONDS = self.saved
        restore(self.saved_tenant)

    def upload(self, *docs, client=None):
        s, d, _ = (client or self.c).upload("/api/applications", {"name": "App"}, "app.yaml", manifest_bytes(*docs))
        self.assertEqual(s, 201, d)
        return d["application"]

    def status(self, app_id, wanted, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            a = self.c.get(f"/api/applications/{app_id}")[1]["application"]
            if a["status"] in wanted:
                return a
            time.sleep(0.05)
        self.fail(f"app {app_id} stuck in {a['status']}")


# ============================================================================ preflight: fail closed
class PreflightTests(Base):
    def assert_blocked(self, needle):
        tenant_isolation.reset_cache()
        app = self.upload(dep())
        n_calls = len(self.fake.calls)
        s, d, _ = self.c.post(f"/api/applications/{app['id']}/deploy")
        self.assertEqual(s, 503, d)
        self.assertIn(needle, d["error"])
        self.assertEqual(self.fake.namespaces, {})                       # nothing was created
        self.assertFalse([c for c in self.fake.calls[n_calls:] if c[:1] == ["apply"]])
        return d["error"]

    def test_fully_isolated_runtime_is_ready(self):
        r = tenant_isolation.preflight(force=True)
        self.assertTrue(r["ready"], r["failed"])
        self.assertEqual(r["tenant_nodes"], ["tenant-1"])

    def test_default_configuration_is_closed(self):
        restore(self.saved_tenant)                      # the shipped defaults
        self.assertFalse(config.DEPLOY_ENABLED)
        r = tenant_isolation.preflight(force=True)
        self.assertFalse(r["ready"])
        self.assertIn("CHAOSLAB_DEPLOY_ENABLED", r["reason"])
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["auth", "can-i"]])   # never even asked the cluster
        self.saved_tenant = configure_isolated()

    def test_missing_operator_settings_block(self):
        for attr, value, needle in (("TENANT_KUBECONFIG", "", "CHAOSLAB_TENANT_KUBECONFIG"),
                                    ("TENANT_KUBECONFIG", "/no/such/file", "CHAOSLAB_TENANT_KUBECONFIG"),
                                    ("TENANT_RUNTIME_CLASS", "", "CHAOSLAB_TENANT_RUNTIME_CLASS"),
                                    ("TENANT_NODE_LABEL", "nolabel", "key=value"),
                                    ("TENANT_ISOLATION_VERIFIED", "", "verify-isolation"),
                                    ("TENANT_ISOLATION_VERIFIED", "2020-01-01.0", "verify-isolation")):
            saved = getattr(config, attr)
            setattr(config, attr, value)
            try:
                with self.subTest(attr=attr, value=value):
                    self.assert_blocked(needle if needle != "verify-isolation" else "CHAOSLAB_TENANT_ISOLATION_VERIFIED")
            finally:
                setattr(config, attr, saved)

    def test_cluster_admin_credentials_are_refused(self):
        self.fake.admin = True
        err = self.assert_blocked("cluster-admin")
        self.assertIn("least-privilege", err)

    def test_missing_required_permission(self):
        self.fake.can_create_namespaces = False
        self.assert_blocked("cannot create namespaces")

    def test_runtime_class_missing_or_not_sandboxed(self):
        self.fake.runtime_class = False
        self.assert_blocked("RuntimeClass 'gvisor' not found")
        self.fake.runtime_class, self.fake.handler = True, "runc"
        self.assert_blocked("not a sandboxed runtime")
        self.fake.handler, self.fake.rc_node_selector = "kata-qemu", False
        self.assert_blocked("scheduling.nodeSelector")
        self.fake.rc_node_selector = True
        tenant_isolation.reset_cache()
        self.assertTrue(tenant_isolation.preflight(force=True)["ready"])        # kata handlers are accepted

    def test_tenant_nodes(self):
        self.fake.nodes = []
        self.assert_blocked("no Ready tenant nodes")
        self.fake.nodes = [{"name": "cp", "control_plane": True, "tainted": True, "ready": True}]
        self.assert_blocked("control-plane")
        self.fake.nodes = [{"name": "n", "control_plane": False, "tainted": False, "ready": True}]
        self.assert_blocked("lacks the taint")
        self.fake.nodes = [{"name": "n", "control_plane": False, "tainted": True, "ready": False}]
        self.assert_blocked("no Ready tenant nodes")

    def test_admission_policies_must_be_installed_current_and_denying(self):
        self.fake.policies = False
        self.assert_blocked("not found")
        self.fake.policies, self.fake.policy_digest = True, "0000000000000000"
        self.assert_blocked("different configuration")
        self.fake.policy_digest, self.fake.binding_actions = None, ["Warn", "Audit"]
        self.assert_blocked("not in Deny mode")

    def test_kubectl_never_falls_back_to_server_credentials(self):
        k8s_deploy.kubectl = self.saved[0]                     # the real wrapper
        saved = config.TENANT_KUBECONFIG
        config.TENANT_KUBECONFIG = ""
        try:
            ok, msg = k8s_deploy.kubectl(["get", "namespaces"])
        finally:
            config.TENANT_KUBECONFIG = saved
            k8s_deploy.kubectl = self.fake.kubectl
        self.assertFalse(ok)
        self.assertIn("No tenant cluster", msg)


# ============================================================================ deploy-time proofs + cleanup
class DeployLifecycleTests(Base):
    def deploy_and_fail(self):
        app = self.upload(dep(), svc())
        s, d, _ = self.c.post(f"/api/applications/{app['id']}/deploy")
        self.assertEqual(s, 202, d)
        a = self.status(app["id"], ("deploy_failed",))
        self.assertNotIn(f"cl-app-{app['id']}", self.fake.namespaces)     # cleaned up
        self.assertIsNone(a["deployment"]["namespace"])
        return a["deployment"]["error"]

    def test_admission_not_enforcing_fails_closed(self):
        self.fake.admission_enforcing = False
        err = self.deploy_and_fail()
        self.assertIn("NOT enforcing", err)
        self.assertEqual([c for c in self.fake.calls if c[:2] == ["apply", "-f"]].__len__(), 1)  # workloads never applied

    def test_pod_without_sandbox_runtime_fails_closed(self):
        self.fake.pod_runtime_class = ""
        self.assertIn("not using the sandboxed RuntimeClass", self.deploy_and_fail())

    def test_pod_outside_tenant_pool_fails_closed(self):
        self.fake.pod_node = "control-plane-1"
        self.assertIn("not running on a tenant node", self.deploy_and_fail())

    def test_apply_failures_and_not_ready_are_cleaned_up(self):
        for setup, needle in ((lambda: setattr(self.fake, "fail_apply", "namespace"), "namespace"),
                              (lambda: setattr(self.fake, "fail_apply", "workloads"), "workloads"),
                              (lambda: setattr(self.fake, "never_ready", True), "ImagePullBackOff")):
            self.fake.fail_apply, self.fake.never_ready = None, False
            setup()
            with self.subTest(needle=needle):
                self.assertIn(needle, self.deploy_and_fail())

    def test_preflight_rechecked_in_the_worker(self):
        app = self.upload(dep())
        tenant_isolation.preflight(force=True)                     # cached as ready by the request...
        self.fake.handler = "runc"                                 # ...then the cluster changes before the worker runs
        ok, msg = k8s_deploy.deploy(app["id"], self.user["id"], [], 1)
        self.assertFalse(ok)
        self.assertIn("not a sandboxed runtime", msg)

    def test_failed_deploy_app_can_be_deleted_without_cluster_access(self):
        self.fake.admission_enforcing = False
        app = self.upload(dep())
        self.c.post(f"/api/applications/{app['id']}/deploy")
        self.status(app["id"], ("deploy_failed",))
        k8s_deploy.kubectl = lambda *a, **k: (False, "cluster unreachable")
        self.assertEqual(self.c.delete(f"/api/applications/{app['id']}")[0], 200)

    def test_ttl_expiry_tears_down(self):
        app = self.upload(dep())
        self.c.post(f"/api/applications/{app['id']}/deploy")
        self.status(app["id"], ("deployed",))
        with db.session() as conn:
            conn.execute("UPDATE applications SET deployed_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (app["id"],))
        self.assertGreaterEqual(deployments.reap_expired(), 1)
        self.assertNotIn(f"cl-app-{app['id']}", self.fake.namespaces)

    def test_authorization_boundaries(self):
        app = self.upload(dep())
        other = Client(); other.register()
        self.assertEqual(other.post(f"/api/applications/{app['id']}/deploy")[0], 404)
        self.assertEqual(self.fake.namespaces, {})
        self.assertEqual(Client().post(f"/api/applications/{app['id']}/deploy")[0], 401)
        self.c.post(f"/api/applications/{app['id']}/deploy")
        self.status(app["id"], ("deployed",))
        self.assertEqual(other.post(f"/api/applications/{app['id']}/undeploy")[0], 404)
        self.assertEqual(other.delete(f"/api/applications/{app['id']}")[0], 404)
        self.assertIn(f"cl-app-{app['id']}", self.fake.namespaces)

    def test_namespace_isolation_quota_and_network_policy(self):
        a, b = self.upload(dep()), self.upload(dep())
        for app in (a, b):
            self.c.post(f"/api/applications/{app['id']}/deploy")
            self.status(app["id"], ("deployed",))
        for app in (a, b):
            ns = self.fake.namespaces[f"cl-app-{app['id']}"]
            self.assertEqual(ns["labels"]["chaoslab.io/app-id"], str(app["id"]))
            self.assertEqual(ns["labels"]["pod-security.kubernetes.io/enforce"], "restricted")
            docs = {d["kind"]: d for d in ns["docs"]}
            quota = docs["ResourceQuota"]["spec"]["hard"]
            for k in ("pods", "limits.cpu", "limits.memory", "services.loadbalancers", "services.nodeports",
                      "persistentvolumeclaims"):
                self.assertIn(k, quota)
            self.assertEqual(quota["services.nodeports"], "0")
            self.assertIn("max", docs["LimitRange"]["spec"]["limits"][0])
            netpol = docs["NetworkPolicy"]["spec"]
            self.assertEqual(netpol["podSelector"], {})                          # every pod
            self.assertEqual(netpol["ingress"], [{"from": [{"podSelector": {}}]}])  # same namespace only
            self.assertEqual(netpol["egress"][0], {"to": [{"podSelector": {}}]})
            self.assertEqual(netpol["egress"][1]["ports"][0]["port"], 53)          # + DNS, nothing else
            for d in ns["docs"]:
                self.assertEqual(d["metadata"]["namespace"], f"cl-app-{app['id']}")


# ============================================================================ workload policy (code level)
class WorkloadPolicyTests(unittest.TestCase):
    def setUp(self):
        self.saved = configure_isolated()

    def tearDown(self):
        restore(self.saved)

    def rejected(self, mutate, needle):
        d = dep()
        mutate(d)
        r = manifests.analyze("yaml", manifest_bytes(d))
        self.assertFalse(r["valid"], needle)
        self.assertTrue(any(needle in e for e in r["errors"]), (needle, r["errors"]))

    def test_forbidden_pod_and_container_fields(self):
        pod = lambda d: d["spec"]["template"]["spec"]
        c0 = lambda d: pod(d)["containers"][0]
        cases = [
            (lambda d: pod(d).update(hostNetwork=True), "hostNetwork"),
            (lambda d: pod(d).update(hostPID=True), "hostPID"),
            (lambda d: pod(d).update(hostIPC=True), "hostIPC"),
            (lambda d: pod(d).update(volumes=[{"name": "h", "hostPath": {"path": "/"}}]), "hostPath"),
            (lambda d: pod(d).update(serviceAccountName="admin"), "serviceAccountName"),
            (lambda d: pod(d).update(automountServiceAccountToken=True), "automountServiceAccountToken"),
            (lambda d: pod(d).update(initContainers=[{"name": "i", "image": "busybox:1.36"}]), "initContainers"),
            (lambda d: pod(d).update(nodeName="control-plane"), "nodeName"),
            (lambda d: pod(d).update(nodeSelector={"x": "y"}), "nodeSelector"),
            (lambda d: pod(d).update(tolerations=[{"operator": "Exists"}]), "tolerations"),
            (lambda d: pod(d).update(runtimeClassName="runc"), "runtimeClassName"),
            (lambda d: pod(d).update(securityContext={"runAsUser": 0}), "template.spec.securityContext"),
            (lambda d: pod(d).update(terminationGracePeriodSeconds=86400), "terminationGracePeriodSeconds"),
            (lambda d: pod(d).update(volumes=[{"name": "m", "emptyDir": {"medium": "Memory"}}]), "medium"),
            (lambda d: pod(d).update(volumes=[{"name": "m", "emptyDir": {"sizeLimit": "50Gi"}}]), "sizeLimit"),
            (lambda d: c0(d).update(securityContext={"privileged": True}), "privileged"),
            (lambda d: c0(d).update(securityContext={"capabilities": {"add": ["SYS_ADMIN"]}}), "capabilities"),
            (lambda d: c0(d).update(securityContext={"runAsUser": 0}), "non-root"),
            (lambda d: c0(d).update(securityContext={"runAsNonRoot": False}), "runAsNonRoot"),
            (lambda d: c0(d).update(securityContext={"allowPrivilegeEscalation": True}), "allowPrivilegeEscalation"),
            (lambda d: c0(d).update(securityContext={"seccompProfile": {"type": "Unconfined"}}), "seccompProfile"),
            (lambda d: c0(d).update(ports=[{"containerPort": 80, "hostPort": 80}]), "hostPort"),
            (lambda d: c0(d).update(imagePullPolicy="Never"), "imagePullPolicy"),
            (lambda d: c0(d).update(livenessProbe={"exec": {"command": ["sh"]}}), "exec"),
            (lambda d: c0(d).update(resources={"limits": {"cpu": "8"}}), "CPU limit exceeds"),
            (lambda d: c0(d).update(resources={"limits": {"nvidia.com/gpu": "1"}}), "only cpu and memory"),
            (lambda d: c0(d).update(env=[{"name": "T", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]), "secretKeyRef"),
            (lambda d: c0(d).update(image="evil.example.com/miner:1.0"), "not allowed"),
            (lambda d: c0(d).update(image="nginx:latest"), "explicit tag"),
            (lambda d: c0(d).update(image="nginx"), "explicit tag"),
        ]
        for mutate, needle in cases:
            with self.subTest(needle=needle):
                self.rejected(mutate, needle)

    def test_platform_owned_labels_names_and_kinds(self):
        for key in ("chaoslab.io/managed-by", "chaoslab.io/app-id", "pod-security.kubernetes.io/enforce",
                    "node-role.kubernetes.io/control-plane", "foo.k8s.io/x", "kubernetes.io/metadata.name"):
            with self.subTest(label=key):
                d = dep()
                d["metadata"]["labels"] = {key: "x"}
                self.assertTrue(any("reserved" in e for e in manifests.analyze("yaml", manifest_bytes(d))["errors"]))
        d = dep()
        d["metadata"]["labels"] = {"app.kubernetes.io/name": "web"}            # recommended labels stay allowed
        self.assertTrue(manifests.analyze("yaml", manifest_bytes(d))["valid"])
        for name in ("chaoslab-quota", "chaoslab-default", "kube-proxy"):
            with self.subTest(name=name):
                self.assertTrue(any("reserved" in e for e in manifests.analyze("yaml", manifest_bytes(dep(name=name)))["errors"]))
        for kind, api in (("Namespace", "v1"), ("ResourceQuota", "v1"), ("LimitRange", "v1"), ("Secret", "v1"),
                          ("NetworkPolicy", "networking.k8s.io/v1"), ("Pod", "v1"), ("ServiceAccount", "v1"),
                          ("RoleBinding", "rbac.authorization.k8s.io/v1"), ("RuntimeClass", "node.k8s.io/v1"),
                          ("ValidatingAdmissionPolicy", "admissionregistration.k8s.io/v1")):
            with self.subTest(kind=kind):
                r = manifests.analyze("yaml", manifest_bytes(dep(), {"apiVersion": api, "kind": kind,
                                                                     "metadata": {"name": "x"}}))
                self.assertTrue(any(f"kind '{kind}' is not allowed" in e for e in r["errors"]))

    def test_render_forces_platform_settings_over_user_values(self):
        d = dep(image="ghcr.io/acme/web:2.1")
        c = d["spec"]["template"]["spec"]["containers"][0]
        c["securityContext"] = {"runAsUser": 1000, "readOnlyRootFilesystem": True}
        d["metadata"]["labels"] = {"team": "a"}
        r = manifests.analyze("yaml", manifest_bytes(d, svc()))
        self.assertTrue(r["valid"], r["errors"])
        bundle = k8s_deploy.render_bundle(42, 7, r["resources"])
        ns = bundle[0]
        self.assertEqual(ns["metadata"]["name"], "cl-app-42")
        self.assertEqual(ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")
        dep_doc = next(x for x in bundle if x["kind"] == "Deployment")
        self.assertEqual(dep_doc["metadata"]["labels"]["chaoslab.io/app-id"], "42")
        self.assertEqual(dep_doc["metadata"]["labels"]["team"], "a")
        pod = dep_doc["spec"]["template"]["spec"]
        self.assertEqual(pod["runtimeClassName"], "gvisor")
        self.assertEqual(pod["nodeSelector"], {"chaoslab.io/tenant-pool": "true"})
        self.assertEqual(pod["tolerations"][0]["key"], "chaoslab.io/tenant-pool")
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}})
        sc = pod["containers"][0]["securityContext"]
        self.assertEqual(sc, {"runAsUser": 1000, "readOnlyRootFilesystem": True, "runAsNonRoot": True,
                              "allowPrivilegeEscalation": False, "privileged": False,
                              "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}})
        self.assertEqual(pod["containers"][0]["image"], "ghcr.io/acme/web:2.1")
        self.assertEqual(pod["containers"][0]["imagePullPolicy"], "Always")
        self.assertEqual(pod["containers"][0]["resources"]["limits"], {"cpu": "250m", "memory": "256Mi"})
        # requests above the default limit become the limit (never request > limit)
        c["resources"] = {"requests": {"cpu": "500m"}}
        r = manifests.analyze("yaml", manifest_bytes(d))
        res = k8s_deploy.workload_objects(1, 1, r["resources"])[0]["spec"]["template"]["spec"]["containers"][0]["resources"]
        self.assertEqual(res["limits"]["cpu"], "500m")
        self.assertEqual(res["requests"]["cpu"], "500m")

    def test_render_refuses_platform_kinds_even_if_smuggled(self):
        with self.assertRaises(ValueError):
            k8s_deploy.workload_objects(1, 1, [{"kind": "ResourceQuota", "metadata": {"name": "q"}}])

    def test_image_qualification(self):
        q = k8s_deploy.qualified_image
        self.assertEqual(q("nginx:1.27"), "docker.io/library/nginx:1.27")
        self.assertEqual(q("bitnami/redis:7.2"), "docker.io/bitnami/redis:7.2")
        self.assertEqual(q("ghcr.io/a/b@sha256:" + "0" * 64), "ghcr.io/a/b@sha256:" + "0" * 64)


# ============================================================================ admission policy + RBAC artefacts
class PolicyArtefactTests(unittest.TestCase):
    def test_committed_policy_matches_the_renderer(self):
        with open(os.path.join(DEPLOY_DIR, "admission-policies.yaml"), encoding="utf-8") as f:
            committed = list(yaml.safe_load_all(f))
        saved = (config.TENANT_RUNTIME_CLASS, config.TENANT_NODE_LABEL, config.ALLOWED_REGISTRIES)
        config.TENANT_RUNTIME_CLASS, config.TENANT_NODE_LABEL = "", "chaoslab.io/tenant-pool=true"
        try:
            self.assertEqual(committed, tenant_isolation.render_policies())
        finally:
            config.TENANT_RUNTIME_CLASS, config.TENANT_NODE_LABEL, config.ALLOWED_REGISTRIES = saved

    def test_policy_covers_every_control(self):
        docs = tenant_isolation.render_policies("gvisor")
        workload = next(d for d in docs if d["metadata"]["name"] == tenant_isolation.WORKLOAD_POLICY
                        and d["kind"] == "ValidatingAdmissionPolicy")
        text = " ".join(v["expression"] for v in workload["spec"]["validations"])
        for needle in ("runtimeClassName == 'gvisor'", "nodeSelector", "hostNetwork", "hostPID", "hostIPC",
                       "emptyDir", "automountServiceAccountToken == false", "initContainers", "ephemeralContainers",
                       "RuntimeDefault", "runAsNonRoot == true", "runAsUser != 0", "allowPrivilegeEscalation == false",
                       "privileged == false", "'ALL' in c.securityContext.capabilities.drop", "capabilities.add",
                       "'cpu' in c.resources.limits", "'memory' in c.resources.limits", "hostPort", "startsWith",
                       "'docker.io/'", "sysctls"):
            self.assertIn(needle, text)
        self.assertEqual(workload["spec"]["failurePolicy"], "Fail")
        kinds = {r for rule in workload["spec"]["matchConstraints"]["resourceRules"] for r in rule["resources"]}
        self.assertEqual(kinds, {"pods", "deployments", "replicasets"})
        bindings = [d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding"]
        self.assertEqual(len(bindings), 2)
        for b in bindings:
            self.assertEqual(b["spec"]["validationActions"], ["Deny"])
        scope = next(d for d in docs if d["metadata"]["name"] == tenant_isolation.SCOPE_POLICY
                     and d["kind"] == "ValidatingAdmissionPolicy")
        self.assertIn(tenant_isolation.DEPLOYER_USER, scope["spec"]["matchConditions"][0]["expression"])
        self.assertIn("cl-app-", " ".join(v["expression"] for v in scope["spec"]["validations"]))

    def test_policy_digest_tracks_configuration(self):
        saved = (config.TENANT_RUNTIME_CLASS, config.ALLOWED_REGISTRIES)
        try:
            config.TENANT_RUNTIME_CLASS = "gvisor"
            a = tenant_isolation.policy_digest()
            config.ALLOWED_REGISTRIES = ("docker.io", "evil.example.com")
            self.assertNotEqual(a, tenant_isolation.policy_digest())
        finally:
            config.TENANT_RUNTIME_CLASS, config.ALLOWED_REGISTRIES = saved

    def test_deployer_rbac_is_least_privilege(self):
        with open(os.path.join(DEPLOY_DIR, "deployer-rbac.yaml"), encoding="utf-8") as f:
            docs = list(yaml.safe_load_all(f))
        role = next(d for d in docs if d["kind"] == "ClusterRole")
        for rule in role["rules"]:
            self.assertNotIn("*", rule["verbs"])
            self.assertNotIn("*", rule["resources"])
            self.assertNotIn("*", rule["apiGroups"])
            for r in ("secrets", "pods/exec", "pods/attach", "pods/portforward", "pods/log", "serviceaccounts",
                      "roles", "rolebindings", "clusterroles", "clusterrolebindings", "nodes/proxy"):
                self.assertNotIn(r, rule["resources"])
            if "nodes" in rule["resources"] or "runtimeclasses" in rule["resources"] \
                    or "validatingadmissionpolicies" in rule["resources"]:
                self.assertTrue(set(rule["verbs"]) <= {"get", "list"}, rule)
            if "pods" in rule["resources"]:
                self.assertTrue(set(rule["verbs"]) <= {"get", "list", "watch"}, rule)
        sa = next(d for d in docs if d["kind"] == "ServiceAccount")
        self.assertIs(sa["automountServiceAccountToken"], False)


if __name__ == "__main__":
    unittest.main()
