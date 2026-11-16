"""Safe application probes: validation, execution against a real local HTTP target, agent pre-flight/post-run
behaviour, server/agent validator parity, API, and how the server Oracle treats the evidence."""

import copy
import http.server
import json
import os
import sys
import threading
import time
import types
import unittest

from harness import CHAOSLAB_DIR, Client
from test_clusters import AgentSim, INVENTORY, connect, make_report, register_app, start_experiment
from test_experiments import wait_done

sys.path.insert(0, os.path.join(CHAOSLAB_DIR, "agent"))
import agent as agent_mod  # noqa: E402
import safe_probes  # noqa: E402
from webapp import probe_spec  # noqa: E402

GOOD = {"health": {"port": 8000, "path": "/health", "expected_status": 200},
        "functional": {"port": 8000, "path": "/items?limit=1", "expected_status": 200, "expected_body_contains": "items"},
        "logs": {"expected_log_pattern": "Uvicorn running", "prohibited_patterns": ["panic:", "fatal error"]}}


# --------------------------------------------------------------------------- a real HTTP target on localhost
class Target(http.server.BaseHTTPRequestHandler):
    healthy = True

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200 if Target.healthy else 503)
            self.end_headers()
            self.wfile.write(b"ok" if Target.healthy else b"down")
        elif self.path.startswith("/items"):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"items": [1]}')
        elif self.path == "/redir":
            self.send_response(302); self.send_header("Location", "http://evil.example/steal"); self.end_headers()
        elif self.path == "/big":
            self.send_response(200); self.end_headers(); self.wfile.write(b"x" * 300000)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):           # must never be reached by a probe
        self.send_response(500); self.end_headers()
        Target.saw_post = True


class FakeForward:
    """Stands in for `kubectl port-forward`: hands back the port of the local target and records the request."""
    calls = []

    def __init__(self, namespace, pod, port):
        FakeForward.calls.append((namespace, pod, port))

    def __enter__(self):
        return Target.port

    def __exit__(self, *a):
        return False


