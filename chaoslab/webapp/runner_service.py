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
from typing import Any, Dict, Optional

from . import catalog, config, db, reports
from .execution import ExecutionBackend, ExecutionError, ProgressSink
from .live_backend import LiveBackend, live_capability
from .sim_backend import SimulatedBackend

BACKENDS: Dict[str, ExecutionBackend] = {"live": LiveBackend(), "simulated": SimulatedBackend()}

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
    app = catalog.get_builtin(app_row["slug"])
    if not app:
        raise ExperimentRejected("Experiments can only be run against the built-in demo applications in this MVP.", 400)
    if mode not in BACKENDS:
        raise ExperimentRejected("mode must be 'live' or 'simulated'.")
    try:
        duration = catalog.validate_selection(app, service, fault_id, duration)
    except ValueError as e:
        raise ExperimentRejected(str(e))

    if mode == "live":
        cap = live_capability()
        if not cap["available"]:
            raise ExperimentRejected(f"Live execution is unavailable: {cap['reason']}", 503)
        disc = catalog.discover_services(app)
        if disc["source"] != "cluster":
            raise ExperimentRejected(
                f"Live execution is unavailable: {app['name']} was not found in the cluster ({disc['error']}).", 503)

    with db.session() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE user_id = ? AND status IN ('pending', 'running')", (user["id"],)
        ).fetchone()[0]
        if active >= config.MAX_ACTIVE_EXPERIMENTS_PER_USER:
            raise ExperimentRejected("Too many experiments are already running. Wait for one to finish.", 429)

        if mode == "live":
            with _live_lock:
                if _live_active:
                    raise ExperimentRejected("Another live experiment is running against the shared cluster. Try again shortly.", 409)
                _live_active = True

        try:
            cur = conn.execute(
                "INSERT INTO experiments (user_id, application_id, target_service, fault_type, duration_seconds, "
                "config_json, mode, status, current_stage, percent, created_at) "
                "VALUES (?, ?, ?, ?, ?, '{}', ?, 'pending', 'pending', 0, ?)",
                (user["id"], app_row["id"], service, fault_id, duration, mode, db.utcnow()),
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
            if mode == "live":
                with _live_lock:
                    _live_active = False
            raise

    spec = {"experiment_id": exp_id, "user_id": user["id"], "service": service, "fault_id": fault_id,
            "duration": duration, "engine_config": engine_config}
    threading.Thread(target=_worker, args=(spec, mode), daemon=True, name=f"experiment-{exp_id}").start()
    return exp_id


def _fail(exp_id: int, message: str) -> None:
    with db.session() as conn:
        conn.execute(
            "UPDATE experiments SET status = 'failed', current_stage = 'failed', error = ?, completed_at = ? WHERE id = ?",
            (message[:1000], db.utcnow(), exp_id),
        )


def _worker(spec: Dict[str, Any], mode: str) -> None:
    global _live_active
    exp_id = spec["experiment_id"]
    sink = ProgressSink(exp_id, simulated=(mode == "simulated"))
    try:
        with db.session() as conn:
            conn.execute("UPDATE experiments SET status = 'running', started_at = ? WHERE id = ?", (db.utcnow(), exp_id))
        result = BACKENDS[mode].run(spec, sink)
        result["metrics"]["series"] = load_samples(exp_id)
        oracle = result.get("oracle") or {}
        with db.session() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'completed', current_stage = 'completed', percent = 100, "
                "oracle_verdict = ?, recovery_seconds = ?, result_json = ?, completed_at = ? WHERE id = ?",
                (oracle.get("verdict"), result["metrics"].get("recovery_seconds"), json.dumps(result), db.utcnow(), exp_id),
            )
    except ExecutionError as e:
        sink.event("failed", str(e), "error")
        _fail(exp_id, str(e))
    except Exception as e:  # never leave an experiment stuck in 'running'
        traceback.print_exc()
        sink.event("failed", f"Unexpected internal error: {type(e).__name__}", "error")
        _fail(exp_id, f"Unexpected internal error: {type(e).__name__}")
    finally:
        if mode == "live":
            with _live_lock:
                _live_active = False
        try:
            reports.generate(exp_id)
        except Exception:
            traceback.print_exc()


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
