"""
Live execution backend
======================
Runs the EXISTING ChaosLab engine (`verifier/engine.py`): baseline check, Chaos Mesh injection
through kubectl, recovery tracking, application probes, Jaeger telemetry and the Verification
Oracle. Nothing here is hard-coded or simulated; if the cluster or Chaos Mesh is unavailable
the run fails with the engine's real error.

While the engine runs, a sampler thread polls the target's pod readiness (real observations)
so the monitoring view can chart service health.
"""

import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Optional, Tuple

import yaml

from . import catalog, config, engine_bridge
from .execution import (DATA_SOURCE_LIVE, ExecutionBackend, ExecutionError, ProgressSink, build_result_common,
                        normalize_oracle, stage_timeline)

# engine progress phase -> platform stage
PHASE_TO_STAGE = {
    "baseline": "baseline",
    "injecting": "fault_injection",
    "waiting_impact": "impact",
    "waiting_recovery": "recovery",
    "probing_app": "verification",
    "telemetry": "verification",
    "oracle": "oracle",
    "completed": "completed",
}

_capability_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def _kubectl(args, timeout=6) -> Tuple[bool, str]:
    try:
        res = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)
        return res.returncode == 0, (res.stdout if res.returncode == 0 else res.stderr).strip()
    except Exception as e:
        return False, str(e)


def live_capability(force: bool = False) -> Dict[str, Any]:
    """Whether Live execution can run right now (cached ~15s)."""
    now = time.time()
    if not force and _capability_cache["value"] and now - _capability_cache["at"] < 15:
        return _capability_cache["value"]
    if not config.LIVE_ENABLED:
        value = {"available": False, "reason": "Live execution is disabled on this server (CHAOSLAB_LIVE_ENABLED=false)."}
    elif not shutil.which("kubectl"):
        value = {"available": False, "reason": "kubectl was not found on the server."}
    else:
        ok, out = _kubectl(["get", "pods", "-n", catalog.DEMO_NAMESPACE, "-o", "name"])
        if not ok:
            value = {"available": False, "reason": f"Kubernetes cluster not reachable: {out[:160]}"}
        else:
            ok_crd, out_crd = _kubectl(["get", "crd", "podchaos.chaos-mesh.org", "-o", "name"])
            if not ok_crd:
                value = {"available": False, "reason": "Chaos Mesh is not installed in the cluster."}
            else:
                value = {"available": True, "reason": "Cluster reachable and Chaos Mesh installed."}
    _capability_cache.update(at=now, value=value)
    return value


class LiveBackend(ExecutionBackend):
    mode = "live"

    def run(self, spec: Dict[str, Any], sink: ProgressSink) -> Dict[str, Any]:
        eng = engine_bridge.engine()
        cfg = spec["engine_config"]

        # Persist the resolved experiment as a regular ChaosLab YAML, then use the engine's normal entrypoint.
        cfg_dir = os.path.join(config.DATA_DIR, "run-configs")
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_path = os.path.join(cfg_dir, f"experiment_{spec['experiment_id']}.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)

        # The engine's native reports go to an absolute location (its default is cwd-relative).
        eng.REPORTS_DIR = os.path.join(config.REPORT_DIR, "engine")

        ns = cfg["target"]["namespace"]
        selector = ",".join(f"{k}={v}" for k, v in cfg["target"]["label"].items())
        started = time.time()
        stop = threading.Event()

        def sampler():
            while not stop.is_set():
                try:
                    pods = eng.get_pods(ns, selector)
                    ready = [p for p in pods if p["status"] == "Running" and p["total_count"] > 0 and p["ready_count"] == p["total_count"]]
                    sink.sample({"t": round(time.time() - started, 1), "replicas_ready": len(ready),
                                 "replicas_total": len(pods), "simulated": False})
                except Exception:
                    pass
                stop.wait(2.0)

        state: Dict[str, Optional[Any]] = {"report": None, "failure": None}

        def on_progress(phase, percent, message, data=None):
            if phase == "failed":
                state["failure"] = message
                sink.event("failed", message, "error", percent=None)
                return
            if data:
                state["report"] = data
            sink.event(PHASE_TO_STAGE.get(phase, "baseline"), message, "info", percent)

        thread = threading.Thread(target=sampler, daemon=True)
        thread.start()
        try:
            eng.verify_experiment(cfg_path, progress_callback=on_progress)
        except Exception as e:
            raise ExecutionError(f"ChaosLab engine error: {e}")
        finally:
            stop.set()
            thread.join(timeout=5)

        report = state["report"]
        if not report:
            raise ExecutionError(state["failure"] or "The engine finished without producing a result.")
        return self._to_result(spec, sink, report)

    @staticmethod
    def _to_result(spec, sink, report: Dict[str, Any]) -> Dict[str, Any]:
        ts = report.get("timestamps", {})
        labels = {
            "experiment_start": ("baseline", "Baseline"),
            "fault_injected": ("fault_injection", "Fault injection"),
            "original_pod_disappeared": ("impact", "Service impact"),
            "replacement_ready": ("recovery", "Recovery"),
            "experiment_end": ("completed", "Completed"),
        }
        timeline = [{"key": k, "label": l, "at": ts[src], "detail": None}
                    for src, (k, l) in labels.items() if ts.get(src)]
        tel = report.get("telemetry") or {}
        lat = tel.get("latency_ms") or {}
        baseline = report.get("baseline", {})
        return build_result_common(
            simulated=False,
            data_source=DATA_SOURCE_LIVE,
            spec=spec,
            timeline=timeline or stage_timeline(sink.stage_times),
            health={
                "baseline_ready": baseline.get("ready_replicas"),
                "min_ready_during_fault": None,
                "recovered_ready": report.get("recovered_ready_replicas"),
                "baseline_pod": report.get("original_pod"),
                "replacement_pod": report.get("replacement_pod"),
            },
            metrics={
                "recovery_seconds": report.get("recovery_time_seconds"),
                "total_recovery_seconds": report.get("total_recovery_seconds"),
                "baseline_p95_ms": None,
                "peak_p95_ms": None,
                "post_recovery_p95_ms": lat.get("p95") if tel.get("traces_count") else None,
                "peak_error_rate": None,
                "error_spans": tel.get("error_spans_count") if tel.get("available") else None,
                "traces_count": tel.get("traces_count") if tel.get("available") else None,
                "series": [],   # filled from the sampled events by the runner
            },
            oracle=normalize_oracle(report["oracle_evaluation"], simulated=False),
            notes=["Collected live from the ChaosLab engine. Latency series are only available when "
                   "Jaeger telemetry is enabled for the experiment."],
        )