def fake_kubectl(logs="Uvicorn running on 0.0.0.0:8000\n", pods=("web-new", "web-old")):
    calls = []

    def kubectl(args, timeout=25):
        calls.append(list(args))
        if args[:2] == ["get", "pods"]:
            items = [{"metadata": {"name": n, "creationTimestamp": f"2026-01-01T00:00:0{i}Z"},
                      "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}} for i, n in enumerate(reversed(pods))]
            return True, json.dumps({"items": items})
        if args[0] == "logs":
            return True, logs
        return False, "unexpected"
    kubectl.calls = calls
    return kubectl


class ProbeBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
        Target.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        Target.healthy, Target.saw_post = True, False
        FakeForward.calls = []


# --------------------------------------------------------------------------- spec validation
BAD_SPECS = [
    ({"health": {"port": 0, "path": "/h"}}, "port"),
    ({"health": {"port": 70000, "path": "/h"}}, "port"),
    ({"health": {"port": "80", "path": "/h"}}, "port"),
    ({"health": {"port": True, "path": "/h"}}, "port"),
    ({"health": {"port": 80, "path": "health"}}, "path"),
    ({"health": {"port": 80, "path": "http://169.254.169.254/latest"}}, "path"),
    ({"health": {"port": 80, "path": "//evil.example/x"}}, None),      # allowed charset but see below
    ({"health": {"port": 80, "path": "/a/../../etc/passwd"}}, "path"),
    ({"health": {"port": 80, "path": "/x'); import os; os.system('id') #"}}, "path"),
    ({"health": {"port": 80, "path": "/x\r\nHost: evil"}}, "path"),
    ({"health": {"port": 80, "path": "/x y"}}, "path"),
    ({"health": {"port": 80, "path": "/" + "a" * 250}}, "path"),
    ({"health": {"port": 80, "path": "/h", "expected_status": 99}}, "expected_status"),
    ({"health": {"port": 80, "path": "/h", "expected_status": 600}}, "expected_status"),
    ({"health": {"port": 80, "path": "/h", "method": "POST"}}, "unsupported"),
    ({"health": {"port": 80, "path": "/h", "command": ["sh"]}}, "unsupported"),
    ({"health": {"port": 80, "path": "/h", "host": "evil"}}, "unsupported"),
    ({"health": {"port": 80, "path": "/h", "expected_body_contains": "x" * 101}}, "expected_body_contains"),
    ({"health": {"port": 80, "path": "/h", "expected_body_contains": "bad\x00"}}, "expected_body_contains"),
    ({"functional": {"port": 80, "path": "/f"}}, "health"),
    ({"health": {"port": 80, "path": "/h"}, "exec": ["id"]}, "unsupported probe section"),
    ({"health": {"port": 80, "path": "/h"}, "logs": {"command": "x"}}, "logs"),
    ({"health": {"port": 80, "path": "/h"}, "logs": {"prohibited_patterns": ["a"] * 11}}, "prohibited"),
    ({"health": {"port": 80, "path": "/h"}, "logs": {"prohibited_patterns": [5]}}, "prohibited"),
    ("not a dict", "object"), (None, "object"), ([], "object"),
]


class ValidationTests(unittest.TestCase):
    def test_good_spec(self):
        clean, err = safe_probes.validate_probe_spec(GOOD)
        self.assertIsNone(err)
        self.assertEqual(clean["health"], {"port": 8000, "path": "/health", "expected_status": 200})
        self.assertEqual(clean["logs"]["prohibited_patterns"], ["panic:", "fatal error"])

    def test_rejections(self):
        for spec, fragment in BAD_SPECS:
            clean, err = safe_probes.validate_probe_spec(spec)
            if fragment is None:
                continue
            self.assertIsNone(clean, spec)
            self.assertIn(fragment, err, spec)

    def test_defaults_and_minimal(self):
        clean, err = safe_probes.validate_probe_spec({"health": {"port": 80, "path": "/"}})
        self.assertIsNone(err)
        self.assertEqual(clean, {"health": {"port": 80, "path": "/", "expected_status": 200}})

    def test_server_and_agent_validators_agree(self):
        """The server's UX validator must never accept something the agent rejects (and vice versa)."""
        corpus = [s for s, _ in BAD_SPECS] + [GOOD, {"health": {"port": 80, "path": "/"}},
                                              {"health": {"port": 80, "path": "/h", "expected_status": 204}},
                                              {"health": {"port": 80, "path": "/h"}, "logs": {}}]
        for spec in corpus:
            a = safe_probes.validate_probe_spec(copy.deepcopy(spec))
            b = probe_spec.validate(copy.deepcopy(spec))
            self.assertEqual(a, b, spec)

    def test_forward_target_is_validated(self):
        for ns, pod, port in [("a;b", "pod", 80), ("ns", "pod; rm -rf /", 80), ("ns", "Pod", 80), ("ns", "pod", 0),
                              ("ns", "pod", "80"), ("NS", "pod", 80), ("ns", "pod --address 0.0.0.0", 80)]:
            with self.assertRaises(safe_probes.ProbeError, msg=(ns, pod, port)):
                safe_probes.PortForward(ns, pod, port)
        pf = safe_probes.PortForward("shop", "web-abc12", 8000)
        self.assertEqual(pf.args, ["kubectl", "port-forward", "-n", "shop", "pod/web-abc12", ":8000", "--address", "127.0.0.1"])


# --------------------------------------------------------------------------- execution against a real HTTP target
class ExecutionTests(ProbeBase):
    def run_it(self, spec=GOOD, **kw):
        kubectl = fake_kubectl(**{k: kw.pop(k) for k in list(kw) if k in ("logs", "pods")})
        ev = safe_probes.run_probes(spec, "shop", "web", kubectl, forward_factory=FakeForward, **kw)
        return ev, kubectl

    def test_all_probes_pass_and_evidence_matches_oracle_shape(self):
        ev, kubectl = self.run_it()
        self.assertTrue(ev["enabled"] and ev["safe_probes"])
        self.assertEqual((ev["probe_type"], ev["probed_pod"]), ("http", "web-new"))      # newest ready pod
        self.assertTrue(ev["health_check_passed"] and ev["functional_test_passed"])
        self.assertEqual(ev["health_status"], "200 OK")
        self.assertTrue(ev["startup_log_detected"])
        self.assertEqual(ev["log_error_count"], 0)
        self.assertTrue(ev["health_details"]["latency_ms"] >= 0)
        self.assertEqual(FakeForward.calls[0], ("shop", "web-new", 8000))
        self.assertFalse(any(c[0] == "exec" for c in kubectl.calls))                       # never exec

    def test_unhealthy_wrong_status_and_body(self):
        Target.healthy = False
        ev, _ = self.run_it()
        self.assertFalse(ev["health_check_passed"])
        self.assertEqual(ev["health_status"], "HTTP 503")
        Target.healthy = True
        spec = copy.deepcopy(GOOD); spec["health"]["expected_body_contains"] = "definitely-not-there"
        ev, _ = self.run_it(spec)
        self.assertFalse(ev["health_check_passed"])
        self.assertIn("does not contain", ev["health_details"]["detail"])
        spec = copy.deepcopy(GOOD); spec["functional"]["path"] = "/nope"
        ev, _ = self.run_it(spec)
        self.assertTrue(ev["health_check_passed"])
        self.assertFalse(ev["functional_test_passed"])

    def test_redirects_are_not_followed_and_only_get_is_used(self):
        ev, _ = self.run_it({"health": {"port": 1, "path": "/redir", "expected_status": 302}})
        self.assertTrue(ev["health_check_passed"])                      # the 302 itself, not evil.example
        ev, _ = self.run_it({"health": {"port": 1, "path": "/redir", "expected_status": 200}})
        self.assertFalse(ev["health_check_passed"])
        self.assertFalse(Target.saw_post)

    def test_large_bodies_are_capped(self):
        r = safe_probes.http_get(Target.port, "/big")
        self.assertEqual(r["status"], 200)
        self.assertLessEqual(len(r["body"]), safe_probes.MAX_BODY)

    def test_connection_errors_become_unreachable_not_exceptions(self):
        class Dead(FakeForward):
            def __enter__(self):
                raise safe_probes.ProbeError("port-forward failed: pod not found")
        ev = safe_probes.run_probes(GOOD, "shop", "web", fake_kubectl(), forward_factory=Dead)
        self.assertFalse(ev["health_check_passed"])
        self.assertEqual(ev["health_status"], "UNREACHABLE")
        self.assertIn("port-forward failed", ev["health_details"]["detail"])

    def test_no_ready_pod(self):
        ev = safe_probes.run_probes(GOOD, "shop", "web", fake_kubectl(pods=()), forward_factory=FakeForward)
        self.assertEqual(ev["health_status"], "NO_READY_POD")
        self.assertFalse(ev["health_check_passed"])
        self.assertIsNone(ev["probed_pod"])

    def test_retries_until_the_app_recovers(self):
        Target.healthy = False
        ticks = {"n": 0}

        def sleep(_):
            ticks["n"] += 1
            if ticks["n"] == 2:
                Target.healthy = True                                   # app comes back after two retries
        clock_t = {"t": 0.0}

        def clock():
            clock_t["t"] += 1.0
            return clock_t["t"]
        ev = safe_probes.run_probes(GOOD, "shop", "web", fake_kubectl(), forward_factory=FakeForward,
                                    wait_seconds=30, interval=2, sleep=sleep, clock=clock)
        self.assertTrue(ev["health_check_passed"])
        self.assertGreaterEqual(ticks["n"], 2)

    def test_log_checks(self):
        ev, _ = self.run_it(logs="boot\npanic: runtime error\nUvicorn running\n")
        self.assertTrue(ev["startup_log_detected"])
        self.assertEqual(ev["log_error_count"], 1)                      # prohibited pattern found
        ev, _ = self.run_it(logs="nothing useful\n")
        self.assertFalse(ev["startup_log_detected"])

    def test_optional_sections_omitted(self):
        ev, _ = self.run_it({"health": {"port": 1, "path": "/health"}})
        self.assertTrue(ev["functional_test_skipped"])
        self.assertFalse(ev["logs_verified"])


# --------------------------------------------------------------------------- agent: job policy + pre-flight/post-run
def probe_job(probes, ns="shop", service="web"):
    cfg = {"name": "chaoslab-exp-1-web-pod-kill", "target": {"namespace": ns, "label": {"app": service}},
           "fault": {"type": "pod-kill", "mode": "one"}, "recovery": {"timeout_seconds": 30, "expected_ready_replicas": 1},
           "application": {"enabled": True, "probes": probes}}
    return {"job_id": 7, "job_token": "clj_x", "engine_config": cfg, "max_runtime_seconds": 300}


class AgentJobPolicyTests(unittest.TestCase):
    P = agent_mod.Policy(["shop"])

    def test_valid_probes_are_kept_and_engine_probes_stay_off(self):
        cfg, err = agent_mod.validate_job(probe_job(GOOD), self.P)
        self.assertIsNone(err)
        self.assertEqual(cfg["application"], {"enabled": False})              # engine's exec probes never enabled
        self.assertEqual(cfg["probes"]["health"]["path"], "/health")

    def test_hostile_probe_spec_rejects_the_whole_job(self):
        for bad in ({"health": {"port": 80, "path": "/x'); import os; os.system('id')#"}},
                    {"health": {"port": 80, "path": "/h", "method": "POST"}},
                    {"exec": ["sh", "-c", "id"]}, {"health": {"port": 80, "path": "http://evil/"}}):
            cfg, err = agent_mod.validate_job(probe_job(bad), self.P)
            self.assertIsNone(cfg)
            self.assertIn("invalid probe spec", err)

    def test_probes_can_be_disabled_locally(self):
        cfg, err = agent_mod.validate_job(probe_job(GOOD), agent_mod.Policy(["shop"], allow_probes=False))
        self.assertIsNone(err)
        self.assertIsNone(cfg["probes"])

    def test_no_probes_means_none(self):
        j = probe_job(GOOD); j["engine_config"].pop("application")
        self.assertIsNone(agent_mod.validate_job(j, self.P)[0]["probes"])


class StubEngine(types.ModuleType):
    """A stand-in for verifier/engine.py: reports a successful pod-kill without touching a cluster."""

    def __init__(self):
        super().__init__("engine")
        self.runs, self.REPORTS_DIR = 0, ""

    def verify_experiment(self, cfg_path, progress_callback=None):
        self.runs += 1
        progress_callback("baseline", 10, "stub baseline")
        progress_callback("completed", 100, "stub done", make_report())
        return True

    def get_pods(self, ns, selector):
        return []

    def generate_chaos_manifest(self, cfg):
        return "chaos-x", "PodChaos", {}


class ExecutorTests(ProbeBase):
    def setUp(self):
        super().setUp()
        self.stub = StubEngine()
        self.saved = (sys.modules.get("engine"), agent_mod.kubectl, safe_probes.PortForward)
        sys.modules["engine"] = self.stub
        agent_mod.kubectl = fake_kubectl()
        safe_probes.PortForward = FakeForward
        self.sent = []
        rep = types.SimpleNamespace(events=[], samples=[], cancelled=threading.Event(),
                                    event=lambda *a, **k: self.sent.append(a), sample=lambda p: None, flush=lambda: None)
        self.reporter = rep

    def tearDown(self):
        eng, agent_mod.kubectl, safe_probes.PortForward = self.saved
        if eng is None:
            sys.modules.pop("engine", None)
        else:
            sys.modules["engine"] = eng

    def execute(self, probes):
        cfg, err = agent_mod.validate_job(probe_job(probes), agent_mod.Policy(["shop"]))
        self.assertIsNone(err)
        return agent_mod.engine_executor(probe_job(probes), cfg, self.reporter, os.path.join(CHAOSLAB_DIR, "verifier"))

    def test_preflight_failure_aborts_before_any_fault(self):
        Target.healthy = False
        out = self.execute(GOOD)
        self.assertIn("Pre-flight probe failed", out["error"])
        self.assertIn("NO fault was injected", out["error"])
        self.assertEqual(self.stub.runs, 0)                              # the engine (fault injection) never ran

    def test_misconfigured_probe_is_caught_preflight(self):
        spec = copy.deepcopy(GOOD); spec["health"]["path"] = "/wrong-path"
        out = self.execute(spec)
        self.assertIn("Pre-flight", out["error"])
        self.assertEqual(self.stub.runs, 0)

    def test_success_merges_post_run_probe_evidence_into_report(self):
        out = self.execute(GOOD)
        self.assertEqual(self.stub.runs, 1)
        app = out["report"]["application"]
        self.assertTrue(app["enabled"] and app["safe_probes"])
        self.assertTrue(app["health_check_passed"] and app["functional_test_passed"] and app["startup_log_detected"])
        self.assertTrue(app["baseline"]["health_passed"])               # pre-flight result recorded
        stages = [a[0] for a in self.sent]
        self.assertIn("verification", stages)

    def test_without_probes_nothing_changes(self):
        cfg, _ = agent_mod.validate_job(probe_job(GOOD), agent_mod.Policy(["shop"], allow_probes=False))
        out = agent_mod.engine_executor(probe_job(GOOD), cfg, self.reporter, os.path.join(CHAOSLAB_DIR, "verifier"))
        self.assertEqual(self.stub.runs, 1)
        self.assertEqual(out["report"]["application"], {"enabled": False})   # engine report untouched


# --------------------------------------------------------------------------- server: API + Oracle semantics
class ServerProbeTests(unittest.TestCase):
    def setUp(self):
        self.user = Client(); self.user.register()
        self.agent = connect(self.user)
        self.app_id = register_app(self.user, self.agent)

    def set_probes(self, service="web", probes=GOOD, client=None):
        return (client or self.user).post(f"/api/applications/{self.app_id}/probes", {"service": service, "probes": probes})

    def run_with_report(self, report):
        status, data, _ = start_experiment(self.user, self.app_id)
        self.assertEqual(status, 201, data)
        job = self.agent.wait_job()
        tok = job["job_token"]
        self.agent.call(f"/agent/v1/jobs/{job['job_id']}/result", {"report": report}, tok)
        return job, wait_done(self.user, data["experiment"]["id"])

    def test_api_validation_and_ownership(self):
        self.assertEqual(self.set_probes()[0], 200)
        self.assertEqual(self.set_probes(service="nope")[0], 400)
        self.assertEqual(self.set_probes(service="redis")[0], 400)               # datastore not targetable
        for spec, fragment in BAD_SPECS:
            if fragment and spec is not None:            # probes=null means "clear", tested below
                s, d, _ = self.set_probes(probes=spec)
                self.assertEqual(s, 400, spec)
        other = Client(); other.register()
        self.assertEqual(self.set_probes(client=other)[0], 404)                  # not yours
        self.assertEqual(Client().post(f"/api/applications/{self.app_id}/probes", {})[0], 401)
        detail = self.user.get(f"/api/applications/{self.app_id}")[1]["application"]
        self.assertEqual(detail["probes"]["web"]["health"]["path"], "/health")
        self.assertEqual(self.set_probes(probes=None)[0], 200)                   # clear
        self.assertEqual(self.user.get(f"/api/applications/{self.app_id}")[1]["application"]["probes"], {})

    def test_probes_only_for_cluster_apps(self):
        s, d, _ = self.user.upload("/api/applications", {"name": "n"}, "a.txt", b"hi")
        self.assertEqual(self.user.post(f"/api/applications/{d['application']['id']}/probes",
                                        {"service": "web", "probes": GOOD})[0], 409)

    def test_job_carries_the_probe_spec_for_the_agent(self):
        self.set_probes()
        status, data, _ = start_experiment(self.user, self.app_id)
        job = self.agent.wait_job()
        app = job["engine_config"]["application"]
        self.assertTrue(app["enabled"])
        self.assertEqual(app["probes"], probe_spec.validate(GOOD)[0])
        self.assertEqual(app["health_probe"]["port"], 8000)
        self.assertTrue(app["verify_logs"])
        # a different service has no probes configured -> disabled
        wait_done_job = self.agent.call(f"/agent/v1/jobs/{job['job_id']}/result", {"error": "x"}, job["job_token"])
        wait_done(self.user, data["experiment"]["id"])
        status, data, _ = start_experiment(self.user, self.app_id, "api")
        job2 = self.agent.wait_job()
        self.assertEqual(job2["engine_config"]["application"], {"enabled": False})
        self.agent.call(f"/agent/v1/jobs/{job2['job_id']}/result", {"error": "x"}, job2["job_token"])

    def rules(self, exp):
        return {r["rule"]: r["status_label"] for r in exp["result"]["oracle"]["rules"]}

    def evidence(self, **over):
        app = {"enabled": True, "probe_type": "http", "safe_probes": True, "health_check_passed": True, "health_status": "200 OK",
               "health_details": {}, "functional_test_passed": True, "transaction_id": None, "startup_log_detected": True,
               "log_error_count": 0, "logs_verified": True}
        app.update(over)
        return {**make_report(), "application": app}

    def test_oracle_application_invariants_pass_with_good_evidence(self):
        self.set_probes()
        _, exp = self.run_with_report(self.evidence())
        r = self.rules(exp)
        self.assertEqual((r["app_health_probe"], r["app_functional_check"], r["app_logs_healthy"]), ("PASS", "PASS", "PASS"))
        self.assertEqual(r["telemetry_traces_recorded"], "N/A")               # still no tracing for customer apps
        self.assertEqual(exp["oracle_verdict"], "PASS")

    def test_oracle_fails_when_the_app_is_unhealthy_after_the_fault(self):
        self.set_probes()
        _, exp = self.run_with_report(self.evidence(health_check_passed=False, health_status="HTTP 503",
                                                    functional_test_passed=False, startup_log_detected=False, log_error_count=2))
        r = self.rules(exp)
        self.assertEqual((r["app_health_probe"], r["app_functional_check"], r["app_logs_healthy"]), ("FAIL", "FAIL", "FAIL"))
        self.assertEqual(exp["oracle_verdict"], "FAIL")
        self.assertEqual(r["service_recovery"], "PASS")                         # infra recovered, the APP did not

    def test_configured_but_no_evidence_is_skip_not_pass(self):
        self.set_probes()
        _, exp = self.run_with_report({**make_report(), "application": {"enabled": False}})      # e.g. agent ran with --no-probes
        r = self.rules(exp)
        self.assertEqual(r["app_health_probe"], "SKIP")
        self.assertEqual(exp["result"]["oracle"]["counts"]["skip"], 1)

    def test_health_only_config_leaves_other_invariants_not_applicable(self):
        self.set_probes(probes={"health": {"port": 8000, "path": "/health"}})
        _, exp = self.run_with_report(self.evidence(functional_test_passed=False, startup_log_detected=False))
        r = self.rules(exp)
        self.assertEqual((r["app_health_probe"], r["app_functional_check"], r["app_logs_healthy"]), ("PASS", "N/A", "N/A"))

    def test_no_probes_configured_stays_not_applicable(self):
        _, exp = self.run_with_report(make_report())
        self.assertEqual(self.rules(exp)["app_health_probe"], "N/A")


if __name__ == "__main__":
    unittest.main()
