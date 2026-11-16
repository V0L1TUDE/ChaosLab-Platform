"""The real agent code (chaoslab/agent/agent.py): local policy, transport rules, and an end-to-end loop
against the test server with a fake executor (the engine itself is exercised on a real cluster separately)."""

import os
import sys
import tempfile
import threading
import time
import unittest

from harness import CHAOSLAB_DIR, PORT, Client, start
from test_clusters import INVENTORY, make_report, register_app, start_experiment
from test_experiments import wait_done

sys.path.insert(0, os.path.join(CHAOSLAB_DIR, "agent"))
import agent as agent_mod  # noqa: E402


def job(ns="shop", service="web", fault="pod-kill", **fault_extra):
    cfg = {"name": "chaoslab-exp-1-web-pod-kill", "target": {"namespace": ns, "label": {"app": service}},
           "fault": {"type": fault, "mode": "one", **fault_extra}, "recovery": {"timeout_seconds": 30, "expected_ready_replicas": 1}}
    return {"job_id": 1, "job_token": "clj_x", "engine_config": cfg, "max_runtime_seconds": 300}


POLICY = lambda **kw: agent_mod.Policy(kw.pop("ns", ["shop"]), **kw)


class PolicyTests(unittest.TestCase):
    def reject(self, j, fragment, policy=None):
        cfg, reason = agent_mod.validate_job(j, policy or POLICY())
        self.assertIsNone(cfg)
        self.assertIn(fragment, reason)

    def test_valid_job_is_rebuilt_from_whitelist(self):
        j = job()
        j["engine_config"]["evil"] = {"exec": ["rm", "-rf", "/"]}
        j["engine_config"]["application"] = {"enabled": True, "health_probe": {"type": "exec", "command": ["sh", "-c", "id"]}}
        j["engine_config"]["telemetry"] = {"enabled": True, "query_endpoint": "http://169.254.169.254"}
        j["engine_config"]["fault"]["inject"] = "$(whoami)"
        cfg, reason = agent_mod.validate_job(j, POLICY())
        self.assertIsNone(reason)
        self.assertEqual(cfg["application"], {"enabled": False})       # probes (could be exec) are never run
        self.assertEqual(cfg["telemetry"], {"enabled": False})
        self.assertNotIn("evil", cfg)
        self.assertEqual(set(cfg["fault"]), {"type", "mode"})
        self.assertEqual(cfg["target"], {"namespace": "shop", "label": {"app": "web"}})

    def test_namespace_allow_list_is_enforced_locally(self):
        self.reject(job(ns="kube-system"), "allow-list")
        self.reject(job(ns="shop2"), "allow-list")
        self.assertIsNone(agent_mod.validate_job(job(ns="b"), POLICY(ns=["a", "b"]))[1])

    def test_target_selector_must_be_a_single_app_label(self):
        j = job(); j["engine_config"]["target"]["label"] = {"app": "web", "tier": "x"}
        self.reject(j, "single 'app' label")
        j = job(); j["engine_config"]["target"]["label"] = {}
        self.reject(j, "single 'app' label")
        j = job(); j["engine_config"]["target"]["label"] = {"app": "x; rm -rf /"}
        self.reject(j, "single 'app' label")
        j = job(); j["engine_config"]["target"]["label"] = {"app": "web,env=prod"}
        self.reject(j, "single 'app' label")

    def test_fault_allow_list_and_limits(self):
        self.reject(job(fault="container-kill"), "not allowed")
        self.reject(job(fault="network-corrupt", duration="10s"), "not allowed")
        self.reject(job(fault="pod-kill"), "not allowed", POLICY(faults=("network-delay",)))
        self.reject(job(fault="network-delay", duration="999s"), "duration")
        self.reject(job(fault="network-delay", duration="forever"), "duration")
        self.reject(job(fault="network-delay", duration="10s", latency="1; reboot"), "invalid latency")
        self.reject(job(fault="network-loss", duration="10s", loss_pct="1000"), "loss_pct")
        j = job(); j["engine_config"]["fault"]["mode"] = "all"
        self.reject(j, "mode 'one'")
        ok = agent_mod.validate_job(job(fault="network-delay", duration="30s", latency="200ms", jitter="10ms"), POLICY())
        self.assertIsNone(ok[1])
        ok = agent_mod.validate_job(job(fault="network-loss", duration="30s", loss_pct="100"), POLICY())
        self.assertEqual(ok[0]["fault"]["loss_pct"], "100")

    def test_recovery_and_runtime_bounds_and_names(self):
        j = job(); j["engine_config"]["recovery"]["timeout_seconds"] = 100000
        self.reject(j, "recovery")
        j = job(); j["max_runtime_seconds"] = 999999
        self.reject(j, "max_runtime")
        j = job(); j["engine_config"]["name"] = "Bad Name; id"
        self.reject(j, "name")
        self.reject({"engine_config": "nope"}, "object")
        self.reject({}, "malformed")

    def test_policy_requires_valid_namespaces(self):
        with self.assertRaises(ValueError):
            agent_mod.Policy([])
        with self.assertRaises(ValueError):
            agent_mod.Policy(["Bad_NS"])


