"""Verification Oracle: no PASS without positive Chaos Mesh evidence; no nominal values presented as measurements;
simulation stays labelled."""

import copy
import unittest

import harness  # noqa: F401  (sys.path + isolated env)

from webapp import engine_bridge  # noqa: E402
from webapp.agent_backend import AgentBackend  # noqa: E402
from webapp.live_backend import LiveBackend  # noqa: E402

CONFIRMED = {"conditions": [{"type": "Selected", "status": "True"}, {"type": "AllInjected", "status": "True"}],
             "container_records": [{"events": [{"operation": "Apply", "type": "Succeeded"}]}]}


def evidence(fault="pod-kill", **over):
    e = {
        "fault": fault,
        "baseline": {"pod_name": "web-1", "ready_replicas": 1, "ready": "1/1", "status": "Running"},
        "timestamps": {"fault_injected": "2026-11-19T10:00:00+00:00"},
        "fault_injected": True,
        "chaos_mesh_evidence": copy.deepcopy(CONFIRMED),
        "original_pod": "web-1",
        "original_pod_disappeared": fault == "pod-kill",
        "replacement_pod": "web-2",
        "replacement_ready": True,
        "recovered_ready_replicas": 1,
        "recovery_time_seconds": 4.2,
        "total_recovery_seconds": 6.0,
        "recovery_measured": True,
        "application": {"enabled": False},
        "telemetry": {"enabled": False},
    }
    e.update(over)
    return e


def cfg(fault="pod-kill"):
    return {"name": "x", "target": {"namespace": "shop", "label": {"app": "web"}},
            "fault": {"type": fault, "mode": "one"}, "recovery": {"timeout_seconds": 30, "expected_ready_replicas": 1},
            "application": {"enabled": False}}


def evaluate(ev, c=None):
    return engine_bridge.oracle().VerificationOracle().evaluate(ev, c or cfg(ev["fault"]))


def rule(result, name):
    return next(r for r in result["rules"] if r["rule"] == name) if "rules" in result else \
        next(r for r in result["rule_results"] if r["rule"] == name)


def rules_of(result):
    return {r["rule"]: r for r in result["rule_evaluations"]}


class FaultInjectedRuleTests(unittest.TestCase):
    def test_confirmed_injection_passes(self):
        r = evaluate(evidence())
        self.assertEqual(rules_of(r)["fault_injected"]["status"], "PASS")
        self.assertEqual(r["verdict"], "PASS")
        only_record = evidence(chaos_mesh_evidence={"conditions": [], "container_records": CONFIRMED["container_records"]})
        self.assertEqual(rules_of(evaluate(only_record))["fault_injected"]["status"], "PASS")

    def test_successful_apply_alone_does_not_pass(self):
        for chaos in ({}, {"conditions": [{"type": "AllInjected", "status": "False"}]},
                      {"conditions": [{"type": "Selected", "status": "True"}], "container_records": [
                          {"events": [{"operation": "Apply", "type": "Failed"}]}]},
                      {"status": "Unknown", "error": "podchaos not found"}, None, "garbage"):
            with self.subTest(chaos=chaos):
                r = evaluate(evidence(chaos_mesh_evidence=chaos))
                fi = rules_of(r)["fault_injected"]
                self.assertEqual(fi["status"], "FAIL")
                self.assertEqual(r["verdict"], "FAIL")              # everything else passed: this alone fails the run
        msg = rules_of(evaluate(evidence(chaos_mesh_evidence={})))["fault_injected"]["message"]
        self.assertIn("never confirmed", msg)
        msg = rules_of(evaluate(evidence(chaos_mesh_evidence={"error": "forbidden"})))["fault_injected"]["message"]
        self.assertIn("could not be read", msg)

    def test_apply_failure_fails(self):
        r = evaluate(evidence(fault_injected=False))
        self.assertEqual(rules_of(r)["fault_injected"]["status"], "FAIL")

    def test_server_reevaluates_agent_evidence(self):
        # An agent that claims PASS while its evidence has no Chaos Mesh confirmation: the server's verdict is FAIL.
        ev = evidence(chaos_mesh_evidence={}, oracle_evaluation={"verdict": "PASS"})
        spec = {"experiment_id": 1, "engine_config": cfg(), "shared_guest": False, "user_id": 1, "fault_id": "pod-kill",
                "service": "web", "duration": 30}

        class Sink:
            stage_times = {}

            def event(self, *a, **k):
                pass
        import webapp.agent_backend as ab
        real_session = ab.db.session

        class FakeConn:
            def execute(self, *a):
                class R:
                    def fetchall(self_inner):
                        return []
                return R()

        class Ctx:
            def __enter__(self):
                return FakeConn()

            def __exit__(self, *a):
                return False
        ab.db.session = lambda: Ctx()
        try:
            result = AgentBackend._to_result(spec, Sink(), ev, job_id=9)
        finally:
            ab.db.session = real_session
        self.assertEqual(result["oracle"]["verdict"], "FAIL")
        self.assertTrue(any("differs from the agent's local verdict (PASS)" in n for n in result["notes"]))


