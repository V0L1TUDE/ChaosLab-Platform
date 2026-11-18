"""
Simulated (Demo) execution backend
==================================
Plays a predefined experiment lifecycle with predefined metrics. NOTHING is injected into
Kubernetes. Every event, sample, invariant and report produced here is flagged SIMULATED.

The simulated *evidence* is passed through the real ChaosLab `VerificationOracle`, so the
invariant semantics (PASS / FAIL / SKIP / N/A, verdict rules) are the genuine ones; only the
evidence feeding them is predefined.
"""

import time
from typing import Any, Dict, List

from . import catalog, config, engine_bridge
from .execution import (DATA_SOURCE_SIMULATED, ExecutionBackend, ProgressSink, build_result_common,
                        normalize_oracle, stage_timeline)

# Predefined "bad day" scenarios so the demo can also show a failing verdict.
DEGRADED_SCENARIOS = {
    ("checkoutservice", "network-failure"): {
        "post_recovery_p95_ms": 1350.0,
        "note": "Predefined scenario: checkout latency stays above its SLA after the network fault ends.",
    },
}


def _params(service: str, fault_id: str) -> Dict[str, Any]:
    h = sum(ord(c) for c in service)
    base_p95 = float(38 + h % 47)
    p = {
        "base_p95": base_p95,
        "post_p95": round(base_p95 * 1.12, 1),
        "traces": 40 + h % 30,
        "tag": f"{h:04d}"[-4:],
    }
    if fault_id == "pod-kill":
        p.update(recovery=round(6.0 + (h % 25) / 10, 1), peak_p95=2400.0, peak_err=0.85, err_spans=31 + h % 20)
    elif fault_id == "network-delay":
        p.update(recovery=round(1.2 + (h % 8) / 10, 1), peak_p95=round(base_p95 + 210, 1), peak_err=0.02, err_spans=6 + h % 5)
    else:
        p.update(recovery=round(2.1 + (h % 9) / 10, 1), peak_p95=3000.0, peak_err=1.0, err_spans=54 + h % 25)
    override = DEGRADED_SCENARIOS.get((service, fault_id))
    if override:
        p["post_p95"] = override["post_recovery_p95_ms"]
        p["note"] = override["note"]
    p["spans"] = p["traces"] * 4 + h % 9
    return p


