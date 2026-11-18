"""Multi-app support and cluster service discovery. kubectl output is faked, so no cluster is needed."""

import unittest

from harness import Client
from test_experiments import demo_app_id, run_sim, wait_done


def _fake_cluster(deployments):
    """Context-free helper: swap catalog's cluster query + enable discovery; returns an undo function."""
    from webapp import catalog
    orig_q, orig_en = catalog._query_cluster, catalog.config.DISCOVERY_ENABLED
    catalog._query_cluster = deployments if callable(deployments) else (lambda: deployments)
    catalog.config.DISCOVERY_ENABLED = True
    catalog._cache.update(at=0.0, data=None)

    def undo():
        catalog._query_cluster, catalog.config.DISCOVERY_ENABLED = orig_q, orig_en
        catalog._cache.update(at=0.0, data=None)
    return undo


class MultiAppDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.c = Client()
        self.c.register()

    def test_each_builtin_app_runs_simulated_with_its_own_namespace_and_probes(self):
        cases = [("learning-platform", "course-service", "learning-platform", "http"),
                 ("project-platform", "task-service", "project-platform", "http"),
                 ("online-boutique", "cartservice", "default", "grpc")]
        for slug, service, ns, probe in cases:
            exp = wait_done(self.c, run_sim(self.c, service, "pod-kill", 30, app_id=demo_app_id(self.c, slug)))
            self.assertEqual(exp["status"], "completed", exp)
            self.assertEqual(exp["result"]["target"]["namespace"], ns)
            self.assertEqual(exp["engine_config"]["target"]["namespace"], ns)
            self.assertEqual(exp["engine_config"]["application"]["health_probe"]["type"], probe)

    def test_service_must_belong_to_selected_app(self):
        status, data, _ = self.c.post("/api/experiments", {
            "application_id": demo_app_id(self.c, "learning-platform"), "target_service": "paymentservice",
            "fault_type": "pod-kill", "duration_seconds": 30, "mode": "simulated"})
        self.assertEqual(status, 400)
        self.assertIn("course-service", data["error"])

    def test_discovery_reads_cluster_and_marks_non_targetable(self):
        undo = _fake_cluster({"learning-platform": [
            {"deployment": "course-service", "app_label": "course-service", "replicas": 2, "ready": 1},
            {"deployment": "new-service", "app_label": "new-service", "replicas": 1, "ready": 1},
            {"deployment": "lms-db-postgres", "app_label": "lms-db-postgres", "replicas": 1, "ready": 1},
            {"deployment": "nolabel", "app_label": None, "replicas": 1, "ready": 1}]})
        try:
            app_id = demo_app_id(self.c, "learning-platform")
            cat = self.c.get(f"/api/applications/{app_id}?refresh=1")[1]["application"]["catalog"]
            self.assertEqual(cat["discovery"]["source"], "cluster")
            names = {s["name"]: s for s in cat["services"]}
            # discovered, not hard-coded: unlabeled skipped, brand-new service present, static-only services absent
            self.assertEqual(set(names), {"course-service", "new-service", "lms-db-postgres"})
            self.assertEqual((names["course-service"]["ready"], names["course-service"]["replicas"]), (1, 2))
            self.assertEqual(names["course-service"]["language"], "Python")      # static metadata merged in
            self.assertEqual(names["new-service"]["language"], "unknown")        # genuinely new service
            self.assertFalse(names["lms-db-postgres"]["targetable"])
            self.assertEqual(cat["targetable_services"], ["course-service", "new-service"])

            ok = self.c.post("/api/experiments", {"application_id": app_id, "target_service": "new-service",
                             "fault_type": "pod-kill", "duration_seconds": 30, "mode": "simulated"})
            self.assertEqual(ok[0], 201)
            wait_done(self.c, ok[1]["experiment"]["id"])
            bad = self.c.post("/api/experiments", {"application_id": app_id, "target_service": "lms-db-postgres",
                              "fault_type": "pod-kill", "duration_seconds": 30, "mode": "simulated"})
            self.assertEqual(bad[0], 400)
        finally:
            undo()

    def test_static_fallback_when_namespace_missing_and_live_refused(self):
        from webapp.live_backend import _capability_cache
        _capability_cache.update(at=9e12, value={"available": True, "reason": "forced for test"})
        undo = _fake_cluster({"default": []})          # project-platform namespace absent
        try:
            app_id = demo_app_id(self.c, "project-platform")
            cat = self.c.get(f"/api/applications/{app_id}?refresh=1")[1]["application"]["catalog"]
            self.assertEqual(cat["discovery"]["source"], "static")
            self.assertIn("No deployments found", cat["discovery"]["error"])
            status, data, _ = self.c.post("/api/experiments", {
                "application_id": app_id, "target_service": "task-service", "fault_type": "pod-kill",
                "duration_seconds": 30, "mode": "live"})
            self.assertEqual(status, 503)
            self.assertIn("not found in the cluster", data["error"])
        finally:
            undo()
            _capability_cache.update(at=0.0, value=None)

    def test_cluster_failure_falls_back_to_static(self):
        from webapp import catalog

        def boom():
            raise RuntimeError("connection refused")
        undo = _fake_cluster(boom)
        try:
            d = catalog.discover_services(catalog.BUILTIN_APPS[0], force=True)
            self.assertEqual(d["source"], "static")
            self.assertEqual(d["error"], "connection refused")
            self.assertEqual(len(d["services"]), 11)
        finally:
            undo()


if __name__ == "__main__":
    unittest.main()
