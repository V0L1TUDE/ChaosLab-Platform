"""
Agent execution backend
=======================
Live execution on a customer's own cluster. `run()` enqueues a job for the connected agent and waits for
it; the agent streams progress through the agent API (see routes_agent.py) and finally posts raw EVIDENCE.
The server then evaluates that evidence with its OWN Verification Oracle (the agent's verdict is not
trusted) and returns the same normalized result structure as every other backend.
"""

import time
from typing import Any, Dict

from . import clusters, config, db, engine_bridge, jobs
from .execution import ExecutionBackend, ExecutionError, ProgressSink, normalize_oracle
from .live_backend import LiveBackend

DATA_SOURCE_AGENT = "LIVE: collected by the ChaosLab agent running in your own Kubernetes cluster"
DATA_SOURCE_SHARED = "LIVE: collected by the ChaosLab agent on the shared demo Kubernetes cluster"


class AgentBackend(ExecutionBackend):
    mode = "live"

    def run(self, spec: Dict[str, Any], sink: ProgressSink) -> Dict[str, Any]:
        cluster_id = spec["cluster_id"]
        cfg = spec["engine_config"]
        # Generous bound: engine timeout + observation + telemetry waits.
        runtime = int(cfg.get("recovery", {}).get("timeout_seconds", 60)) * 2 + 180
        with db.session() as conn:
            name = conn.execute("SELECT name FROM clusters WHERE id = ?", (cluster_id,)).fetchone()[0]
        try:
            job_id = jobs.enqueue(spec["experiment_id"], cluster_id, {"engine_config": cfg, "max_runtime_seconds": runtime})
        except jobs.JobRefused as e:
            raise ExecutionError(str(e))
        clusters.audit(spec["user_id"], cluster_id, "system", "job_queued",
                       f"experiment {spec['experiment_id']}: {cfg['fault']['type']} on {cfg['target']['namespace']}/"
                       f"{cfg['target']['label'].get('app')}")
        sink.event("baseline", f"Job queued for the ChaosLab agent on cluster '{name}'. Waiting for it to pick the job up...", "info", 3)

        queue_deadline = time.time() + config.AGENT_QUEUE_TIMEOUT_SECONDS
        cancel_seen = None
        while True:
            job = jobs.get(job_id)
            st = job["status"]
            if st == "done":
                break
            if st == "failed":
                raise ExecutionError(job["error"] or "The agent reported a failure.")
            if st == "cancelled":
                raise ExecutionError(job["error"] or "Cancelled by emergency stop.")
            now = time.time()
            if st == "queued" and now > queue_deadline:
                jobs.mark(job_id, "expired", "The agent did not pick up the job in time.")
                raise ExecutionError("The ChaosLab agent did not pick up the job in time. Check that it is running and online.")
            if st in ("leased", "running"):
                exp = jobs._parse(job["lease_expires_at"])
                if exp and exp < jobs._now():
                    jobs.mark(job_id, "expired", "The agent did not report a result before the lease expired.")
                    raise ExecutionError("The agent did not report a result in time.")
                if job["cancel_requested"]:
                    cancel_seen = cancel_seen or now
                    if now - cancel_seen > 60:        # agent never acknowledged the stop
                        jobs.mark(job_id, "cancelled", "Emergency stop (agent did not acknowledge).")
                        raise ExecutionError("Cancelled by emergency stop.")
            time.sleep(1)

        import json
        report = json.loads(job["result_json"])
        return self._to_result(spec, sink, report, job_id)

    @staticmethod
    def _to_result(spec: Dict[str, Any], sink: ProgressSink, report: Dict[str, Any], job_id: int) -> Dict[str, Any]:
        if not isinstance(report, dict) or "baseline" not in report or "timestamps" not in report:
            raise ExecutionError("The agent returned malformed evidence.")
        cfg = spec["engine_config"]
        oracle = engine_bridge.oracle().VerificationOracle()
        # Server-side verification: evaluate the agent's evidence with OUR config and OUR Oracle.
        evaluation = oracle.evaluate(report, cfg)
        notes_extra = []
        claimed = (report.get("oracle_evaluation") or {}).get("verdict")
        if claimed and claimed != evaluation["verdict"]:
            notes_extra.append(f"The server re-evaluated the agent's evidence; its verdict ({evaluation['verdict']}) "
                               f"differs from the agent's local verdict ({claimed}).")
        with db.session() as conn:
            rows = conn.execute("SELECT stage, MIN(ts) AS ts FROM experiment_events WHERE experiment_id = ? "
                                "AND stage != 'metric' GROUP BY stage", (spec["experiment_id"],)).fetchall()
        stage_times = {r["stage"]: r["ts"] for r in rows}
        stage_times.setdefault("oracle", db.utcnow())
        stage_times.setdefault("completed", db.utcnow())
        sink.event("oracle", "Server evaluated the agent's evidence with the Verification Oracle.", "info", 97)
        shared = bool(spec.get("shared_guest"))
        result = LiveBackend.build_result(spec, stage_times, {**report, "oracle_evaluation": evaluation},
                                          DATA_SOURCE_SHARED if shared else DATA_SOURCE_AGENT)
        where = "on the shared demo cluster" if shared else "in your cluster"
        result["notes"] = [f"Evidence collected by the ChaosLab agent (job {job_id}) {where} and verified by the "
                           "server-side Oracle."] + notes_extra + [n for n in result["notes"][1:]]
        sink.event("completed", f"Oracle verdict: {evaluation['verdict']}.", "info", 100)
        return result
