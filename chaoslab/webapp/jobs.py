"""
Experiment jobs for connected clusters
======================================
Lifecycle:  queued -> leased (agent picked it up) -> running (first evidence) -> done | failed | cancelled | expired

* One active job per cluster. A job is DATA ONLY (a declarative engine config); the agent re-validates it
  against its own local policy and never executes anything the server sends as a command.
* Each lease issues a one-time job token (stored hashed) that only authorises events/results for THAT job,
  and only until the lease expires.
* Cancellation (emergency stop) is delivered to the agent on its next poll / event / result call.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import db
from .execution import STAGE_LABELS, ProgressSink

ACTIVE = ("queued", "leased", "running")
MAX_EVENTS_PER_JOB = 2000
MAX_EVENTS_PER_CALL = 200
MAX_MESSAGE_LEN = 500
_STAGES = set(STAGE_LABELS) | {"failed", "metric"}


def _hash(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


class JobRefused(Exception):
    pass


def enqueue(experiment_id: int, cluster_id: int, spec: Dict[str, Any]) -> int:
    with db.session() as conn:
        c = conn.execute("SELECT paused, status FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
        if not c or c["status"] != "connected":
            raise JobRefused("The cluster is no longer connected.")
        if c["paused"]:
            raise JobRefused("Cancelled by emergency stop.")
        cur = conn.execute("INSERT INTO jobs (experiment_id, cluster_id, status, spec_json, created_at) "
                           "VALUES (?, ?, 'queued', ?, ?)", (experiment_id, cluster_id, json.dumps(spec), db.utcnow()))
        return cur.lastrowid


def get(job_id: int):
    with db.session() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def active_for_cluster(cluster_id: int) -> int:
    with db.session() as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs WHERE cluster_id = ? AND status IN ('queued', 'leased', 'running')",
                            (cluster_id,)).fetchone()[0]


def mark(job_id: int, status: str, error: Optional[str] = None) -> None:
    with db.session() as conn:
        conn.execute("UPDATE jobs SET status = ?, error = COALESCE(?, error), finished_at = ? "
                     "WHERE id = ? AND status IN ('queued', 'leased', 'running')", (status, error, db.utcnow(), job_id))


def cancel_all(cluster_id: int, reason: str) -> int:
    """Cancel queued jobs outright; flag leased/running ones so the agent aborts. Returns affected count."""
    with db.session() as conn:
        q = conn.execute("UPDATE jobs SET status = 'cancelled', error = ?, finished_at = ? WHERE cluster_id = ? AND status = 'queued'",
                         (reason, db.utcnow(), cluster_id)).rowcount
        r = conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE cluster_id = ? AND status IN ('leased', 'running')",
                         (cluster_id,)).rowcount
    return q + r


def cancel_requests(cluster_id: int) -> List[int]:
    with db.session() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE cluster_id = ? AND cancel_requested = 1 AND status IN ('leased', 'running')", (cluster_id,))]


def lease_next(cluster_id: int, agent_busy: bool) -> Optional[Dict[str, Any]]:
    """Hand the oldest queued job to the polling agent (at most one active job per cluster)."""
    with db.session() as conn:
        active = conn.execute("SELECT * FROM jobs WHERE cluster_id = ? AND status IN ('leased', 'running')", (cluster_id,)).fetchall()
        for j in active:
            # The agent says it is idle but we think it holds a job: it restarted or lost it.
            leased = _parse(j["leased_at"])
            if not agent_busy and leased and (_now() - leased).total_seconds() > 15:
                conn.execute("UPDATE jobs SET status = 'failed', error = 'The agent restarted or lost the job.', finished_at = ? WHERE id = ?",
                             (db.utcnow(), j["id"]))
        if conn.execute("SELECT 1 FROM jobs WHERE cluster_id = ? AND status IN ('leased', 'running')", (cluster_id,)).fetchone():
            return None
        job = conn.execute("SELECT * FROM jobs WHERE cluster_id = ? AND status = 'queued' ORDER BY id LIMIT 1", (cluster_id,)).fetchone()
        if not job:
            return None
        spec = json.loads(job["spec_json"])
        token = "clj_" + secrets.token_urlsafe(24)
        expires = (_now() + timedelta(seconds=int(spec.get("max_runtime_seconds", 300)))).isoformat(timespec="seconds")
        cur = conn.execute("UPDATE jobs SET status = 'leased', job_token_hash = ?, leased_at = ?, lease_expires_at = ? "
                           "WHERE id = ? AND status = 'queued'", (_hash(token), db.utcnow(), expires, job["id"]))
        if cur.rowcount != 1:
            return None
    return {"job_id": job["id"], "job_token": token, "experiment_id": job["experiment_id"],
            "engine_config": spec["engine_config"], "max_runtime_seconds": spec.get("max_runtime_seconds", 300)}


def authenticate_job(job_id: Any, authorization: Optional[str], cluster_id: Optional[int] = None):
    """The job row if the bearer token is this job's live token, else None."""
    if not authorization or not authorization.startswith("Bearer clj_") or len(authorization) > 120:
        return None
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return None
    job = get(jid)
    if not job or job["job_token_hash"] != _hash(authorization[len("Bearer "):]):
        return None
    if job["status"] not in ("leased", "running"):
        return None
    exp = _parse(job["lease_expires_at"])
    if not exp or exp < _now():
        return None
    return job


def record_events(job, events: Any, samples: Any) -> bool:
    """Persist agent-reported progress. Returns True when the agent should abort (cancel requested)."""
    exp_id = job["experiment_id"]
    sink = ProgressSink(exp_id, simulated=False)
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM experiment_events WHERE experiment_id = ?", (exp_id,)).fetchone()[0]
        conn.execute("UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ? AND status = 'leased'",
                     (db.utcnow(), job["id"]))
        cancel = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job["id"],)).fetchone()[0]
    budget = max(0, MAX_EVENTS_PER_JOB - n)
    for ev in (events if isinstance(events, list) else [])[:min(MAX_EVENTS_PER_CALL, budget)]:
        if not isinstance(ev, dict) or ev.get("stage") not in _STAGES or ev.get("stage") == "metric":
            continue
        level = ev.get("level") if ev.get("level") in ("info", "warn", "error") else "info"
        pct = ev.get("percent")
        sink.event(ev["stage"], str(ev.get("message", ""))[:MAX_MESSAGE_LEN], level,
                   pct if isinstance(pct, int) and 0 <= pct <= 100 else None)
    for s in (samples if isinstance(samples, list) else [])[:50]:
        if isinstance(s, dict) and isinstance(s.get("t"), (int, float)) and isinstance(s.get("replicas_ready"), int):
            sink.sample({"t": s["t"], "replicas_ready": s["replicas_ready"],
                         "replicas_total": s.get("replicas_total") if isinstance(s.get("replicas_total"), int) else None,
                         "simulated": False})
    return bool(cancel)


def finish(job, *, report: Optional[Dict[str, Any]] = None, error: Optional[str] = None, cancelled: bool = False) -> None:
    status = "cancelled" if cancelled else ("failed" if error else "done")
    with db.session() as conn:
        conn.execute("UPDATE jobs SET status = ?, result_json = ?, error = ?, finished_at = ? "
                     "WHERE id = ? AND status IN ('leased', 'running')",
                     (status, json.dumps(report) if report is not None else None, (error or None), db.utcnow(), job["id"]))
