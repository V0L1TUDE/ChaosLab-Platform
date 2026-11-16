import json
import time
import unittest

from harness import Client


def demo_app_id(c, slug="online-boutique"):
    return [a for a in c.get("/api/applications")[1]["applications"] if a.get("slug") == slug][0]["id"]


def run_sim(c, service="paymentservice", fault="pod-kill", duration=30, app_id=None):
    status, data, _ = c.post("/api/experiments", {
        "application_id": app_id or demo_app_id(c), "target_service": service, "fault_type": fault,
        "duration_seconds": duration, "mode": "simulated"})
    assert status == 201, (status, data)
    return data["experiment"]["id"]


def wait_done(c, exp_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        exp = c.get(f"/api/experiments/{exp_id}")[1]["experiment"]
        if exp["status"] in ("completed", "failed"):
            return exp
        time.sleep(0.1)
    raise AssertionError("experiment did not finish")


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.c = Client()
        self.c.register()

    def test_simulated_run_end_to_end(self):
        exp_id = run_sim(self.c)
        exp = wait_done(self.c, exp_id)
        self.assertEqual(exp["status"], "completed")
        self.assertTrue(exp["simulated"])
        self.assertEqual(exp["mode"], "simulated")
        r = exp["result"]
        self.assertTrue(r["simulated"])
        self.assertIn("SIMULATED", r["data_source"])
        self.assertEqual(exp["oracle_verdict"], "PASS")
        # every event is flagged + prefixed
        self.assertTrue(exp["events"])
        for e in exp["events"]:
            self.assertEqual(e["simulated"], 1)
            self.assertTrue(e["message"].startswith("[SIMULATED]"), e)
        # all 11 real Oracle invariants present, split in three groups
        o = r["oracle"]
        self.assertEqual(o["counts"]["total"], 11)
        self.assertEqual([len(o["groups"][g]) for g in ("infrastructure", "application", "telemetry")], [5, 3, 3])
        self.assertTrue(all(rule["simulated"] for rule in o["rules"]))
        stages = [t["key"] for t in r["timeline"]]
        self.assertEqual(stages, ["baseline", "fault_injection", "impact", "recovery", "verification", "oracle", "completed"])
        self.assertTrue(exp["samples"] and all(s["simulated"] for s in exp["samples"]))

    def test_network_fault_marks_target_impact_not_applicable_not_pass(self):
        exp = wait_done(self.c, run_sim(self.c, "frontend", "network-delay", 20))
        rules = {r["rule"]: r for r in exp["result"]["oracle"]["rules"]}
        self.assertEqual(rules["target_impact"]["status"], "NOT_APPLICABLE")
        self.assertEqual(rules["target_impact"]["status_label"], "N/A")
        counts = exp["result"]["oracle"]["counts"]
        self.assertEqual(counts["na"], 1)
        self.assertEqual(counts["executed"], counts["pass"] + counts["fail"])
        self.assertLess(counts["pass"], counts["total"])

    def test_predefined_degraded_scenario_fails_oracle(self):
        exp = wait_done(self.c, run_sim(self.c, "checkoutservice", "network-failure", 20))
        self.assertEqual(exp["status"], "completed")
        self.assertEqual(exp["oracle_verdict"], "FAIL")
        failed = [r["rule"] for r in exp["result"]["oracle"]["rules"] if r["status"] == "FAIL"]
        self.assertEqual(failed, ["telemetry_latency_recovered"])

    def test_validation(self):
        app_id = demo_app_id(self.c)
        base = {"application_id": app_id, "target_service": "paymentservice", "fault_type": "pod-kill",
                "duration_seconds": 30, "mode": "simulated"}
        post = lambda **kw: self.c.post("/api/experiments", {**base, **kw})[0]
        self.assertEqual(post(target_service="redis-cart"), 400)       # not targetable
        self.assertEqual(post(target_service="nope"), 400)
        self.assertEqual(post(fault_type="cpu-stress"), 400)
        self.assertEqual(post(duration_seconds=1), 400)
        self.assertEqual(post(duration_seconds=9999), 400)
        self.assertEqual(post(duration_seconds="30"), 400)
        self.assertEqual(post(mode="turbo"), 400)
        self.assertEqual(post(application_id="1"), 400)
        self.assertEqual(post(application_id=999999), 404)

    def test_live_mode_rejected_when_unavailable(self):
        status, data, _ = self.c.post("/api/experiments", {
            "application_id": demo_app_id(self.c), "target_service": "paymentservice", "fault_type": "pod-kill",
            "duration_seconds": 30, "mode": "live"})
        self.assertEqual(status, 503)
        self.assertIn("unavailable", data["error"])
        caps = self.c.get("/api/capabilities")[1]["modes"]
        self.assertFalse(caps["live"]["available"])
        self.assertTrue(caps["simulated"]["available"])

    def test_uploaded_app_cannot_run_experiments(self):
        app = self.c.upload("/api/applications", {"name": "Mine"}, "a.yaml", b"a: 1\n")[1]["application"]
        status, data, _ = self.c.post("/api/experiments", {
            "application_id": app["id"], "target_service": "paymentservice", "fault_type": "pod-kill",
            "duration_seconds": 30, "mode": "simulated"})
        self.assertEqual(status, 400)

    def test_failed_execution_is_recorded_not_hidden(self):
        from webapp import runner_service
        from webapp.execution import ExecutionBackend, ExecutionError

        class Boom(ExecutionBackend):
            mode = "simulated"

            def run(self, spec, sink):
                sink.event("baseline", "starting")
                raise ExecutionError("cluster exploded")

        original = runner_service.BACKENDS["simulated"]
        runner_service.BACKENDS["simulated"] = Boom()
        try:
            exp = wait_done(self.c, run_sim(self.c))
        finally:
            runner_service.BACKENDS["simulated"] = original
        self.assertEqual(exp["status"], "failed")
        self.assertIn("cluster exploded", exp["error"])
        self.assertIsNone(exp["oracle_verdict"])
        report = self.c.request("GET", f"/api/experiments/{exp['id']}/report?format=json")[1]
        self.assertEqual(report["overall_verdict"], "NOT_EVALUATED")  # never silently PASS

    def test_experiments_are_private(self):
        exp_id = run_sim(self.c)
        wait_done(self.c, exp_id)
        other = Client()
        other.register()
        self.assertEqual(other.get(f"/api/experiments/{exp_id}")[0], 404)
        self.assertEqual(other.get(f"/api/experiments/{exp_id}/report?format=html")[0], 404)
        self.assertEqual(other.post(f"/api/experiments/{exp_id}/report")[0], 404)
        self.assertEqual(other.get("/api/experiments")[1]["experiments"], [])
        self.assertEqual(other.get("/api/reports")[1]["reports"], [])

    def test_dashboard_counts(self):
        wait_done(self.c, run_sim(self.c))
        wait_done(self.c, run_sim(self.c, "checkoutservice", "network-failure", 20))
        d = self.c.get("/api/dashboard")[1]
        self.assertEqual(d["totals"]["experiments"], 2)
        self.assertEqual(d["totals"]["successful"], 1)
        self.assertEqual(d["totals"]["failed"], 1)
        self.assertEqual(d["totals"]["simulated"], 2)
        self.assertEqual(d["totals"]["applications"], 3)  # the three built-in demos
        self.assertEqual(len(d["recent"]), 2)

    def test_oracle_catalog(self):
        data = self.c.get("/api/oracle/invariants")[1]
        names = [i["name"] for g in data["groups"] for i in g["invariants"]]
        self.assertEqual(len(names), 11)
        self.assertEqual({s["status"] for s in data["statuses"]}, {"PASS", "FAIL", "SKIP", "N/A"})


class ReportTests(unittest.TestCase):
    def test_report_generation_download_and_labels(self):
        c = Client()
        c.register()
        exp = wait_done(c, run_sim(c, "cartservice", "pod-kill", 30))
        self.assertEqual(sorted(exp["reports"]), ["html", "json"])

        status, rep, _ = c.request("GET", f"/api/experiments/{exp['id']}/report?format=json")
        for key in ("experiment_id", "application", "target_service", "fault", "configuration", "execution_mode",
                    "timeline", "oracle", "recovery_metrics", "overall_verdict", "final_status"):
            self.assertIn(key, rep)
        self.assertEqual(rep["execution_mode"], "Simulated")
        self.assertIn("SIMULATED", rep["simulation_notice"])
        self.assertEqual(rep["target_service"], "cartservice")

        status, body, resp = c.request("GET", f"/api/experiments/{exp['id']}/report?format=html&download=1")
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("SIMULATED REPORT", text)
        self.assertIn("attachment", resp.getheader("Content-Disposition"))
        self.assertIn("SIMULATED", resp.getheader("Content-Disposition"))
        self.assertIn("sandbox", resp.getheader("Content-Security-Policy"))

        listing = c.get("/api/reports")[1]["reports"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["overall_result"], "PASS")
        self.assertTrue(listing[0]["simulated"])

        self.assertEqual(c.post(f"/api/experiments/{exp['id']}/report")[0], 201)  # regenerate
        self.assertEqual(c.request("GET", f"/api/experiments/{exp['id']}/report?format=exe")[0], 400)

    def test_html_report_escapes_user_controlled_text(self):
        from webapp import db, reports
        c = Client()
        user, *_ = c.register()
        exp = wait_done(c, run_sim(c))
        with db.session() as conn:
            conn.execute("UPDATE experiments SET error = ? WHERE id = ?", ("<script>alert(1)</script>", exp["id"]))
        reports.generate(exp["id"])
        text = c.request("GET", f"/api/experiments/{exp['id']}/report?format=html")[1].decode()
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;", text)


if __name__ == "__main__":
    unittest.main()
