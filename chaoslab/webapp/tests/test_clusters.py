"""Connected clusters: enrollment, agent protocol, trust boundaries, kill switch (server side, fake agent)."""

import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from harness import Client
from test_experiments import wait_done

INVENTORY = {"shop": [{"deployment": "web", "app_label": "web", "replicas": 1, "ready": 1},
                      {"deployment": "api", "app_label": "api", "replicas": 2, "ready": 2},
                      {"deployment": "redis", "app_label": "redis", "replicas": 1, "ready": 1}],
             "other-ns": [{"deployment": "secret-thing", "app_label": "secret-thing", "replicas": 1, "ready": 1}]}


def iso(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()


def make_report(service="web", recovered=1, recovery=3.2, claims="PASS"):
    return {"experiment": "x", "baseline": {"pod_name": f"{service}-abc", "ready_replicas": 1, "ready": "1/1", "status": "Running"},
            "timestamps": {"experiment_start": iso(-10), "fault_injected": iso(-8), "original_pod_disappeared": iso(-7),
                           "replacement_ready": iso(-4), "experiment_end": iso()},
            "original_pod": f"{service}-abc", "fault_injected": True, "original_pod_disappeared": True,
            "replacement_pod": f"{service}-def", "replacement_ready": True, "recovered_ready_replicas": recovered,
            "recovery_time_seconds": recovery, "total_recovery_seconds": recovery + 1,
            "application": {"enabled": False}, "telemetry": {"enabled": False},
            "chaos_mesh_evidence": {"conditions": [{"type": "AllInjected", "status": "True"}]}, "kubernetes_events": [],
            "oracle_evaluation": {"verdict": claims}}


class AgentSim:
    """A hand-driven fake agent speaking the real HTTP protocol."""

    def __init__(self, user_client, name="Prod", uid=None, namespaces=("shop", "other-ns")):
        self.user = user_client
        status, data, _ = user_client.post("/api/clusters", {"name": name})
        assert status == 201, data
        self.cluster_id = data["cluster"]["id"]
        self.token = data["enrollment"]["token"]
        self.uid, self.namespaces = uid or ("uid-" + uuid.uuid4().hex), list(namespaces)
        self.http = Client()           # cookie-less: agents authenticate with bearer tokens only
        self.cred = None

    def call(self, path, body, bearer=None):
        h = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        return self.http.request("POST", path, body, headers=h)

    def enroll(self, token=None, **over):
        body = {"token": token or self.token, "agent_version": "1.0.0", "cluster_uid": self.uid, "kube_version": "v1.30.0",
                "allowed_namespaces": self.namespaces, **over}
        status, data, _ = self.call("/agent/v1/enroll", body)
        if status == 201:
            self.cred = data["credential"]
        return status, data

    def poll(self, **over):
        body = {"agent_version": "1.0.0", "cluster_uid": self.uid, "allowed_namespaces": self.namespaces,
                "inventory": INVENTORY, "busy": False, "wait": 0, **over}
        return self.call("/agent/v1/poll", body, self.cred)

    def wait_job(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            status, data, _ = self.poll()
            if data.get("job"):
                return data["job"]
            time.sleep(0.2)
        raise AssertionError("no job arrived")


def connect(user_client, **kw):
    agent = AgentSim(user_client, **kw)
    assert agent.enroll()[0] == 201
    assert agent.poll()[0] == 200          # sends the inventory
    return agent


def register_app(user_client, agent, namespace="shop", name="Shop"):
    status, data, _ = user_client.post(f"/api/clusters/{agent.cluster_id}/applications", {"name": name, "namespace": namespace})
    assert status == 201, data
    return data["application_id"]


def start_experiment(user_client, app_id, service="web", fault="pod-kill", mode="live"):
    return user_client.post("/api/experiments", {"application_id": app_id, "target_service": service, "fault_type": fault,
                                                 "duration_seconds": 30, "mode": mode})


class ClusterBase(unittest.TestCase):
    def setUp(self):
        from webapp import config
        self.config, self.saved_q = config, config.AGENT_QUEUE_TIMEOUT_SECONDS
        self.user = Client()
        self.user.register()

    def tearDown(self):
        self.config.AGENT_QUEUE_TIMEOUT_SECONDS = self.saved_q


class EnrollmentTests(ClusterBase):
    def test_enrollment_lifecycle(self):
        agent = AgentSim(self.user)
        c = self.user.get(f"/api/clusters/{agent.cluster_id}")[1]["cluster"]
        self.assertEqual((c["state"], c["online"]), ("pending", False))
        self.assertTrue(agent.token.startswith("cle_"))
        self.assertEqual(agent.enroll(token="cle_wrong")[0], 401)
        status, data = agent.enroll()
        self.assertEqual(status, 201)
        self.assertTrue(data["credential"].startswith("cla_"))
        self.assertEqual(agent.enroll()[0], 401)                       # one-time token
        agent.poll()
        c = self.user.get(f"/api/clusters/{agent.cluster_id}")[1]["cluster"]
        self.assertEqual((c["state"], c["online"], c["allowed_namespaces"]), ("online", True, ["other-ns", "shop"]))
        self.assertEqual(sorted(c["inventory"]), ["other-ns", "shop"])
        self.assertNotIn("credential", str(c))
        from webapp import db
        with db.session() as conn:      # only hashes at rest
            row = conn.execute("SELECT * FROM clusters WHERE id = ?", (agent.cluster_id,)).fetchone()
        self.assertNotIn(agent.cred, str(dict(row)))
        self.assertIsNone(row["enroll_token_hash"])

    def test_enrollment_validation_and_expiry(self):
        agent = AgentSim(self.user)
        self.assertEqual(agent.enroll(allowed_namespaces=[])[0], 400)               # explicit allow-list required
        self.assertEqual(agent.enroll(allowed_namespaces=["Bad_NS"])[0], 400)
        self.assertEqual(agent.enroll(cluster_uid="x")[0], 400)
        from webapp import db
        with db.session() as conn:
            conn.execute("UPDATE clusters SET enroll_expires_at = ? WHERE id = ?", (iso(-5), agent.cluster_id))
        self.assertEqual(agent.enroll()[0], 401)                                    # expired
        token = self.user.post(f"/api/clusters/{agent.cluster_id}/enrollment")[1]["enrollment"]["token"]
        self.assertEqual(agent.enroll(token=token)[0], 201)

    def test_one_account_per_cluster_identity(self):
        connect(self.user, uid="uid-shared-cluster-01")
        other = Client(); other.register()
        agent2 = AgentSim(other, uid="uid-shared-cluster-01")
        status, data = agent2.enroll()
        self.assertEqual(status, 409)
        self.assertIn("already connected", data["error"])

    def test_credential_from_wrong_cluster_is_rejected_and_audited(self):
        agent = connect(self.user)
        status, _, _ = agent.poll(cluster_uid="uid-some-other-cluster")
        self.assertEqual(status, 403)
        audit = self.user.get(f"/api/clusters/{agent.cluster_id}")[1]["cluster"]["audit"]
        self.assertIn("cluster_uid_mismatch", [a["action"] for a in audit])

    def test_auth_required_on_agent_api(self):
        agent = connect(self.user)
        for bearer in (None, "cla_nope", "clj_notacluster", agent.cred + "x"):
            self.assertEqual(agent.call("/agent/v1/poll", {}, bearer)[0], 401)

    def test_inventory_only_kept_for_allowed_namespaces(self):
        agent = AgentSim(self.user, namespaces=("shop",))
        agent.enroll()
        agent.poll(inventory=INVENTORY)            # includes other-ns, which the agent did not allow
        inv = self.user.get(f"/api/clusters/{agent.cluster_id}")[1]["cluster"]["inventory"]
        self.assertEqual(list(inv), ["shop"])

    def test_cluster_limit_and_isolation(self):
        agent = connect(self.user)
        other = Client(); other.register()
        cid = agent.cluster_id
        self.assertEqual(other.get(f"/api/clusters/{cid}")[0], 404)
        self.assertEqual(other.post(f"/api/clusters/{cid}/emergency-stop")[0], 404)
        self.assertEqual(other.post(f"/api/clusters/{cid}/enrollment")[0], 404)
        self.assertEqual(other.delete(f"/api/clusters/{cid}")[0], 404)
        self.assertEqual(other.post(f"/api/clusters/{cid}/applications", {"name": "x", "namespace": "shop"})[0], 404)
        self.assertEqual(other.get("/api/clusters")[1]["clusters"], [])
        self.assertEqual(Client().get("/api/clusters")[0], 401)


class RegistrationTests(ClusterBase):
    def test_targets_must_be_explicitly_registered_in_allowed_namespaces(self):
        agent = connect(self.user, namespaces=("shop",))
        c = self.user
        self.assertEqual(c.post(f"/api/clusters/{agent.cluster_id}/applications", {"name": "x", "namespace": "kube-system"})[0], 403)
        self.assertEqual(c.post(f"/api/clusters/{agent.cluster_id}/applications", {"name": "x", "namespace": "BAD NS"})[0], 400)
        app_id = register_app(c, agent)
        self.assertEqual(c.post(f"/api/clusters/{agent.cluster_id}/applications", {"name": "dup", "namespace": "shop"})[0], 409)
        pending = AgentSim(self.user, uid="uid-pending-cluster-02")
        self.assertEqual(c.post(f"/api/clusters/{pending.cluster_id}/applications", {"name": "x", "namespace": "shop"})[0], 409)
        detail = c.get(f"/api/applications/{app_id}")[1]["application"]
        self.assertEqual(detail["catalog"]["discovery"]["source"], "agent")
        names = {s["name"]: s for s in detail["catalog"]["services"]}
        self.assertEqual(set(names), {"web", "api", "redis"})                  # from the agent's inventory
        self.assertEqual(detail["catalog"]["targetable_services"], ["api", "web"])   # datastore protected
        self.assertEqual((names["api"]["ready"], names["api"]["replicas"]), (2, 2))


class ExecutionTests(ClusterBase):
    def setUp(self):
        super().setUp()
        self.agent = connect(self.user)
        self.app_id = register_app(self.user, self.agent)

    def run_job(self, report=None, error=None, service="web"):
        status, data, _ = start_experiment(self.user, self.app_id, service)
        self.assertEqual(status, 201, data)
        exp_id = data["experiment"]["id"]
        job = self.agent.wait_job()
        self.assertEqual(job["experiment_id"], exp_id)
        tok = job["job_token"]
        self.agent.call(f"/agent/v1/jobs/{job['job_id']}/events", {
            "events": [{"stage": "baseline", "message": "baseline ok", "percent": 10},
                       {"stage": "fault_injection", "message": "injected", "percent": 30},
                       {"stage": "evil", "message": "ignored: unknown stage"}],
            "samples": [{"t": 1.0, "replicas_ready": 1, "replicas_total": 1}]}, tok)
        body = {"error": error} if error else {"report": report or make_report(service)}
        self.assertEqual(self.agent.call(f"/agent/v1/jobs/{job['job_id']}/result", body, tok)[0], 200)
        return exp_id, job

    def test_full_job_flow_server_verifies_evidence(self):
        exp_id, job = self.run_job()
        exp = wait_done(self.user, exp_id)
        self.assertEqual(exp["status"], "completed", exp)
        self.assertEqual(exp["mode"], "live")
        self.assertFalse(exp["simulated"])
        self.assertEqual(exp["cluster"]["name"], "Prod")
        r = exp["result"]
        self.assertIn("ChaosLab agent", r["data_source"])
        self.assertEqual(r["target"]["namespace"], "shop")
        self.assertEqual(exp["oracle_verdict"], "PASS")
        statuses = {x["rule"]: x["status_label"] for x in r["oracle"]["rules"]}
        self.assertEqual(statuses["service_recovery"], "PASS")
        self.assertEqual(statuses["app_health_probe"], "N/A")                # no probes for customer apps
        self.assertEqual([e["message"] for e in exp["events"] if e["stage"] in ("baseline", "fault_injection")][-2:],
                         ["baseline ok", "injected"])
        self.assertNotIn("ignored", str(exp["events"]))                      # unknown stages dropped
        self.assertTrue(exp["samples"])
        self.assertEqual(sorted(exp["reports"]), ["html", "json"])
        rep = self.user.request("GET", f"/api/experiments/{exp_id}/report?format=json")[1]
        self.assertEqual(rep["cluster"]["name"], "Prod")
        self.assertFalse(rep["simulated"])
        # job tokens die with the job
        self.assertEqual(self.agent.call(f"/agent/v1/jobs/{job['job_id']}/events", {"events": []}, job["job_token"])[0], 401)
        self.assertEqual(self.agent.call(f"/agent/v1/jobs/{job['job_id']}/result", {"error": "x"}, job["job_token"])[0], 401)
        actions = [a["action"] for a in self.user.get(f"/api/clusters/{self.agent.cluster_id}")[1]["cluster"]["audit"]]
        for a in ("cluster_created", "agent_enrolled", "application_registered", "job_queued", "job_leased", "job_completed"):
            self.assertIn(a, actions)

    def test_lying_agent_is_overruled_by_server_oracle(self):
        exp_id, _ = self.run_job(report=make_report(recovered=0, claims="PASS"))      # evidence says: did NOT recover
        exp = wait_done(self.user, exp_id)
        self.assertEqual(exp["status"], "completed")
        self.assertEqual(exp["oracle_verdict"], "FAIL")
        failed = [x["rule"] for x in exp["result"]["oracle"]["rules"] if x["status"] == "FAIL"]
        self.assertIn("service_recovery", failed)
        self.assertTrue(any("differs from the agent" in n for n in exp["result"]["notes"]))

    def test_malformed_evidence_and_agent_error(self):
        exp_id, _ = self.run_job(report={"junk": True})
        exp = wait_done(self.user, exp_id)
        self.assertEqual(exp["status"], "failed")
        self.assertIn("malformed", exp["error"])
        exp_id, _ = self.run_job(error="Rejected by the agent's local policy: nope")
        exp = wait_done(self.user, exp_id)
        self.assertEqual((exp["status"], exp["oracle_verdict"]), ("failed", None))
        self.assertIn("local policy", exp["error"])

    def test_job_tokens_are_scoped_to_their_job(self):
        status, data, _ = start_experiment(self.user, self.app_id)
        job1 = self.agent.wait_job()
        # a second cluster + job: its token must not work on job1, and job1's must not work elsewhere
        other = connect(self.user, name="Staging", uid="uid-staging-000000002")
        other_app = register_app(self.user, other, name="Staging shop")
        start_experiment(self.user, other_app)
        job2 = other.wait_job()
        for path_job, tok in ((job1["job_id"], job2["job_token"]), (job2["job_id"], job1["job_token"])):
            self.assertEqual(self.agent.call(f"/agent/v1/jobs/{path_job}/events", {"events": []}, tok)[0], 401)
        self.assertEqual(self.agent.call(f"/agent/v1/jobs/{job1['job_id']}/events", {"events": []}, self.agent.cred)[0], 401)  # cred != job token
        self.assertEqual(self.agent.call(f"/agent/v1/jobs/{job1['job_id']}/events", {"events": []}, job1["job_token"])[0], 200)

    def test_one_active_job_per_cluster(self):
        self.assertEqual(start_experiment(self.user, self.app_id)[0], 201)
        status, data, _ = start_experiment(self.user, self.app_id, "api")
        self.assertEqual(status, 409)
        self.assertIn("already running", data["error"])

    def test_validation_uses_agent_inventory(self):
        self.assertEqual(start_experiment(self.user, self.app_id, "redis")[0], 400)           # datastore
        self.assertEqual(start_experiment(self.user, self.app_id, "secret-thing")[0], 400)    # other namespace
        self.assertEqual(start_experiment(self.user, self.app_id, mode="turbo")[0], 400)

    def test_simulated_mode_still_works_without_agent_involvement(self):
        status, data, _ = start_experiment(self.user, self.app_id, mode="simulated")
        self.assertEqual(status, 201, data)
        exp = wait_done(self.user, data["experiment"]["id"])
        self.assertTrue(exp["simulated"])
        self.assertIsNone(exp["cluster"])
        self.assertEqual(self.agent.poll()[1]["job"], None)                                   # no job was queued

    def test_agent_offline_blocks_live(self):
        from webapp import db
        with db.session() as conn:
            conn.execute("UPDATE clusters SET last_seen = ? WHERE id = ?", (iso(-600), self.agent.cluster_id))
        status, data, _ = start_experiment(self.user, self.app_id)
        self.assertEqual(status, 503)
        self.assertIn("offline", data["error"])
        self.assertEqual(self.user.get(f"/api/clusters/{self.agent.cluster_id}")[1]["cluster"]["state"], "offline")

    def test_job_not_picked_up_times_out(self):
        self.config.AGENT_QUEUE_TIMEOUT_SECONDS = 2
        status, data, _ = start_experiment(self.user, self.app_id)
        exp = wait_done(self.user, data["experiment"]["id"], timeout=20)
        self.assertEqual(exp["status"], "failed")
        self.assertIn("did not pick up", exp["error"])


class KillSwitchTests(ClusterBase):
    def setUp(self):
        super().setUp()
        self.agent = connect(self.user)
        self.app_id = register_app(self.user, self.agent)

    def wait_for_queued_job(self, timeout=5):
        from webapp import jobs
        end = time.time() + timeout
        while time.time() < end:
            if jobs.active_for_cluster(self.agent.cluster_id):
                return
            time.sleep(0.05)
        self.fail("job was never enqueued")

    def test_paused_poll_waits_instead_of_spinning(self):
        self.user.post(f"/api/clusters/{self.agent.cluster_id}/emergency-stop")
        started = time.time()
        status, data, _ = self.agent.poll(wait=1.5)
        self.assertEqual(status, 200)
        self.assertTrue(data["paused"])
        self.assertGreaterEqual(time.time() - started, 1.2)          # it held the request open (long-poll)
        self.user.post(f"/api/clusters/{self.agent.cluster_id}/resume")

    def test_paused_cluster_refuses_new_jobs_even_if_stop_races_the_enqueue(self):
        from webapp import jobs
        self.user.post(f"/api/clusters/{self.agent.cluster_id}/emergency-stop")
        with self.assertRaises(jobs.JobRefused):
            jobs.enqueue(1, self.agent.cluster_id, {"engine_config": {}})
        self.user.post(f"/api/clusters/{self.agent.cluster_id}/resume")

    def test_emergency_stop_cancels_queued_job_and_blocks_new_ones(self):
        status, data, _ = start_experiment(self.user, self.app_id)
        exp_id = data["experiment"]["id"]
        self.wait_for_queued_job()
        s, d, _ = self.user.post(f"/api/clusters/{self.agent.cluster_id}/emergency-stop")
        self.assertEqual((s, d["jobs_affected"]), (200, 1))
        exp = wait_done(self.user, exp_id)
        self.assertEqual(exp["status"], "failed")
        self.assertIn("emergency stop", exp["error"].lower())
        self.assertEqual(self.user.get(f"/api/clusters/{self.agent.cluster_id}")[1]["cluster"]["state"], "paused")
        status, data, _ = start_experiment(self.user, self.app_id)
        self.assertEqual(status, 409)
        reply = self.agent.poll()[1]
        self.assertTrue(reply["paused"])
        self.assertIsNone(reply["job"])
        self.assertEqual(self.user.post(f"/api/clusters/{self.agent.cluster_id}/resume")[0], 200)
        self.assertEqual(start_experiment(self.user, self.app_id)[0], 201)
        actions = [a["action"] for a in self.user.get(f"/api/clusters/{self.agent.cluster_id}")[1]["cluster"]["audit"]]
        self.assertIn("emergency_stop", actions)
        self.assertIn("resume", actions)

    def test_emergency_stop_aborts_running_job(self):
        status, data, _ = start_experiment(self.user, self.app_id)
        exp_id = data["experiment"]["id"]
        job = self.agent.wait_job()
        self.assertFalse(self.agent.call(f"/agent/v1/jobs/{job['job_id']}/events", {"events": []}, job["job_token"])[1]["cancel"])
        self.user.post(f"/api/clusters/{self.agent.cluster_id}/emergency-stop")
        self.assertEqual(self.agent.poll(busy=True)[1]["cancel"], [job["job_id"]])       # delivered on the next poll
        self.assertTrue(self.agent.call(f"/agent/v1/jobs/{job['job_id']}/events", {"events": []}, job["job_token"])[1]["cancel"])
        self.agent.call(f"/agent/v1/jobs/{job['job_id']}/result", {"cancelled": True}, job["job_token"])
        exp = wait_done(self.user, exp_id)
        self.assertEqual(exp["status"], "failed")
        self.assertIn("emergency stop", exp["error"].lower())

    def test_revoke_and_regenerate_cut_off_the_agent(self):
        old = self.agent.cred
        token = self.user.post(f"/api/clusters/{self.agent.cluster_id}/enrollment")[1]["enrollment"]["token"]
        self.assertEqual(self.agent.poll()[0], 401)                               # old credential is dead
        self.assertEqual(self.agent.enroll(token=token)[0], 201)
        self.assertNotEqual(old, self.agent.cred)
        self.assertEqual(self.agent.poll()[0], 200)
        self.assertEqual(self.user.delete(f"/api/clusters/{self.agent.cluster_id}")[0], 200)
        self.assertEqual(self.agent.poll()[0], 401)
        self.assertEqual(start_experiment(self.user, self.app_id)[0], 400)        # app no longer executable
        listing = self.user.get("/api/clusters")[1]["clusters"][0]
        self.assertEqual(listing["state"], "revoked")
        # history is kept; identity is freed so the cluster can be connected again
        self.assertEqual(self.user.get(f"/api/applications/{self.app_id}")[0], 200)
        again = AgentSim(self.user)
        self.assertEqual(again.enroll()[0], 201)

    def test_enrollment_attempts_are_throttled(self):
        from webapp import clusters
        clusters._attempts.clear()
        agent = AgentSim(self.user, uid="uid-throttle-00000003")
        codes = [agent.enroll(token="cle_bad")[0] for _ in range(14)]
        self.assertEqual(codes[0], 401)
        self.assertEqual(codes[-1], 429)
        clusters._attempts.clear()


if __name__ == "__main__":
    unittest.main()
