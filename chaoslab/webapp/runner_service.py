"""
Experiment orchestration
========================
Creates experiment records, dispatches them to the selected backend on a worker thread, persists
progress / results, and triggers report generation. Live and Simulated execution stay separate:
the only thing they share is the `ExecutionBackend` contract.
"""

import json
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from . import catalog, config, db, reports
from .execution import ExecutionBackend, ExecutionError, ProgressSink
from .agent_backend import AgentBackend
from .live_backend import LiveBackend, live_capability
from .sim_backend import SimulatedBackend

BACKENDS: Dict[str, ExecutionBackend] = {"live": LiveBackend(), "simulated": SimulatedBackend(), "agent": AgentBackend()}
_USER_MODES = ("live", "simulated")

_live_lock = threading.Lock()
_live_active = False


class ExperimentRejected(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def capabilities() -> Dict[str, Any]:
    live = live_capability()
    return {
        "simulated": {"available": True, "reason": "Predefined demonstration data. No fault is injected."},
        "live": live,
    }


def create_and_start(user: Dict[str, Any], app_row, service: Any, fault_id: Any, duration: Any, mode: Any) -> int:
    global _live_active
    app = catalog.resolve_app(app_row)
    if not app:
        raise ExperimentRejected("This application cannot run experiments. Upload valid Kubernetes manifests "
                                 "or connect a cluster.", 400)
    if app.get("uploaded") and mode == "live" and app_row["status"] != "deployed":
        raise ExperimentRejected("Deploy the application first: Live experiments need it running in the cluster.", 409)
    if mode not in _USER_MODES:
        raise ExperimentRejected("mode must be 'live' or 'simulated'.")
    cluster_id = app.get("cluster_id")
    shared_guest = (config.SHARED_APP_ID > 0 and app_row["id"] == config.SHARED_APP_ID
                    and app_row["user_id"] is not None and app_row["user_id"] != user["id"])
    try:
        duration = catalog.validate_selection(app, service, fault_id, duration)
    except ValueError as e:
        raise ExperimentRejected(str(e))
    if shared_guest and duration > config.SHARED_MAX_DURATION_SECONDS:
        raise ExperimentRejected(f"On the shared demo, faults last at most {config.SHARED_MAX_DURATION_SECONDS} seconds.")

    if mode == "live" and cluster_id:
        # Customer cluster: executed by that cluster's agent, never by this server's own kubectl.
        if app["cluster_paused"]:
            raise ExperimentRejected("Emergency stop is active for this cluster. Resume it first.", 409)
        if not app["cluster_online"]:
            raise ExperimentRejected("The ChaosLab agent for this cluster is offline. Start it and try again.", 503)
    elif mode == "live":
        cap = live_capability()
        if not cap["available"]:
            raise ExperimentRejected(f"Live execution is unavailable: {cap['reason']}", 503)
        disc = catalog.discover_services(app)
        if disc["source"] != "cluster":
            raise ExperimentRejected(
                f"Live execution is unavailable: {app['name']} was not found in the cluster ({disc['error']}).", 503)

    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")      # take the write lock first: the checks below must be atomic with the INSERT
        if cluster_id and mode == "live":
            busy = conn.execute("SELECT COUNT(*) FROM experiments WHERE cluster_id = ? AND status IN ('pending', 'running')",
                                (cluster_id,)).fetchone()[0]
            if busy:
                raise ExperimentRejected(
                    "Someone is already running an experiment on the shared demo cluster. Try again in a minute."
                    if shared_guest else "Another experiment is already running on this cluster.", 409)
            if shared_guest:
                since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
                recent = conn.execute(
                    "SELECT COUNT(*) FROM experiments WHERE user_id = ? AND application_id = ? AND mode = 'live' "
                    "AND created_at >= ?", (user["id"], app_row["id"], since)).fetchone()[0]
                if recent >= config.SHARED_LIVE_RUNS_PER_HOUR:
                    raise ExperimentRejected(
                        f"Fair use: at most {config.SHARED_LIVE_RUNS_PER_HOUR} Live runs per hour on the shared demo. "
                        "Try again later, use Simulated mode, or connect your own cluster.", 429)
        active = conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE user_id = ? AND status IN ('pending', 'running')", (user["id"],)
        ).fetchone()[0]
        if active >= config.MAX_ACTIVE_EXPERIMENTS_PER_USER:
            raise ExperimentRejected("Too many experiments are already running. Wait for one to finish.", 429)

        if mode == "live" and not cluster_id:
            with _live_lock:
                if _live_active:
                    raise ExperimentRejected("Another live experiment is running against the shared cluster. Try again shortly.", 409)
                _live_active = True

        try:
            cur = conn.execute(
                "INSERT INTO experiments (user_id, application_id, target_service, fault_type, duration_seconds, "
                "config_json, mode, status, current_stage, percent, created_at, cluster_id) "
                "VALUES (?, ?, ?, ?, ?, '{}', ?, 'pending', 'pending', 0, ?, ?)",
                (user["id"], app_row["id"], service, fault_id, duration, mode, db.utcnow(),
                 cluster_id if mode == "live" else None),
            )
            exp_id = cur.lastrowid
            engine_config = catalog.build_engine_config(
                app, service, fault_id, duration, f"chaoslab-exp-{exp_id}-{service}-{fault_id}",
                full_verification=(mode == "simulated"),
            )
            conn.execute(
                "UPDATE experiments SET config_json = ? WHERE id = ?",
                (json.dumps({"service": service, "fault": fault_id, "duration_seconds": duration,
                             "namespace": app["namespace"], "engine_config": engine_config}), exp_id),
            )
        except Exception:
            if mode == "live" and not cluster_id:
                with _live_lock:
                    _live_active = False
            raise

    spec = {"experiment_id": exp_id, "user_id": user["id"], "service": service, "fault_id": fault_id,
            "duration": duration, "engine_config": engine_config,
            "cluster_id": cluster_id if mode == "live" else None, "shared_guest": shared_guest}
    threading.Thread(target=_worker, args=(spec, mode), daemon=True, name=f"experiment-{exp_id}").start()
    return exp_id


