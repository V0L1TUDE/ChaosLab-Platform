"""
Agent API (/agent/v1/*): used ONLY by the ChaosLab agent running in a customer's cluster.
Bearer-token authenticated (no cookies, so no CSRF surface). The agent only ever makes outbound calls here.

  POST /agent/v1/enroll             one-time token  -> long-lived credential
  POST /agent/v1/poll               credential      -> heartbeat + inventory in, (job | cancel list | paused) out
  POST /agent/v1/jobs/<id>/events   job token       -> progress + samples in, {cancel} out
  POST /agent/v1/jobs/<id>/result   job token       -> raw evidence (or error / cancelled) in
"""

import time

from . import clusters, config, db, jobs
from .http_core import HttpError, json_response, router


def _agent(req):
    row = clusters.authenticate_agent(req.headers.get("Authorization"))
    if not row:
        raise HttpError(401, "Invalid or revoked agent credential.")
    return row


@router.route("POST", "/agent/v1/enroll", auth_required=False)
def enroll(req):
    if clusters.throttled(req.ip):
        raise HttpError(429, "Too many enrollment attempts. Try again later.")
    body = req.json()
    try:
        result = clusters.enroll(body.get("token"), body, req.ip)
    except clusters.ClusterError as e:
        clusters.throttled(req.ip, record=True)
        raise HttpError(e.status, str(e))
    return json_response({**result, "poll_interval_seconds": 5, "max_wait_seconds": config.AGENT_MAX_WAIT_SECONDS}, 201)


@router.route("POST", "/agent/v1/poll", auth_required=False)
def poll(req):
    row = _agent(req)
    body = req.json()
    try:
        clusters.record_heartbeat(row, body, req.ip)
    except clusters.ClusterError as e:
        raise HttpError(e.status, str(e))
    try:
        wait = max(0, min(float(body.get("wait", 0)), config.AGENT_MAX_WAIT_SECONDS))
    except (TypeError, ValueError):
        wait = 0
    busy = bool(body.get("busy"))
    deadline = time.time() + wait
    job, cancel, paused = None, [], False
    while True:
        with db.session() as conn:
            current = conn.execute("SELECT paused, status FROM clusters WHERE id = ?", (row["id"],)).fetchone()
        if not current or current["status"] != "connected":
            raise HttpError(401, "Invalid or revoked agent credential.")
        paused = bool(current["paused"])
        cancel = jobs.cancel_requests(row["id"])
        job = None if (paused or busy) else jobs.lease_next(row["id"], busy)
        if job or cancel or time.time() >= deadline:
            break
        time.sleep(0.5)
    if job:
        clusters.audit(row["user_id"], row["id"], "agent", "job_leased", f"job {job['job_id']} experiment {job['experiment_id']}", req.ip)
    return json_response({"job": job, "cancel": cancel, "paused": paused, "poll_interval_seconds": 5})


def _job(req):
    job = jobs.authenticate_job(req.params["id"], req.headers.get("Authorization"))
    if not job:
        raise HttpError(401, "Invalid, expired or finished job token.")
    return job


@router.route("POST", "/agent/v1/jobs/<id>/events", auth_required=False)
def job_events(req):
    job = _job(req)
    body = req.json()
    cancel = jobs.record_events(job, body.get("events"), body.get("samples"))
    with db.session() as conn:
        conn.execute("UPDATE clusters SET last_seen = ? WHERE id = ?", (db.utcnow(), job["cluster_id"]))
    return json_response({"cancel": cancel})


@router.route("POST", "/agent/v1/jobs/<id>/result", auth_required=False)
def job_result(req):
    job = _job(req)
    body = req.json()
    with db.session() as conn:
        owner = conn.execute("SELECT user_id FROM clusters WHERE id = ?", (job["cluster_id"],)).fetchone()["user_id"]
    if body.get("cancelled"):
        jobs.finish(job, cancelled=True, error="Cancelled by emergency stop.")
        clusters.audit(owner, job["cluster_id"], "agent", "job_cancelled", f"job {job['id']}", req.ip)
    elif body.get("error"):
        jobs.finish(job, error=str(body["error"])[:1000])
        clusters.audit(owner, job["cluster_id"], "agent", "job_failed", str(body["error"])[:200], req.ip)
    elif isinstance(body.get("report"), dict):
        jobs.finish(job, report=body["report"])
        clusters.audit(owner, job["cluster_id"], "agent", "job_completed", f"job {job['id']}", req.ip)
    else:
        raise HttpError(400, "Provide 'report', 'error' or 'cancelled'.")
    return json_response({"ok": True})