class TransportTests(unittest.TestCase):
    def test_plain_http_is_refused_except_localhost(self):
        with self.assertRaises(ValueError):
            agent_mod.Api("http://chaoslab.example.com")
        with self.assertRaises(ValueError):
            agent_mod.Api("ftp://x")
        agent_mod.Api("https://chaoslab.example.com")
        agent_mod.Api("http://localhost:8090")
        agent_mod.Api("http://127.0.0.1:8090")
        agent_mod.Api("http://lab.internal", allow_http=True)

    def test_credential_is_not_replayed_to_a_different_server(self):
        d = tempfile.mkdtemp()
        f = os.path.join(d, "agent.json")
        a = agent_mod.Agent(agent_mod.Api("http://127.0.0.1:1"), POLICY(), f)
        a.cred = {"server": a.api.base, "credential": "cla_secret", "cluster_id": 1}
        a.save_state()
        same = agent_mod.Agent(agent_mod.Api("http://127.0.0.1:1"), POLICY(), f)
        same.load_state()
        self.assertEqual(same.cred["credential"], "cla_secret")
        other = agent_mod.Agent(agent_mod.Api("http://127.0.0.1:2"), POLICY(), f)
        other.load_state()
        self.assertIsNone(other.cred)
        if os.name != "nt":
            self.assertEqual(os.stat(f).st_mode & 0o777, 0o600)


class PausedLoopTests(unittest.TestCase):
    def test_paused_agent_backs_off_and_logs_once(self):
        """Regression: a paused cluster made the agent spin and print the pause message hundreds of times."""
        replies = [{"paused": True, "job": None, "cancel": []}] * 3 + [{"paused": False, "job": None, "cancel": []}]
        posts = []

        class FakeApi:
            base = "http://127.0.0.1:1"

            def post(self, path, body, bearer=None, timeout=40):
                posts.append(path)
                return replies[min(len(posts) - 1, len(replies) - 1)]
        a = agent_mod.Agent(FakeApi(), agent_mod.Policy(["shop"]), os.path.join(tempfile.mkdtemp(), "a.json"),
                            cluster_probe=lambda: ("uid-paused-test-1", "v1"), inventory_fn=lambda ns: {}, poll_wait=0)
        a.cred = {"credential": "cla_x", "cluster_id": 1}
        a.paused_backoff = 0.3
        logged = []
        orig = agent_mod.log
        agent_mod.log = lambda m: logged.append(m)
        try:
            started = time.time()
            for _ in range(4):
                a.step()
            elapsed = time.time() - started
        finally:
            agent_mod.log = orig
        self.assertGreaterEqual(elapsed, 0.8)                                     # 3 paused polls x 0.3 s backoff
        self.assertEqual(sum("Cluster is paused" in m for m in logged), 1)        # logged once, not on every poll
        self.assertEqual(sum("Cluster resumed" in m for m in logged), 1)