def _build_ticks(service: str, fault_id: str, duration: int, p: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The predefined script: a list of ticks (stage, optional message, optional sample)."""
    ticks: List[Dict[str, Any]] = []
    t = [0]

    def tick(stage, message=None, pause=0.8, level="info", percent=None, **sample):
        entry = {"stage": stage, "message": message, "pause": pause, "level": level, "percent": percent}
        if sample:
            entry["sample"] = {"t": t[0], **sample}
            t[0] += 2
        ticks.append(entry)

    tag = p["tag"]
    # 1. Baseline
    tick("baseline", f"Establishing baseline for '{service}': 1/1 replica Ready (simulated pod {service}-sim-{tag}).",
         percent=8, replicas_ready=1, p95_ms=p["base_p95"], error_rate=0.0, phase="baseline")
    tick("baseline", "Baseline steady: latency and error rate nominal.", percent=15,
         replicas_ready=1, p95_ms=p["base_p95"], error_rate=0.0, phase="baseline")
    tick("baseline", None, percent=18, replicas_ready=1, p95_ms=p["base_p95"], error_rate=0.0, phase="baseline")

    # 2. Fault injection
    label = catalog.get_fault(fault_id)["label"]
    tick("fault_injection", f"Applying simulated {label} fault to '{service}' (no real fault is injected).",
         pause=1.4, percent=28, replicas_ready=1, p95_ms=p["base_p95"], error_rate=0.0, phase="fault")
    tick("fault_injection", "Simulated chaos controller reports the fault as injected.", percent=32)

    # 3. Impact
    if fault_id == "pod-kill":
        tick("impact", f"Simulated pod {service}-sim-{tag} terminated; 0/1 replicas Ready.", percent=42,
             replicas_ready=0, p95_ms=p["peak_p95"], error_rate=p["peak_err"], phase="impact")
        tick("impact", "Simulated callers observe connection errors and timeouts.", percent=50,
             level="warn", replicas_ready=0, p95_ms=p["peak_p95"], error_rate=p["peak_err"], phase="impact")
    else:
        n = max(2, min(5, duration // 8))
        msg = ("added latency visible in simulated traces" if fault_id == "network-delay"
               else "simulated packet loss makes the service unreachable")
        for i in range(n):
            tick("impact", f"Service impact: {msg}." if i == 0 else None, percent=40 + i * 3, level="warn" if i == 0 else "info",
                 replicas_ready=1, p95_ms=p["peak_p95"], error_rate=p["peak_err"], phase="impact")

    # 4. Recovery
    if fault_id == "pod-kill":
        tick("recovery", f"Simulated replacement pod scheduled: {service}-sim-{tag}-r.", percent=62,
             replicas_ready=0, p95_ms=p["peak_p95"] * 0.6, error_rate=p["peak_err"] * 0.4, phase="recovery")
    else:
        tick("recovery", "Simulated fault window ended; traffic returning to normal.", percent=62,
             replicas_ready=1, p95_ms=(p["peak_p95"] + p["post_p95"]) / 2, error_rate=p["peak_err"] * 0.3, phase="recovery")
    tick("recovery", f"Service recovered in {p['recovery']}s (simulated); 1/1 replicas Ready.", percent=72,
         replicas_ready=1, p95_ms=round(p["post_p95"] * 1.3, 1), error_rate=0.02, phase="recovery")
    tick("recovery", None, percent=74, replicas_ready=1, p95_ms=p["post_p95"], error_rate=0.0, phase="recovered")

    # 5. Verification
    tick("verification", "Running simulated health, functional and log probes.", percent=80)
    tick("verification", "Collecting simulated distributed traces and latency distribution.", percent=88)
    # 6. Oracle
    tick("oracle", "Evaluating Verification Oracle invariants on simulated evidence.", pause=1.2, percent=95)
    return ticks


def _build_evidence(service: str, fault_id: str, p: Dict[str, Any], probe_type: str) -> Dict[str, Any]:
    tag = p["tag"]
    grpc = probe_type == "grpc"
    return {
        "baseline": {"pod_name": f"{service}-sim-{tag}", "ready_replicas": 1, "ready": "1/1", "status": "Running"},
        "fault_injected": True,
        "chaos_mesh_evidence": {"conditions": [{"type": "AllInjected", "status": "True"}]},
        "original_pod": f"{service}-sim-{tag}",
        "original_pod_disappeared": fault_id == "pod-kill",
        "replacement_pod": f"{service}-sim-{tag}-r" if fault_id == "pod-kill" else f"{service}-sim-{tag}",
        "replacement_ready": True,
        "recovered_ready_replicas": 1,
        "recovery_time_seconds": p["recovery"],
        "total_recovery_seconds": round(p["recovery"] + 2.0, 1),
        "recovery_measured": True,                 # "measured" by the simulation: the whole result is labelled SIMULATED
        "simulated": True,
        "application": {
            "enabled": True, "probe_type": probe_type, "health_check_passed": True,
            "health_status": "SERVING" if grpc else 200,
            "functional_test_passed": True, "transaction_id": f"SIM-{tag}", "startup_log_detected": True,
            "log_error_count": 0,
        },
        "telemetry": {
            "available": True, "service": service, "traces_count": p["traces"], "spans_count": p["spans"],
            "error_spans_count": p["err_spans"],
            "latency_ms": {"p50": round(p["post_p95"] * 0.6, 1), "p95": p["post_p95"], "max": round(p["post_p95"] * 1.8, 1)},
        },
    }


class SimulatedBackend(ExecutionBackend):
    mode = "simulated"

    def run(self, spec: Dict[str, Any], sink: ProgressSink) -> Dict[str, Any]:
        service, fault_id, duration = spec["service"], spec["fault_id"], spec["duration"]
        p = _params(service, fault_id)
        series: List[Dict[str, Any]] = []

        for tk in _build_ticks(service, fault_id, duration, p):
            if tk["message"]:
                sink.event(tk["stage"], tk["message"], tk["level"], tk["percent"])
            elif tk["stage"] not in sink.stage_times:
                sink.event(tk["stage"], f"Entering stage: {tk['stage']}", "info", tk["percent"])
            if "sample" in tk:
                point = {**tk["sample"], "simulated": True}
                series.append(point)
                sink.sample(point)
            time.sleep(tk["pause"] * config.SIM_SPEED)

        probe_type = spec["engine_config"]["application"]["health_probe"]["type"]
        evidence = _build_evidence(service, fault_id, p, probe_type)
        oracle = engine_bridge.oracle().VerificationOracle()
        evaluation = oracle.evaluate(evidence, spec["engine_config"])
        sink.event("completed", f"Simulated run finished. Oracle verdict: {evaluation['verdict']}.", "info", 100)

        notes = ["All data in this run is SIMULATED. No fault was injected into any Kubernetes cluster."]
        if p.get("note"):
            notes.append(p["note"])
        return build_result_common(
            simulated=True,
            data_source=DATA_SOURCE_SIMULATED,
            spec=spec,
            timeline=stage_timeline(sink.stage_times),
            health={
                "baseline_ready": 1,
                "min_ready_during_fault": 0 if fault_id == "pod-kill" else 1,
                "recovered_ready": 1,
                "baseline_pod": evidence["original_pod"],
                "replacement_pod": evidence["replacement_pod"],
            },
            metrics={
                "recovery_seconds": p["recovery"],
                "total_recovery_seconds": evidence["total_recovery_seconds"],
                "baseline_p95_ms": p["base_p95"],
                "peak_p95_ms": p["peak_p95"],
                "post_recovery_p95_ms": p["post_p95"],
                "peak_error_rate": p["peak_err"],
                "error_spans": p["err_spans"],
                "traces_count": p["traces"],
                "series": series,
            },
            oracle=normalize_oracle(evaluation, simulated=True),
            notes=notes,
        )
