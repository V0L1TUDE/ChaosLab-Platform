"""Default behaviour: NO built-in demo applications are visible to any user (CHAOSLAB_SHOW_DEMO_APPS off)."""

import unittest

from harness import Client
from test_clusters import connect, register_app, start_experiment


class NoDemoAppsTests(unittest.TestCase):
    def setUp(self):
        from webapp import config
        self.config, self.saved = config, config.SHOW_DEMO_APPS
        config.SHOW_DEMO_APPS = False          # the production default; the rest of the suite turns demos on
        self.c = Client()
        self.c.register()

    def tearDown(self):
        self.config.SHOW_DEMO_APPS = self.saved

    def demo_id(self):
        """Look the demo up directly in the DB (it is hidden from the API)."""
        from webapp import db
        with db.session() as conn:
            return conn.execute("SELECT id FROM applications WHERE slug = 'online-boutique'").fetchone()[0]

    def test_a_new_user_sees_no_applications(self):
        self.assertEqual(self.c.get("/api/applications")[1]["applications"], [])
        d = self.c.get("/api/dashboard")[1]
        self.assertEqual(d["totals"]["applications"], 0)
        self.assertEqual(d["applications"], [])
        self.assertEqual(self.c.get("/api/clusters")[1]["clusters"], [])

    def test_demo_apps_cannot_be_reached_even_by_id(self):
        app_id = self.demo_id()
        self.assertEqual(self.c.get(f"/api/applications/{app_id}")[0], 404)
        self.assertEqual(self.c.delete(f"/api/applications/{app_id}")[0], 404)
        self.assertEqual(self.c.post(f"/api/applications/{app_id}/deploy")[0], 404)
        self.assertEqual(self.c.post(f"/api/applications/{app_id}/probes", {"service": "x", "probes": None})[0], 404)
        status, data, _ = self.c.post("/api/experiments", {"application_id": app_id, "target_service": "paymentservice",
                                                           "fault_type": "pod-kill", "duration_seconds": 30, "mode": "simulated"})
        self.assertEqual(status, 404)
        status, data, _ = self.c.post("/api/experiments", {"application_id": app_id, "target_service": "paymentservice",
                                                           "fault_type": "pod-kill", "duration_seconds": 30, "mode": "live"})
        self.assertEqual(status, 404)                      # no way to fault the shared demo cluster

    def test_own_applications_still_work(self):
        s, d, _ = self.c.upload("/api/applications", {"name": "Mine"}, "notes.txt", b"hello")
        self.assertEqual(s, 201)
        apps = self.c.get("/api/applications")[1]["applications"]
        self.assertEqual([a["name"] for a in apps], ["Mine"])
        self.assertFalse(any(a["is_builtin"] for a in apps))
        self.assertEqual(self.c.get("/api/dashboard")[1]["totals"]["applications"], 1)

    def test_connected_cluster_apps_are_unaffected(self):
        agent = connect(self.c)
        app_id = register_app(self.c, agent)
        apps = self.c.get("/api/applications")[1]["applications"]
        self.assertEqual([a["name"] for a in apps], ["Shop"])
        self.assertEqual(start_experiment(self.c, app_id, mode="simulated")[0], 201)

    def test_switching_demos_on_restores_them(self):
        self.config.SHOW_DEMO_APPS = True
        apps = self.c.get("/api/applications")[1]["applications"]
        self.assertEqual([a["slug"] for a in apps], ["online-boutique", "learning-platform", "project-platform"])

    def test_default_is_off(self):
        import importlib
        import os
        os.environ.pop("CHAOSLAB_SHOW_DEMO_APPS", None)
        prev = os.environ.get("CHAOSLAB_SHOW_DEMO_APPS")
        try:
            from webapp import config
            importlib.reload(config)
            self.assertFalse(config.SHOW_DEMO_APPS)
            os.environ["CHAOSLAB_SHOW_DEMO_APPS"] = "true"
            importlib.reload(config)
            self.assertTrue(config.SHOW_DEMO_APPS)
        finally:
            os.environ.pop("CHAOSLAB_SHOW_DEMO_APPS", None)
            if prev is not None:
                os.environ["CHAOSLAB_SHOW_DEMO_APPS"] = prev
            os.environ["CHAOSLAB_SHOW_DEMO_APPS"] = "true"      # keep the rest of the suite's setting
            importlib.reload(config)
            self.config = config


if __name__ == "__main__":
    unittest.main()