class AgentEndToEndTests(unittest.TestCase):
    """The real Agent class talking to the real server over HTTP, with a fake executor."""

    def setUp(self):
        port = start()
        self.user = Client()
        self.user.register()
        status, data, _ = self.user.post("/api/clusters", {"name": "E2E"})
        self.cluster_id, self.token = data["cluster"]["id"], data["enrollment"]["token"]
        self.uid = "uid-e2e-" + str(time.time_ns())
        self.calls = []

        def fake_exec(j, cfg, reporter):
            self.calls.append(cfg)
            reporter.event("baseline", "agent baseline", 10)
            reporter.event("fault_injection", "agent injecting", 30)
            reporter.sample({"t": 1.0, "replicas_ready": 1, "replicas_total": 1})
            reporter.flush()
            return {"report": make_report(cfg["target"]["label"]["app"])}

        self.agent = agent_mod.Agent(
            agent_mod.Api(f"http://127.0.0.1:{port}"), POLICY(ns=["shop", "other-ns"]),
            os.path.join(tempfile.mkdtemp(), "agent.json"), executor=fake_exec,
            cluster_probe=lambda: (self.uid, "v1.30.0"), inventory_fn=lambda ns: INVENTORY, poll_wait=0.5)
        self.agent.enroll(self.token)
        self.thread = threading.Thread(target=self.agent.run_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.agent.stop.set()
        self.thread.join(timeout=10)

    def wait_online(self):
        end = time.time() + 10
        while time.time() < end:
            c = self.user.get(f"/api/clusters/{self.cluster_id}")[1]["cluster"]
            if c["online"] and c["inventory"]:
                return c
            time.sleep(0.2)
        self.fail("agent never came online")

    def test_agent_runs_a_job_end_to_end(self):
        c = self.wait_online()
        self.assertEqual(c["allowed_namespaces"], ["other-ns", "shop"])
        app_id = register_app(self.user, type("A", (), {"cluster_id": self.cluster_id})())
        status, data, _ = start_experiment(self.user, app_id, "api")
        self.assertEqual(status, 201, data)
        exp = wait_done(self.user, data["experiment"]["id"], timeout=30)
        self.assertEqual(exp["status"], "completed", exp)
        self.assertEqual(exp["oracle_verdict"], "PASS")
        self.assertEqual(self.calls[0]["target"], {"namespace": "shop", "label": {"app": "api"}})
        self.assertEqual(self.calls[0]["application"], {"enabled": False})
        msgs = [e["message"] for e in exp["events"]]
        self.assertIn("agent baseline", msgs)
        self.assertTrue(exp["samples"])

    def test_agent_refuses_a_job_outside_its_local_policy(self):
        """Defence in depth: even if the server (wrongly) dispatches a job for a namespace the agent was not
        allowed, the agent refuses it and reports the reason."""
        self.wait_online()
        app_id = register_app(self.user, type("A", (), {"cluster_id": self.cluster_id})())
        # Simulate a compromised/buggy server: re-point the registered app at a namespace the agent does NOT allow.
        from webapp import db
        with db.session() as conn:
            conn.execute("UPDATE applications SET namespace = 'kube-system' WHERE id = ?", (app_id,))
            conn.execute("UPDATE clusters SET allowed_namespaces = ?, inventory_json = ? WHERE id = ?",
                         ('["kube-system","other-ns","shop"]',
                          '{"kube-system": [{"deployment": "coredns", "app_label": "coredns", "replicas": 2, "ready": 2}]}',
                          self.cluster_id))
        status, data, _ = start_experiment(self.user, app_id, "coredns")
        self.assertEqual(status, 201, data)
        exp = wait_done(self.user, data["experiment"]["id"], timeout=30)
        self.assertEqual(exp["status"], "failed")
        self.assertIn("local policy", exp["error"])
        self.assertIn("allow-list", exp["error"])
        self.assertEqual(self.calls, [])                       # nothing was executed

    def test_agent_stops_when_credential_is_revoked(self):
        self.wait_online()
        self.user.delete(f"/api/clusters/{self.cluster_id}")
        self.thread.join(timeout=15)
        self.assertFalse(self.thread.is_alive())               # exits instead of retrying forever


if __name__ == "__main__":
    unittest.main()
