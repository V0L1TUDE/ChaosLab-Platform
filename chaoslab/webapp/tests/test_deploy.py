"""Deployment lifecycle of uploaded manifest apps, against a fake cluster (no kubectl needed)."""

import json
import time
import unittest

import yaml

from harness import Client
from fake_tenant import FakeTenantCluster, configure_isolated, restore
from test_experiments import wait_done
from test_manifests import dep, svc


FakeCluster = FakeTenantCluster       # kept for older imports


def manifest_bytes(*docs):
    return "\n---\n".join(yaml.safe_dump(d) for d in docs).encode()


class DeployTests(unittest.TestCase):
    def setUp(self):
        from webapp import catalog, config, deployments, k8s_deploy
        from webapp.live_backend import _capability_cache
        from webapp import db
        import harness
        harness.start()                  # initialises the DB when this module runs on its own
        with db.session() as conn:       # the test DB is shared: drop deployments left by earlier tests (global cap)
            conn.execute("UPDATE applications SET status = 'validated', namespace = NULL, deployed_at = NULL "
                         "WHERE status IN ('deploying', 'deployed', 'deploy_failed')")
        self.fake = FakeTenantCluster()
        self.saved = (k8s_deploy.kubectl, catalog._query_cluster, config.DISCOVERY_ENABLED,
                      config.DEPLOY_TIMEOUT_SECONDS, config.MAX_DEPLOYED_PER_USER)
        self.saved_tenant = configure_isolated()
        k8s_deploy.kubectl = self.fake.kubectl
        catalog._query_cluster = self.fake.query_cluster
        config.DISCOVERY_ENABLED, config.DEPLOY_TIMEOUT_SECONDS = True, 1
        catalog._cache.update(at=0.0, data=None)
        _capability_cache.update(at=9e12, value={"available": True, "reason": "fake cluster"})
        self.c = Client()
        self.user, *_ = self.c.register()

    def tearDown(self):
        from webapp import catalog, config, k8s_deploy
        from webapp.live_backend import _capability_cache
        (k8s_deploy.kubectl, catalog._query_cluster, config.DISCOVERY_ENABLED,
         config.DEPLOY_TIMEOUT_SECONDS, config.MAX_DEPLOYED_PER_USER) = self.saved
        restore(self.saved_tenant)
        catalog._cache.update(at=0.0, data=None)
        _capability_cache.update(at=0.0, value=None)

    # helpers -----------------------------------------------------------------------------
    def upload(self, *docs, name="My App", client=None):
        c = client or self.c
        status, data, _ = c.upload("/api/applications", {"name": name}, "app.yaml", manifest_bytes(*docs))
        self.assertEqual(status, 201, data)
        return data["application"]

    def wait_status(self, app_id, wanted, client=None, timeout=15):
        c = client or self.c
        end = time.time() + timeout
        while time.time() < end:
            a = c.get(f"/api/applications/{app_id}")[1]["application"]
            if a["status"] in wanted:
                return a
            time.sleep(0.1)
        self.fail(f"app {app_id} never reached {wanted}; last={a['status']}")

    def deploy(self, app_id, client=None):
        return (client or self.c).post(f"/api/applications/{app_id}/deploy")

    # tests -------------------------------------------------------------------------------
    def test_upload_validation_states(self):
        ok = self.upload(dep(), svc())
        self.assertEqual(ok["status"], "validated")
        self.assertTrue(ok["deployable"] and ok["executable"])
        self.assertEqual(ok["validation"]["errors"], [])

        bad = dep(); bad["spec"]["template"]["spec"]["hostNetwork"] = True
        invalid = self.upload(bad, name="Bad")
        self.assertEqual(invalid["status"], "invalid")
        self.assertTrue(any("hostNetwork" in e for e in invalid["validation"]["errors"]))
        self.assertFalse(invalid["deployable"] or invalid["executable"])
        self.assertEqual(self.deploy(invalid["id"])[0], 409)          # invalid can never be deployed

        s, d, _ = self.c.upload("/api/applications", {"name": "Notes"}, "notes.txt", b"hello")
        self.assertEqual(d["application"]["status"], "registered")    # not a manifest: stored only
        self.assertEqual(self.deploy(d["application"]["id"])[0], 409)

    def test_deploy_builds_hardened_sandbox_namespace(self):
        app = self.upload(dep(), svc())
        status, data, _ = self.deploy(app["id"])
        self.assertEqual(status, 202, data)
        detail = self.wait_status(app["id"], ("deployed",))
        ns = f"cl-app-{app['id']}"
        self.assertEqual(detail["deployment"]["namespace"], ns)
        self.assertTrue(detail["deployment"]["expires_at"])
        cluster_ns = self.fake.namespaces[ns]
        labels = cluster_ns["labels"]
        self.assertEqual(labels["chaoslab.io/managed-by"], "chaoslab-online")
        self.assertEqual(labels["chaoslab.io/app-id"], str(app["id"]))
        self.assertEqual(labels["pod-security.kubernetes.io/enforce"], "restricted")
        kinds = [d["kind"] for d in cluster_ns["docs"]]
        for needed in ("ResourceQuota", "LimitRange", "NetworkPolicy", "Deployment", "Service"):
            self.assertIn(needed, kinds)
        deployment = next(d for d in cluster_ns["docs"] if d["kind"] == "Deployment")
        self.assertEqual(deployment["metadata"]["namespace"], ns)
        pod = deployment["spec"]["template"]["spec"]
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertIs(pod["enableServiceLinks"], False)
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertIs(pod["containers"][0]["securityContext"]["allowPrivilegeEscalation"], False)
        self.assertEqual(pod["runtimeClassName"], "gvisor")
        self.assertEqual(pod["nodeSelector"], {"chaoslab.io/tenant-pool": "true"})
        self.assertEqual(pod["containers"][0]["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertIs(pod["containers"][0]["securityContext"]["runAsNonRoot"], True)
        self.assertEqual(pod["containers"][0]["image"], "docker.io/library/nginx:1.27-alpine")
        self.assertEqual(set(pod["containers"][0]["resources"]["limits"]), {"cpu", "memory"})
        # the admission canary ran (and was denied) before the workloads were applied
        dry = [i for i, c in enumerate(self.fake.calls) if "--dry-run=server" in c]
        applies = [i for i, c in enumerate(self.fake.calls) if c[:2] == ["apply", "-f"]]
        self.assertTrue(dry and applies[0] < dry[0] < applies[1])
        netpol = next(d for d in cluster_ns["docs"] if d["kind"] == "NetworkPolicy")
        self.assertEqual(netpol["spec"]["policyTypes"], ["Ingress", "Egress"])
        # discovery now sees it from the cluster
        cat = self.c.get(f"/api/applications/{app['id']}?refresh=1")[1]["application"]["catalog"]
        self.assertEqual(cat["discovery"]["source"], "cluster")
        self.assertEqual(cat["targetable_services"], ["web"])

    def test_failed_deploy_is_cleaned_up_with_diagnostics_and_retryable(self):
        app = self.upload(dep())
        self.fake.never_ready = True
        self.deploy(app["id"])
        detail = self.wait_status(app["id"], ("deploy_failed",))
        self.assertIn("ImagePullBackOff", detail["deployment"]["error"])
        self.assertNotIn(f"cl-app-{app['id']}", self.fake.namespaces)    # no leftovers
        self.fake.never_ready = False
        self.assertEqual(self.deploy(app["id"])[0], 202)                  # retry works
        self.wait_status(app["id"], ("deployed",))

    def test_ownership(self):
        app = self.upload(dep())
        other = Client(); other.register()
        self.assertEqual(self.deploy(app["id"], other)[0], 404)
        self.assertEqual(other.post(f"/api/applications/{app['id']}/undeploy")[0], 404)
        self.deploy(app["id"]); self.wait_status(app["id"], ("deployed",))
        self.assertEqual(other.post(f"/api/applications/{app['id']}/undeploy")[0], 404)
        self.assertIn(f"cl-app-{app['id']}", self.fake.namespaces)        # untouched

    def test_cluster_unavailable_or_disabled(self):
        from webapp import config, tenant_isolation
        app = self.upload(dep())
        self.fake.runtime_class = False                       # the sandbox runtime disappeared
        tenant_isolation.reset_cache()
        s, d, _ = self.deploy(app["id"])
        self.assertEqual((s, "isolated tenant runtime" in d["error"], "RuntimeClass" in d["error"]), (503, True, True))
        self.fake.runtime_class = True
        tenant_isolation.reset_cache()
        config.DEPLOY_ENABLED = False
        self.assertEqual(self.deploy(app["id"])[0], 503)
        self.assertEqual(self.fake.namespaces, {})

    def test_per_user_limit(self):
        from webapp import config
        config.MAX_DEPLOYED_PER_USER = 1
        a, b = self.upload(dep(), name="A"), self.upload(dep(), name="B")
        self.deploy(a["id"]); self.wait_status(a["id"], ("deployed",))
        self.assertEqual(self.deploy(b["id"])[0], 429)

    def test_undeploy_and_delete_clean_the_namespace(self):
        app = self.upload(dep())
        self.deploy(app["id"]); self.wait_status(app["id"], ("deployed",))
        ns = f"cl-app-{app['id']}"
        self.assertEqual(self.c.post(f"/api/applications/{app['id']}/undeploy")[0], 200)
        self.assertNotIn(ns, self.fake.namespaces)
        self.assertEqual(self.wait_status(app["id"], ("validated",))["deployment"]["namespace"], None)
        # redeploy, then delete the whole app: namespace must go too
        self.deploy(app["id"]); self.wait_status(app["id"], ("deployed",))
        self.assertEqual(self.c.delete(f"/api/applications/{app['id']}")[0], 200)
        self.assertNotIn(ns, self.fake.namespaces)

    def test_refuses_to_delete_namespace_not_managed_by_chaoslab(self):
        from webapp import k8s_deploy
        self.fake.namespaces["cl-app-777"] = {"labels": {"team": "prod"}, "docs": []}      # wrong labels
        ok, msg = k8s_deploy.undeploy(777)
        self.assertFalse(ok)
        self.assertIn("Refusing", msg)
        self.fake.namespaces["cl-app-778"] = {"labels": {"chaoslab.io/managed-by": "chaoslab-online",
                                                         "chaoslab.io/app-id": "1"}, "docs": []}   # other app's id
        self.assertFalse(k8s_deploy.undeploy(778)[0])
        self.assertIn("cl-app-777", self.fake.namespaces)
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["delete", "namespace"]])

    def test_undeploy_blocked_while_experiment_running(self):
        from webapp import db
        app = self.upload(dep())
        self.deploy(app["id"]); self.wait_status(app["id"], ("deployed",))
        with db.session() as conn:
            conn.execute("INSERT INTO experiments (user_id, application_id, target_service, fault_type, duration_seconds, "
                         "config_json, mode, status, created_at) VALUES (?, ?, 'web', 'pod-kill', 30, '{}', 'live', 'running', ?)",
                         (self.user["id"], app["id"], db.utcnow()))
        self.assertEqual(self.c.post(f"/api/applications/{app['id']}/undeploy")[0], 409)
        self.assertEqual(self.c.delete(f"/api/applications/{app['id']}")[0], 409)
        self.assertIn(f"cl-app-{app['id']}", self.fake.namespaces)

    def test_ttl_reaper_undeploys_expired_apps_only(self):
        from webapp import db, deployments
        old, fresh = self.upload(dep(), name="Old"), self.upload(dep(), name="Fresh")
        for a in (old, fresh):
            self.deploy(a["id"]); self.wait_status(a["id"], ("deployed",))
        with db.session() as conn:
            conn.execute("UPDATE applications SET deployed_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (old["id"],))
        self.assertEqual(deployments.reap_expired(), 1)
        self.assertNotIn(f"cl-app-{old['id']}", self.fake.namespaces)
        self.assertIn(f"cl-app-{fresh['id']}", self.fake.namespaces)
        self.assertEqual(self.wait_status(old["id"], ("validated",))["status"], "validated")

    def test_recover_interrupted_deployments(self):
        from webapp import db, deployments
        app = self.upload(dep())
        with db.session() as conn:
            conn.execute("UPDATE applications SET status = 'deploying' WHERE id = ?", (app["id"],))
        self.assertEqual(deployments.recover_interrupted(), 1)
        a = self.c.get(f"/api/applications/{app['id']}")[1]["application"]
        self.assertEqual(a["status"], "deploy_failed")
        self.assertIn("restarted", a["deployment"]["error"])

    def test_experiments_on_uploaded_apps(self):
        from webapp import runner_service
        from webapp.execution import ExecutionBackend, ExecutionError
        app = self.upload(dep(), svc())
        base = {"application_id": app["id"], "target_service": "web", "fault_type": "pod-kill", "duration_seconds": 30}

        # Simulated works as soon as the manifests are validated (clearly labelled SIMULATED)
        s, d, _ = self.c.post("/api/experiments", {**base, "mode": "simulated"})
        self.assertEqual(s, 201, d)
        exp = wait_done(self.c, d["experiment"]["id"])
        self.assertTrue(exp["simulated"])
        self.assertEqual(exp["engine_config"]["target"]["namespace"], f"cl-app-{app['id']}")

        # Live needs a running deployment
        s, d, _ = self.c.post("/api/experiments", {**base, "mode": "live"})
        self.assertEqual(s, 409)
        self.assertIn("Deploy the application first", d["error"])

        self.deploy(app["id"]); self.wait_status(app["id"], ("deployed",))
        seen = {}

        class Capture(ExecutionBackend):
            mode = "live"

            def run(self, spec, sink):
                seen.update(spec)
                raise ExecutionError("captured")
        original = runner_service.BACKENDS["live"]
        runner_service.BACKENDS["live"] = Capture()
        try:
            s, d, _ = self.c.post("/api/experiments", {**base, "mode": "live"})
            self.assertEqual(s, 201, d)
            wait_done(self.c, d["experiment"]["id"])
        finally:
            runner_service.BACKENDS["live"] = original
        cfg = seen["engine_config"]
        self.assertEqual(cfg["target"], {"namespace": f"cl-app-{app['id']}", "label": {"app": "web"}})
        self.assertEqual(cfg["fault"]["type"], "pod-kill")
        self.assertEqual(cfg["application"], {"enabled": False})       # honest: no deep probes for uploaded apps

        # service must belong to this app
        s, d, _ = self.c.post("/api/experiments", {**base, "target_service": "paymentservice", "mode": "simulated"})
        self.assertEqual(s, 400)
        # another user cannot run experiments on it
        other = Client(); other.register()
        self.assertEqual(other.post("/api/experiments", {**base, "mode": "simulated"})[0], 404)


if __name__ == "__main__":
    unittest.main()