class RecoveryMeasurementTests(unittest.TestCase):
    def test_network_fault_without_measurement_is_skipped_not_passed(self):
        # what older engines sent: a nominal 0.5 s with no measurement flag
        ev = evidence("network-delay", recovery_time_seconds=0.5, total_recovery_seconds=5.0)
        ev.pop("recovery_measured")
        r = evaluate(ev)
        rec = rules_of(r)["recovery_within_timeout"]
        self.assertEqual(rec["status"], "SKIPPED")
        self.assertIn("not measured", rec["message"])

    def test_measured_network_recovery_is_judged(self):
        r = evaluate(evidence("network-delay", recovery_time_seconds=0.31, recovery_measured=True))
        rec = rules_of(r)["recovery_within_timeout"]
        self.assertEqual(rec["status"], "PASS")
        self.assertIn("after the network fault was removed", rec["message"])
        r = evaluate(evidence("network-loss", recovery_time_seconds=45.0, recovery_measured=True))
        self.assertEqual(rules_of(r)["recovery_within_timeout"]["status"], "FAIL")

    def test_pod_kill_without_recovery_fails(self):
        r = evaluate(evidence(recovery_time_seconds=None, recovered_ready_replicas=0, recovery_measured=False))
        self.assertEqual(rules_of(r)["recovery_within_timeout"]["status"], "FAIL")
        self.assertEqual(r["verdict"], "FAIL")

    def test_live_result_never_shows_nominal_values_as_measured(self):
        spec = {"experiment_id": 1, "engine_config": cfg("network-delay"), "service": "web",
                "fault_id": "network-delay", "duration": 30}
        ev = evidence("network-delay", recovery_time_seconds=0.5, total_recovery_seconds=5.0)
        ev.pop("recovery_measured")
        ev["oracle_evaluation"] = evaluate(ev)
        res = LiveBackend.build_result(spec, {}, ev, "LIVE: test")
        self.assertIsNone(res["metrics"]["recovery_seconds"])
        self.assertIs(res["metrics"]["recovery_measured"], False)
        self.assertTrue(any("not measured" in n for n in res["notes"]))
        ev = evidence("network-delay", recovery_time_seconds=0.31, recovery_measured=True)
        ev["oracle_evaluation"] = evaluate(ev)
        res = LiveBackend.build_result(spec, {}, ev, "LIVE: test")
        self.assertEqual(res["metrics"]["recovery_seconds"], 0.31)


class SimulationLabelTests(unittest.TestCase):
    def test_simulated_evidence_is_flagged(self):
        from webapp import sim_backend
        ev = sim_backend._build_evidence("web", "network-delay", {"tag": "t", "recovery": 1.4, "traces": 5, "spans": 9,
                                                                 "err_spans": 2, "post_p95": 100.0}, "http")
        self.assertIs(ev["simulated"], True)
        from webapp.execution import normalize_oracle
        norm = normalize_oracle(evaluate(ev, cfg("network-delay")), simulated=True)
        self.assertTrue(norm["simulated"])
        self.assertTrue(norm["rules"] and all(r["simulated"] and str(r["message"]).startswith("[SIMULATED]")
                                              for r in norm["rules"]))


if __name__ == "__main__":
    unittest.main()