def _fail(exp_id: int, message: str) -> None:
    with db.session() as conn:
        conn.execute(
            "UPDATE experiments SET status = 'failed', current_stage = 'failed', error = ?, completed_at = ? WHERE id = ?",
            (message[:1000], db.utcnow(), exp_id),
        )


def _worker(spec: Dict[str, Any], mode: str) -> None:
    """
    Runs one experiment. The final status flip happens only AFTER the reports exist, so a client that sees
    `completed`/`failed` always finds its reports.
    """
    global _live_active
    exp_id = spec["experiment_id"]
    sink = ProgressSink(exp_id, simulated=(mode == "simulated"))
    final: Dict[str, Any] = {"status": "failed", "error": "Unknown failure."}
    try:
        with db.session() as conn:
            conn.execute("UPDATE experiments SET status = 'running', started_at = ? WHERE id = ?", (db.utcnow(), exp_id))
        backend = BACKENDS["agent"] if (mode == "live" and spec.get("cluster_id")) else BACKENDS[mode]
        result = backend.run(spec, sink)
        result["metrics"]["series"] = load_samples(exp_id)
        oracle = result.get("oracle") or {}
        with db.session() as conn:
            conn.execute(
                "UPDATE experiments SET current_stage = 'completed', percent = 100, oracle_verdict = ?, "
                "recovery_seconds = ?, result_json = ? WHERE id = ?",
                (oracle.get("verdict"), result["metrics"].get("recovery_seconds"), json.dumps(result), exp_id))
        final = {"status": "completed", "error": None}
    except ExecutionError as e:
        sink.event("failed", str(e), "error")
        final = {"status": "failed", "error": str(e)[:1000]}
    except Exception as e:  # never leave an experiment stuck in 'running'
        traceback.print_exc()
        sink.event("failed", f"Unexpected internal error: {type(e).__name__}", "error")
        final = {"status": "failed", "error": f"Unexpected internal error: {type(e).__name__}"}
    finally:
        if mode == "live" and not spec.get("cluster_id"):
            with _live_lock:
                _live_active = False
        completed_at = db.utcnow()
        try:
            reports.generate(exp_id, {**final, "completed_at": completed_at})
        except Exception:
            traceback.print_exc()
        with db.session() as conn:
            if final["status"] == "completed":
                conn.execute("UPDATE experiments SET status = 'completed', completed_at = ? WHERE id = ?", (completed_at, exp_id))
            else:
                conn.execute("UPDATE experiments SET status = 'failed', current_stage = 'failed', error = ?, completed_at = ? WHERE id = ?",
                             (final["error"], completed_at, exp_id))


def load_samples(exp_id: int):
    with db.session() as conn:
        rows = conn.execute(
            "SELECT data FROM experiment_events WHERE experiment_id = ? AND stage = 'metric' ORDER BY id", (exp_id,)
        ).fetchall()
    return [json.loads(r["data"]) for r in rows if r["data"]]


def experiment_summary(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "application_id": row["application_id"],
        "target_service": row["target_service"],
        "fault_type": row["fault_type"],
        "fault_label": (catalog.get_fault(row["fault_type"]) or {}).get("label", row["fault_type"]),
        "duration_seconds": row["duration_seconds"],
        "mode": row["mode"],
        "simulated": row["mode"] == "simulated",
        "status": row["status"],
        "current_stage": row["current_stage"],
        "percent": row["percent"],
        "error": row["error"],
        "oracle_verdict": row["oracle_verdict"],
        "recovery_seconds": row["recovery_seconds"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def experiment_detail(conn, row, after_event: int = 0) -> Dict[str, Any]:
    d = experiment_summary(row)
    d["application_name"] = conn.execute("SELECT name FROM applications WHERE id = ?", (row["application_id"],)).fetchone()[0]
    d["cluster"] = None
    if row["cluster_id"]:
        c = conn.execute("SELECT id, name FROM clusters WHERE id = ?", (row["cluster_id"],)).fetchone()
        d["cluster"] = {"id": c["id"], "name": c["name"]} if c else None
    cfg = json.loads(row["config_json"] or "{}")
    d["config"] = {k: v for k, v in cfg.items() if k != "engine_config"}
    d["engine_config"] = cfg.get("engine_config")
    events = conn.execute(
        "SELECT id, ts, stage, level, message, simulated FROM experiment_events "
        "WHERE experiment_id = ? AND stage != 'metric' AND id > ? ORDER BY id", (row["id"], after_event)
    ).fetchall()
    d["events"] = [dict(e) for e in events]
    if row["status"] in ("pending", "running"):
        samples = conn.execute(
            "SELECT data FROM experiment_events WHERE experiment_id = ? AND stage = 'metric' ORDER BY id", (row["id"],)
        ).fetchall()
        d["samples"] = [json.loads(s["data"]) for s in samples if s["data"]]
    d["result"] = json.loads(row["result_json"]) if row["result_json"] else None
    if d["result"]:
        d["samples"] = d["result"]["metrics"].get("series", [])
    return d
