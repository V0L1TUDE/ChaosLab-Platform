"""Shared Live demo: one operator-chosen cluster application that every account may run experiments against."""

import unittest

from harness import Client
from test_clusters import connect, make_report, register_app, start_experiment
from test_experiments import wait_done


class SharedDemoTests(unittest.TestCase):
    def setUp(self):
        from webapp import config
        self.config = config
        self.saved = (config.SHARED_APP_ID, config.SHARED_LIVE_RUNS_PER_HOUR, config.SHARED_MAX_DURATION_SECONDS)
        self.owner = Client()
        self.owner.register()
        self.agent = connect(self.owner, name="Demo")
        self.app_id = register_app(self.owner, self.agent, name="Shared Shop")
        self.guest = Client()
        self.guest.register()

    def tearDown(self):
        (self.config.SHARED_APP_ID, self.config.SHARED_LIVE_RUNS_PER_HOUR,
         self.config.SHARED_MAX_DURATION_SECONDS) = self.saved

    def share(self, app_id=None):
        self.config.SHARED_APP_ID = app_id or self.app_id

    def guest_run(self, service="web"):
        status, data, _ = start_experiment(self.guest, self.app_id, service)
        self.assertEqual(status, 201, data)
        exp_id = data["experiment"]["id"]
        job = self.agent.wait_job()
        self.assertEqual(job["experiment_id"], exp_id)
        self.assertEqual(self.agent.call(f"/agent/v1/jobs/{job['job_id']}/result",
                                         {"report": make_report(service)}, job["job_token"])[0], 200)
        return exp_id

    def test_not_visible_unless_shared(self):
        self.assertEqual(self.guest.get(f"/api/applications/{self.app_id}")[0], 404)
        self.assertNotIn(self.app_id, [a["id"] for a in self.guest.get("/api/applications")[1]["applications"]])
        self.assertEqual(start_experiment(self.guest, self.app_id)[0], 404)

    def test_guest_sees_it_read_only(self):
        self.share()
        apps = {a["id"]: a for a in self.guest.get("/api/applications")[1]["applications"]}
        self.assertTrue(apps[self.app_id]["is_shared"])
        self.assertFalse(apps[self.app_id]["can_manage"])
        self.assertTrue(apps[self.app_id]["executable"])
        dash = {a["id"] for a in self.guest.get("/api/dashboard")[1]["applications"]}
        self.assertIn(self.app_id, dash)
        a = self.guest.get(f"/api/applications/{self.app_id}")[1]["application"]
        self.assertIn("Shared Live demo", a["note"])
        self.assertIsNone(a["cluster"]["id"])                   # no link into the owner's cluster page
        self.assertEqual(a["cluster"]["name"], "Demo")
        # the owner still manages it
        mine = self.owner.get(f"/api/applications/{self.app_id}")[1]["application"]
        self.assertTrue(mine["is_shared"] and mine["can_manage"])
        self.assertEqual(mine["cluster"]["id"], self.agent.cluster_id)

    def test_guest_cannot_manage_app_or_cluster(self):
        self.share()
        self.assertEqual(self.guest.delete(f"/api/applications/{self.app_id}")[0], 403)
        self.assertEqual(self.guest.post(f"/api/applications/{self.app_id}/deploy")[0], 403)
        self.assertEqual(self.guest.post(f"/api/applications/{self.app_id}/undeploy")[0], 403)
        probes = {"health": {"port": 8080, "path": "/health"}}
        self.assertEqual(self.guest.post(f"/api/applications/{self.app_id}/probes", {"service": "web", "probes": probes})[0], 404)
        self.assertEqual(self.guest.post(f"/api/applications/{self.app_id}/telemetry",
                                         {"service": "web", "telemetry": None})[0], 404)
        cid = self.agent.cluster_id
        self.assertEqual(self.guest.get(f"/api/clusters/{cid}")[0], 404)
        self.assertEqual(self.guest.post(f"/api/clusters/{cid}/emergency-stop")[0], 404)
        self.assertEqual(self.guest.post(f"/api/clusters/{cid}/applications", {"name": "x", "namespace": "shop"})[0], 404)
        self.assertEqual(self.owner.get(f"/api/applications/{self.app_id}")[0], 200)     # still there
        # even the owner cannot delete it while shared (it would cascade to guests' experiments)
        self.assertEqual(self.owner.delete(f"/api/applications/{self.app_id}")[0], 409)
        self.config.SHARED_APP_ID = 0
        self.assertEqual(self.owner.delete(f"/api/applications/{self.app_id}")[0], 200)

    def test_guest_live_run_is_real_and_private(self):
        self.share()
        exp_id = self.guest_run()
        exp = wait_done(self.guest, exp_id)
        self.assertEqual(exp["status"], "completed", exp)
        self.assertEqual(exp["oracle_verdict"], "PASS")
        self.assertFalse(exp["simulated"])
        self.assertIn("shared demo", exp["result"]["data_source"])
        self.assertIn("on the shared demo cluster", exp["result"]["notes"][0])
        # the owner does not see the guest's experiment or report
        self.assertEqual(self.owner.get(f"/api/experiments/{exp_id}")[0], 404)
        self.assertNotIn(exp_id, [e["id"] for e in self.owner.get("/api/experiments")[1]["experiments"]])
        self.assertEqual(self.owner.request("GET", f"/api/experiments/{exp_id}/report?format=json")[0], 404)
        # and the guest's experiment count on the app is their own
        a = self.guest.get(f"/api/applications/{self.app_id}")[1]["application"]
        self.assertEqual([e["id"] for e in a["experiments"]], [exp_id])

    def test_fair_use_limits(self):
        self.share()
        self.config.SHARED_LIVE_RUNS_PER_HOUR = 1
        status, data, _ = self.guest.post("/api/experiments", {"application_id": self.app_id, "target_service": "web",
                                                              "fault_type": "pod-kill", "duration_seconds": 90, "mode": "live"})
        self.assertEqual(status, 400)
        self.assertIn("at most 60 seconds", data["error"])
        # someone else's run occupies the cluster
        self.assertEqual(start_experiment(self.owner, self.app_id)[0], 201)
        status, data, _ = start_experiment(self.guest, self.app_id)
        self.assertEqual(status, 409)
        self.assertIn("shared demo cluster", data["error"])
        job = self.agent.wait_job()
        self.agent.call(f"/agent/v1/jobs/{job['job_id']}/result", {"report": make_report()}, job["job_token"])
        wait_done(self.owner, job["experiment_id"])
        # one Live run per hour for the guest
        wait_done(self.guest, self.guest_run())
        status, data, _ = start_experiment(self.guest, self.app_id)
        self.assertEqual(status, 429)
        self.assertIn("Fair use", data["error"])
        # Simulated mode is not rate limited, and the owner is never limited
        self.assertEqual(start_experiment(self.guest, self.app_id, mode="simulated")[0], 201)
        self.assertEqual(start_experiment(self.owner, self.app_id, "api")[0], 201)

    def test_only_cluster_apps_can_be_shared(self):
        status, data, _ = self.owner.upload("/api/applications", {"name": "notes"}, "notes.txt", b"hello")
        self.assertEqual(status, 201, data)
        upload_id = data["application"]["id"]
        self.share(upload_id)
        self.assertEqual(self.guest.get(f"/api/applications/{upload_id}")[0], 404)


if __name__ == "__main__":
    unittest.main()
