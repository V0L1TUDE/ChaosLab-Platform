"""
Execution layer contract
========================
Both execution modes implement `ExecutionBackend.run(spec, sink) -> result` and return the SAME
normalized result structure, so the monitoring UI, Oracle view and reports never care which
backend produced the data:

    LiveBackend       (live_backend.py)  real Chaos Mesh + Kubernetes via the existing engine
    SimulatedBackend  (sim_backend.py)   predefined data, every item flagged SIMULATED

Replacing the simulation with a real engine later means registering another backend; nothing
in the API or the frontend changes.
"""

import json
from typing import Any, Dict, List, Optional

from . import db, engine_bridge

STAGES = [
    ("baseline", "Baseline"),
    ("fault_injection", "Fault injection"),
    ("impact", "Service impact"),
    ("recovery", "Recovery"),
    ("verification", "Probes & telemetry"),
    ("oracle", "Oracle verification"),
    ("completed", "Completed"),
]
STAGE_LABELS = dict(STAGES)

ORACLE_GROUPS = {
    "infrastructure": ["baseline_healthy", "fault_injected", "target_impact", "service_recovery", "recovery_within_timeout"],
    "application": ["app_health_probe", "app_functional_check", "application_data_persisted", "app_logs_healthy"],
    "telemetry": ["telemetry_traces_recorded", "telemetry_error_spike_detected", "telemetry_latency_recovered"],
}
_GROUP_OF = {rule: group for group, rules in ORACLE_GROUPS.items() for rule in rules}
_STATUS_LABEL = {"PASS": "PASS", "FAIL": "FAIL", "SKIPPED": "SKIP", "NOT_APPLICABLE": "N/A"}

DATA_SOURCE_SIMULATED = "SIMULATED: predefined demonstration data, not observed from Kubernetes"
DATA_SOURCE_LIVE = "LIVE: collected by the ChaosLab engine from Chaos Mesh and Kubernetes"


class ExecutionError(Exception):
    """An execution could not be completed (as opposed to completing with an Oracle FAIL)."""


class ProgressSink:
    """Persists lifecycle events and metric samples for one experiment (pollable by the UI)."""

    def __init__(self, experiment_id: int, simulated: bool):
        self.experiment_id = experiment_id
        self.simulated = simulated
        self.stage_times: Dict[str, str] = {}

    def event(self, stage: str, message: str, level: str = "info", percent: Optional[int] = None) -> None:
        if self.simulated:
            message = f"[SIMULATED] {message}"
        now = db.utcnow()
        self.stage_times.setdefault(stage, now)
        with db.session() as conn:
            conn.execute(
                "INSERT INTO experiment_events (experiment_id, ts, stage, level, message, simulated) VALUES (?, ?, ?, ?, ?, ?)",
                (self.experiment_id, now, stage, level, message, int(self.simulated)),
            )
            if stage in STAGE_LABELS:
                if percent is None:
                    conn.execute("UPDATE experiments SET current_stage = ? WHERE id = ?", (stage, self.experiment_id))
                else:
                    conn.execute("UPDATE experiments SET current_stage = ?, percent = ? WHERE id = ?",
                                 (stage, int(percent), self.experiment_id))

    def sample(self, point: Dict[str, Any]) -> None:
        """Record a metric sample: {t, replicas_ready, p95_ms?, error_rate?, ...}."""
        with db.session() as conn:
            conn.execute(
                "INSERT INTO experiment_events (experiment_id, ts, stage, level, message, simulated, data) "
                "VALUES (?, ?, 'metric', 'metric', '', ?, ?)",
                (self.experiment_id, db.utcnow(), int(self.simulated), json.dumps(point)),
            )


class ExecutionBackend:
    mode = ""

    def run(self, spec: Dict[str, Any], sink: ProgressSink) -> Dict[str, Any]:
        raise NotImplementedError


def _rule_descriptions() -> Dict[str, str]:
    try:
        return {r.name: r.description for r in engine_bridge.oracle().VerificationOracle().rules}
    except Exception:
        return {}


def normalize_oracle(evaluation: Dict[str, Any], simulated: bool) -> Dict[str, Any]:
    """Convert the engine's Oracle evaluation into the UI/report structure (statuses stay distinct)."""
    descriptions = _rule_descriptions()
    rules: List[Dict[str, Any]] = []
    for r in evaluation.get("rule_evaluations", []):
        status = r.get("status") or ("PASS" if r.get("passed") else "FAIL")
        rules.append({
            "rule": r["rule"],
            "group": _GROUP_OF.get(r["rule"], "other"),
            "description": descriptions.get(r["rule"], ""),
            "status": status,
            "status_label": _STATUS_LABEL.get(status, status),
            "message": ("[SIMULATED] " + str(r.get("message"))) if simulated else r.get("message"),
            "observed": None if r.get("observed") is None else str(r.get("observed")),
            "expected": None if r.get("expected") is None else str(r.get("expected")),
            "simulated": simulated,
        })
    count = lambda label: sum(1 for r in rules if r["status_label"] == label)
    return {
        "verdict": evaluation.get("verdict", "FAIL"),
        "simulated": simulated,
        "counts": {
            "total": len(rules), "pass": count("PASS"), "fail": count("FAIL"),
            "skip": count("SKIP"), "na": count("N/A"), "executed": count("PASS") + count("FAIL"),
        },
        "failure_reasons": evaluation.get("failure_reasons", []),
        "rules": rules,
        "groups": {g: [r for r in rules if r["group"] == g] for g in ORACLE_GROUPS},
    }


def stage_timeline(stage_times: Dict[str, str], extra: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Ordered timeline entries [{key, label, at}] from first-seen stage timestamps."""
    out = []
    for key, label in STAGES:
        at = stage_times.get(key)
        if at:
            out.append({"key": key, "label": label, "at": at, "detail": (extra or {}).get(key)})
    return out


def build_result_common(*, simulated: bool, data_source: str, spec: Dict[str, Any], timeline: List[Dict[str, Any]],
                        health: Dict[str, Any], metrics: Dict[str, Any], oracle: Optional[Dict[str, Any]],
                        notes: List[str]) -> Dict[str, Any]:
    """The single result shape every backend must return (and the UI/reports consume)."""
    from . import catalog
    fault = catalog.get_fault(spec["fault_id"]) or {}
    return {
        "schema_version": 1,
        "mode": "simulated" if simulated else "live",
        "simulated": simulated,
        "data_source": data_source,
        "target": {"service": spec["service"], "namespace": spec["engine_config"]["target"]["namespace"]},
        "fault": {"id": spec["fault_id"], "label": fault.get("label"), "engine_type": fault.get("engine_type"),
                  "duration_seconds": spec["duration"]},
        "timeline": timeline,
        "health": health,
        "metrics": metrics,
        "oracle": oracle,
        "notes": notes,
    }
