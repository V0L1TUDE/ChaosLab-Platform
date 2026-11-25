"""Strict-sandbox manifest validation (pure functions, no cluster)."""

import copy
import io
import unittest
import zipfile

import yaml

import harness  # noqa: F401  (sets isolated env and sys.path before webapp.config is used)

from webapp import manifests  # noqa: E402


def dep(name="web", image="nginx:1.27-alpine", **over):
    d = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                  "template": {"metadata": {"labels": {"app": name}},
                               "spec": {"containers": [{"name": "c", "image": image, "ports": [{"containerPort": 80}]}]}}}}
    for k, v in over.items():
        d[k] = v
    return d


def svc(name="web", selector=None, **spec):
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
            "spec": {"selector": selector or {"app": name}, "ports": [{"port": 80, "targetPort": 80}], **spec}}


def run(*docs, ext="yaml"):
    text = "\n---\n".join(yaml.safe_dump(d) for d in docs)
    return manifests.analyze(ext, text.encode())


def errs(*docs):
    r = run(*docs)
    return r["errors"]


class ValidManifestTests(unittest.TestCase):
    def test_minimal_valid_bundle(self):
        r = run(dep(), svc())
        self.assertTrue(r["valid"], r["errors"])
        self.assertEqual(r["workloads"], [{"name": "web", "app": "web", "replicas": 1,
                                           "images": ["nginx:1.27-alpine"], "ports": [80]}])
        self.assertEqual([d["kind"] for d in r["resources"]], ["Deployment", "Service"])

    def test_app_label_is_auto_added_with_warning(self):
        d = dep()
        d["spec"]["selector"]["matchLabels"] = {"tier": "x"}
        d["spec"]["template"]["metadata"]["labels"] = {"tier": "x"}
        r = run(d)
        self.assertTrue(r["valid"], r["errors"])
        self.assertEqual(r["resources"][0]["spec"]["selector"]["matchLabels"]["app"], "web")
        self.assertTrue(any("app=web" in w for w in r["warnings"]))

    def test_namespace_is_stripped_with_warning(self):
        d = dep()
        d["metadata"]["namespace"] = "kube-system"
        r = run(d)
        self.assertTrue(r["valid"])
        self.assertNotIn("namespace", r["resources"][0]["metadata"])
        self.assertTrue(any("kube-system" in w for w in r["warnings"]))

    def test_configmap_env_and_volume_and_digest_image(self):
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cfg"}, "data": {"A": "1"}}
        d = dep(image="ghcr.io/org/app@sha256:" + "a" * 64)
        c = d["spec"]["template"]["spec"]["containers"][0]
        c["env"] = [{"name": "A", "valueFrom": {"configMapKeyRef": {"name": "cfg", "key": "A"}}}]
        c["volumeMounts"] = [{"name": "v", "mountPath": "/etc/cfg"}, {"name": "tmp", "mountPath": "/tmp"}]
        d["spec"]["template"]["spec"]["volumes"] = [{"name": "v", "configMap": {"name": "cfg"}}, {"name": "tmp", "emptyDir": {}}]
        r = run(cm, d)
        self.assertTrue(r["valid"], r["errors"])

    def test_zip_bundle(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("k8s/deploy.yaml", yaml.safe_dump(dep()))
            zf.writestr("k8s/svc.yml", yaml.safe_dump(svc()))
            zf.writestr("README.md", "ignored")
        r = manifests.analyze("zip", buf.getvalue())
        self.assertTrue(r["valid"], r["errors"])
        self.assertEqual(len(r["resources"]), 2)


class RejectionTests(unittest.TestCase):
    def assertRejected(self, docs, fragment):
        e = errs(*docs)
        self.assertTrue(any(fragment in x for x in e), (fragment, e))

    def test_disallowed_kinds(self):
        for kind, api in [("Secret", "v1"), ("Pod", "v1"), ("DaemonSet", "apps/v1"), ("ClusterRole", "rbac.authorization.k8s.io/v1"),
                          ("Namespace", "v1"), ("Ingress", "networking.k8s.io/v1"), ("PersistentVolumeClaim", "v1"),
                          ("ServiceAccount", "v1"), ("NetworkPolicy", "networking.k8s.io/v1")]:
            self.assertRejected([{"apiVersion": api, "kind": kind, "metadata": {"name": "x"}}], f"kind '{kind}' is not allowed")

    def test_privileged_and_host_access(self):
        d = dep()
        pod = d["spec"]["template"]["spec"]
        pod["containers"][0]["securityContext"] = {"privileged": True}
        self.assertRejected([d], "securityContext.privileged: field is not allowed")
        d = dep(); d["spec"]["template"]["spec"]["hostNetwork"] = True
        self.assertRejected([d], "hostNetwork: field is not allowed")
        d = dep(); d["spec"]["template"]["spec"]["hostPID"] = True
        self.assertRejected([d], "hostPID")
        d = dep(); d["spec"]["template"]["spec"]["volumes"] = [{"name": "h", "hostPath": {"path": "/"}}]
        self.assertRejected([d], "hostPath: field is not allowed")
        d = dep(); d["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"capabilities": {"add": ["SYS_ADMIN"]}}
        self.assertRejected([d], "capabilities: field is not allowed")
        d = dep(); d["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"allowPrivilegeEscalation": True}
        self.assertRejected([d], "allowPrivilegeEscalation: must be false")
        d = dep(); d["spec"]["template"]["spec"]["serviceAccountName"] = "admin"
        self.assertRejected([d], "serviceAccountName: field is not allowed")
        d = dep(); d["spec"]["template"]["spec"]["initContainers"] = [{"name": "i", "image": "busybox:1"}]
        self.assertRejected([d], "initContainers: field is not allowed")

    def test_volume_must_be_emptydir_or_configmap(self):
        d = dep(); d["spec"]["template"]["spec"]["volumes"] = [{"name": "s", "secret": {"secretName": "x"}}]
        self.assertRejected([d], "secret: field is not allowed")
        d = dep(); d["spec"]["template"]["spec"]["volumes"] = [{"name": "s"}]
        self.assertRejected([d], "exactly one of emptyDir or configMap")

    def test_images(self):
        self.assertRejected([dep(image="nginx")], "explicit tag")
        self.assertRejected([dep(image="nginx:latest")], "explicit tag")
        self.assertRejected([dep(image="evil.example.com/miner:1.0")], "registry 'evil.example.com' is not allowed")
        self.assertRejected([dep(image="localhost:5000/app:1")], "registry 'localhost:5000' is not allowed")
        self.assertRejected([dep(image="")], "image must be")
        self.assertEqual(errs(dep(image="docker.io/library/nginx:1.27")), [])
        self.assertEqual(errs(dep(image="index.docker.io/library/nginx:1.27")), [])

    def test_resource_and_replica_limits(self):
        d = dep(); d["spec"]["replicas"] = 50
        self.assertRejected([d], "replicas: must be between")
        d = dep(); d["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"cpu": "8", "memory": "64Gi"}}
        e = errs(d)
        self.assertTrue(any("CPU limit exceeds" in x for x in e) and any("memory limit exceeds" in x for x in e), e)
        d = dep(); d["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"nvidia.com/gpu": 1}}
        self.assertRejected([d], "only cpu and memory")
        d = dep(); d["spec"]["template"]["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "500m"}, "limits": {"cpu": "100m"}}
        self.assertRejected([d], "request is larger than its limit")

    def test_services(self):
        self.assertRejected([dep(), svc(type="NodePort")], "only ClusterIP")
        self.assertRejected([dep(), svc(type="LoadBalancer")], "only ClusterIP")
        self.assertRejected([dep(), svc(externalIPs=["1.2.3.4"])], "externalIPs: field is not allowed")
        bad = svc(); bad["spec"].pop("selector")
        self.assertRejected([dep(), bad], "selector: required")
        r = run(dep(), svc(selector={"app": "nothing"}))
        self.assertTrue(r["valid"])
        self.assertTrue(any("matches no Deployment" in w for w in r["warnings"]))

    def test_structural(self):
        self.assertRejected([svc()], "at least one Deployment")
        self.assertRejected([dep(), dep()], "defined more than once")
        a, b = dep("a"), dep("b")
        b["spec"]["selector"]["matchLabels"] = {"app": "a"}
        b["spec"]["template"]["metadata"]["labels"] = {"app": "a"}
        self.assertRejected([a, b], "unique 'app' label")
        d = dep(); d["spec"]["selector"]["matchLabels"] = {"app": "other"}
        self.assertRejected([d], "must match the pod template labels")
        d = dep("Bad_Name")
        self.assertRejected([d], "DNS label")
        d = dep(); d["metadata"]["labels"] = {"chaoslab.io/managed-by": "x"}
        self.assertRejected([d], "reserved prefix")
        d = dep(); d["apiVersion"] = "apps/v1beta1"
        self.assertRejected([d], "apiVersion apps/v1")
        d = dep(); d["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "A", "value": 5}]
        self.assertRejected([d], "must be a string")
        d = dep(); d["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "A", "valueFrom": {"configMapKeyRef": {"name": "nope", "key": "k"}}}]
        self.assertRejected([d], "not part of this upload")

    def test_yaml_attacks_and_garbage(self):
        bomb = "a: &a [x]\nb: &b [*a,*a]\nc: &c [*b,*b]\nd: *c\n"
        r = manifests.analyze("yaml", bomb.encode())
        self.assertFalse(r["valid"])
        self.assertTrue(any("aliases" in e for e in r["errors"]))
        for bad in (b"a: [unclosed", b"\xff\xfe\x00bad", b"just text", b"- 1\n- 2\n", b""):
            self.assertFalse(manifests.analyze("yaml", bad)["valid"])
        # arbitrary python object tags must not execute / construct
        self.assertFalse(manifests.analyze("yaml", b"!!python/object/apply:os.system ['echo hi']\n")["valid"])
        self.assertFalse(manifests.analyze("yaml", b"x" * (manifests.MAX_FILE_BYTES + 1))["valid"])

    def test_plain_non_k8s_yaml_is_invalid(self):
        r = manifests.analyze("yaml", b"name: hello\nversion: 1\n")
        self.assertFalse(r["valid"])
        self.assertTrue(any("not allowed" in e or "No Kubernetes" in e for e in r["errors"]))

    def test_zip_limits(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(manifests.MAX_FILES + 1):
                zf.writestr(f"f{i}.yaml", "a: 1")
        self.assertTrue(any("Too many manifest files" in e for e in manifests.analyze("zip", buf.getvalue())["errors"]))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.yaml", "a: " + "b" * (manifests.MAX_FILE_BYTES + 10))   # compresses tiny, expands large
        self.assertTrue(any("larger than" in e for e in manifests.analyze("zip", buf.getvalue())["errors"]))

    def test_input_is_not_mutated_between_runs(self):
        d = dep()
        before = copy.deepcopy(d)
        run(d)
        self.assertEqual(d, before)


if __name__ == "__main__":
    unittest.main()
