"""Tracing invariants for connected clusters: agent-side Jaeger collection and traffic, server settings, Oracle."""

import copy
import http.server
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse

from harness import CHAOSLAB_DIR, Client
from test_clusters import connect, make_report, register_app, start_experiment
from test_experiments import wait_done

sys.path.insert(0, os.path.join(CHAOSLAB_DIR, "agent"))
sys.path.insert(0, os.path.join(CHAOSLAB_DIR, "verifier"))
import safe_telemetry  # noqa: E402
import telemetry as engine_telemetry  # noqa: E402
from webapp import telemetry_spec  # noqa: E402

ANALYZE = engine_telemetry.TelemetryClient().analyze_traces


def span(ms, error=False):
    return {"operationName": "GET", "duration": int(ms * 1000), "tags": [{"key": "error", "value": True}] if error else []}


class FakeJaeger(http.server.BaseHTTPRequestHandler):
    """Jaeger query API stand-in. FakeJaeger.data[service] = list of (span-list) traces; records every query."""
    data, queries, status = {}, [], 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        FakeJaeger.queries.append({k: v[0] for k, v in q.items()})
        if FakeJaeger.status != 200:
            self.send_response(FakeJaeger.status); self.end_headers(); return
        svc = q.get("service", [""])[0]
        traces = [{"traceID": f"{svc}-{i}", "spans": spans} for i, spans in enumerate(FakeJaeger.data.get(svc, []))]
        body = json.dumps({"data": traces}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)


class Target(http.server.BaseHTTPRequestHandler):
    """A caller service: answers 503 while 'broken' (its dependency is down), else 200."""
    broken, hits = False, 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        Target.hits += 1
        self.send_response(503 if Target.broken else 200); self.end_headers(); self.wfile.write(b"x")


def serve(handler):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def forward_to(port, calls=None, fail=False):
    class Fwd:
        def __init__(self, namespace, name, p, kind="pod", startup_timeout=10.0):
            if calls is not None:
                calls.append((namespace, name, p, kind))

        def __enter__(self):
            if fail:
                import safe_probes
                raise safe_probes.ProbeError("port-forward failed: service not found")
            return port

        def __exit__(self, *a):
            return False
    return Fwd


REPORT = {**make_report("project-service"),
          "timestamps": {"fault_injected": "2026-01-01T00:00:10+00:00", "replacement_ready": "2026-01-01T00:00:30+00:00",
                         "experiment_end": "2026-01-01T00:00:40+00:00"}}
SPEC = {"service_name": "project-service", "max_allowed_p95_ms": 1000, "verify_error_spike": True,
        "traffic": {"service": "task-service", "port": 8003, "path": "/tasks/test"}}


class CollectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jaeger = serve(FakeJaeger)

    @classmethod
    def tearDownClass(cls):
        cls.jaeger.shutdown()

    def setUp(self):
        FakeJaeger.data, FakeJaeger.queries, FakeJaeger.status = {}, [], 200

    def collect(self, spec=SPEC, jaeger=("default", "jaeger", 16686), calls=None, fail=False):
        return safe_telemetry.collect(jaeger, spec, REPORT, ANALYZE, traffic_stats={"sent": 20, "errors": 9},
                                      forward_factory=forward_to(self.jaeger.server_address[1], calls, fail),
                                      now_us=1767225640_000000)

    def test_traces_latency_and_error_spans(self):
        FakeJaeger.data = {"project-service": [[span(20), span(30)], [span(40)]],
                           "task-service": [[span(15, error=True), span(5)], [span(9, error=True)]]}
        calls = []
        ev = self.collect(calls=calls)
        self.assertTrue(ev["available"])
        self.assertEqual(ev["traces_count"], 2)
        self.assertEqual(ev["error_spans_count"], 2)                          # from the caller during the fault
        self.assertEqual(ev["fault_window"]["caller_error_spans"], 2)
        self.assertLessEqual(ev["latency_ms"]["p95"], 40)
        self.assertEqual(ev["traffic"], {"sent": 20, "errors": 9})
        self.assertEqual(calls[0], ("default", "jaeger", 16686, "svc"))      # agent-chosen Jaeger, as a Service
        fault_q = [q for q in FakeJaeger.queries if q["service"] == "task-service"][0]
        self.assertEqual(int(fault_q["start"]), int(REPORT and 1767225610_000000) - 1_000_000)    # fault window start - 1s

    def test_no_tracing_source_is_a_collection_error(self):
        ev = self.collect(jaeger=None)
        self.assertFalse(ev["available"])
        self.assertIn("--jaeger", ev["collection_error"])

    def test_unreachable_or_failing_jaeger_is_a_collection_error(self):
        self.assertIn("could not query Jaeger", self.collect(fail=True)["collection_error"])
        FakeJaeger.status = 500
        self.assertIn("HTTP 500", self.collect()["collection_error"])

    def test_empty_post_window_widens_the_search(self):
        FakeJaeger.data = {}
        ev = self.collect()
        self.assertTrue(ev["available"])
        self.assertEqual(ev["traces_count"], 0)
        self.assertEqual(sum(1 for q in FakeJaeger.queries if q["service"] == "project-service"), 3)  # post, widened, fault


class TrafficTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target = serve(Target)

    @classmethod
    def tearDownClass(cls):
        cls.target.shutdown()

    def kubectl(self, args, timeout=25):
        return True, json.dumps({"items": [{"metadata": {"name": "task-abc", "creationTimestamp": "2026"},
                                            "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}}]})

    def test_traffic_counts_failures_and_stops_promptly(self):
        Target.broken, Target.hits = True, 0
        calls = []
        d = safe_telemetry.TrafficDriver("project-platform", SPEC["traffic"], self.kubectl,
                                         forward_factory=forward_to(self.target.server_address[1], calls), interval=0.05)
        d.start()
        time.sleep(0.6)
        started = time.time()
        stats = d.stop()
        self.assertLess(time.time() - started, 2)
        self.assertGreater(stats["sent"], 3)
        self.assertEqual(stats["errors"], stats["sent"])                     # 503 while the dependency is down
        self.assertEqual(calls[0][:3], ("project-platform", "task-abc", 8003))

    def test_traffic_is_bounded_in_time(self):
        Target.broken = False
        d = safe_telemetry.TrafficDriver("ns", SPEC["traffic"], self.kubectl,
                                         forward_factory=forward_to(self.target.server_address[1]), interval=0.05, max_seconds=0.3)
        d.start()
        time.sleep(0.8)
        sent = d.stats["sent"]
        time.sleep(0.3)
        self.assertEqual(d.stats["sent"], sent)                              # stopped by itself at max_seconds
        self.assertGreater(d.stop()["ok"], 0)


BAD = [
    ({"max_allowed_p95_ms": 0}, "p95"), ({"max_allowed_p95_ms": "1000"}, "p95"), ({"max_allowed_p95_ms": True}, "p95"),
    ({"service_name": "bad name;"}, "service_name"), ({"verify_error_spike": "yes"}, "verify_error_spike"),
    ({"verify_error_spike": True}, "needs traffic"), ({"query_endpoint": "http://x"}, "unsupported"),
    ({"traffic": {"service": "Bad!", "port": 80, "path": "/"}}, "traffic.service"),
    ({"traffic": {"service": "web", "port": 0, "path": "/"}}, "port"),
    ({"traffic": {"service": "web", "port": 80, "path": "http://evil/"}}, "path"),
    ({"traffic": {"service": "web", "port": 80, "path": "/", "method": "POST"}}, "unsupported"),
    ("nope", "object"),
]


class ValidationTests(unittest.TestCase):
    def test_parity_between_server_and_agent(self):
        corpus = [s for s, _ in BAD] + [{}, {"max_allowed_p95_ms": 250}, copy.deepcopy(SPEC), {"service_name": "custom.svc"}]
        for spec in corpus:
            server = telemetry_spec.validate(copy.deepcopy(spec), "project-service")
            agent = safe_telemetry.validate_telemetry_spec(copy.deepcopy(spec), "project-service")
            self.assertEqual(server, agent, spec)

    def test_rejections(self):
        for spec, frag in BAD:
            clean, err = telemetry_spec.validate(spec, "web")
            self.assertIsNone(clean, spec)
            self.assertIn(frag, err, spec)

    def test_parse_jaeger(self):
        self.assertEqual(safe_telemetry.parse_jaeger("default/jaeger:16686"), ("default", "jaeger", 16686))
        self.assertIsNone(safe_telemetry.parse_jaeger(None))
        for bad in ("jaeger:16686", "default/jaeger", "http://jaeger:16686", "default/jaeger:99999", "a b/c:1", "default/jaeger:80;rm"):
            with self.assertRaises(ValueError, msg=bad):
                safe_telemetry.parse_jaeger(bad)


class ServerTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.user = Client(); self.user.register()
        self.agent = connect(self.user)
        self.app_id = register_app(self.user, self.agent)

    def set_tel(self, tel, service="web", client=None):
        return (client or self.user).post(f"/api/applications/{self.app_id}/telemetry", {"service": service, "telemetry": tel})

    def run_with(self, telemetry_evidence):
        status, data, _ = start_experiment(self.user, self.app_id)
        self.assertEqual(status, 201, data)
        job = self.agent.wait_job()
        report = {**make_report(), "telemetry": telemetry_evidence}
        self.agent.call(f"/agent/v1/jobs/{job['job_id']}/result", {"report": report}, job["job_token"])
        return job, wait_done(self.user, data["experiment"]["id"])

    def rules(self, exp):
        return {r["rule"]: r["status_label"] for r in exp["result"]["oracle"]["rules"]}

    def good_evidence(self, **over):
        ev = {"enabled": True, "available": True, "service": "web", "traces_count": 12, "spans_count": 30,
              "error_spans_count": 4, "fault_window_errors": 4, "latency_ms": {"p50": 8, "p95": 20, "max": 40}}
        ev.update(over)
        return ev

    def test_api_validation_and_storage(self):
        tel = {"max_allowed_p95_ms": 800, "verify_error_spike": True, "traffic": {"service": "api", "port": 8080, "path": "/x"}}
        status, data, _ = self.set_tel(tel)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["telemetry"]["web"]["service_name"], "web")
        self.assertEqual(self.set_tel({**tel, "traffic": {"service": "not-in-ns", "port": 80, "path": "/"}})[0], 400)
        for spec, _ in BAD:
            if spec != "nope":
                self.assertEqual(self.set_tel(spec)[0], 400, spec)
        other = Client(); other.register()
        self.assertEqual(self.set_tel(tel, client=other)[0], 404)
        self.assertEqual(self.set_tel(None)[0], 200)
        self.assertEqual(self.user.get(f"/api/applications/{self.app_id}")[1]["application"]["telemetry"], {})

    def test_job_carries_telemetry_and_never_an_endpoint(self):
        self.set_tel({"max_allowed_p95_ms": 500})
        start_experiment(self.user, self.app_id)
        job = self.agent.wait_job()
        tel = job["engine_config"]["telemetry"]
        self.assertEqual(tel, {"enabled": True, "service_name": "web", "max_allowed_p95_ms": 500, "verify_error_spike": False})
        self.assertNotIn("query_endpoint", json.dumps(job))
        self.agent.call(f"/agent/v1/jobs/{job['job_id']}/result", {"error": "x"}, job["job_token"])

    def test_all_three_telemetry_invariants_pass(self):
        self.set_tel({"verify_error_spike": True, "traffic": {"service": "api", "port": 8080, "path": "/x"}})
        _, exp = self.run_with(self.good_evidence())
        r = self.rules(exp)
        self.assertEqual((r["telemetry_traces_recorded"], r["telemetry_error_spike_detected"], r["telemetry_latency_recovered"]),
                         ("PASS", "PASS", "PASS"))
        self.assertEqual(exp["oracle_verdict"], "PASS")

    def test_slow_recovery_and_invisible_fault_fail(self):
        self.set_tel({"max_allowed_p95_ms": 100, "verify_error_spike": True, "traffic": {"service": "api", "port": 8080, "path": "/x"}})
        _, exp = self.run_with(self.good_evidence(error_spans_count=0, latency_ms={"p95": 900}))
        r = self.rules(exp)
        self.assertEqual((r["telemetry_error_spike_detected"], r["telemetry_latency_recovered"]), ("FAIL", "FAIL"))
        self.assertEqual(exp["oracle_verdict"], "FAIL")

    def test_collection_error_is_skip_not_fail(self):
        self.set_tel({"verify_error_spike": True, "traffic": {"service": "api", "port": 8080, "path": "/x"}})
        _, exp = self.run_with({"enabled": True, "available": False, "collection_error": "this agent has no tracing source configured (--jaeger)"})
        r = self.rules(exp)
        self.assertEqual((r["telemetry_traces_recorded"], r["telemetry_error_spike_detected"], r["telemetry_latency_recovered"]),
                         ("SKIP", "SKIP", "SKIP"))
        self.assertEqual(exp["oracle_verdict"], "PASS")                        # skips never fail the run...
        self.assertEqual(exp["result"]["oracle"]["counts"]["skip"], 3)         # ...and are reported as skips

    def test_not_configured_stays_not_applicable(self):
        _, exp = self.run_with(self.good_evidence())
        r = self.rules(exp)
        self.assertEqual((r["telemetry_traces_recorded"], r["telemetry_latency_recovered"]), ("N/A", "N/A"))

    def test_heartbeat_reports_tracing_source(self):
        self.agent.poll(telemetry_source="default/jaeger:16686")
        c = self.user.get(f"/api/clusters/{self.agent.cluster_id}")[1]["cluster"]
        self.assertEqual(c["telemetry_source"], "default/jaeger:16686")
        self.agent.poll(telemetry_source="http://evil:1/; rm")                 # junk is ignored
        self.assertEqual(self.user.get(f"/api/clusters/{self.agent.cluster_id}")[1]["cluster"]["telemetry_source"],
                         "default/jaeger:16686")


if __name__ == "__main__":
    unittest.main()
