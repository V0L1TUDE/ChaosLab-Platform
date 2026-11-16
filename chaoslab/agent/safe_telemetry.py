"""
Safe telemetry (distributed tracing) collection for the ChaosLab agent
=====================================================================
Fills the Oracle's telemetry invariants for a customer cluster without trusting the server with network targets:

  * WHERE Jaeger lives is decided only by the agent operator (`--jaeger NAMESPACE/SERVICE:PORT`); the server can only
    name the traced service and thresholds. Queries are read-only GETs to Jaeger's query API through a temporary
    `kubectl port-forward svc/...` bound to 127.0.0.1.
  * Optional "traffic during the fault": a GET once per second to a caller service in the SAME allowed namespace
    (e.g. task-service -> project-service), so the fault becomes visible as error spans. GET only, validated path,
    capped rate and duration.
  * If tracing cannot be collected, evidence carries `collection_error` and the server's Oracle marks the telemetry
    invariants SKIPPED (never a false FAIL or PASS).
"""

import json
import re
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import safe_probes

SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
JAEGER_RE = re.compile(r"^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)/([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?):(\d{1,5})$")
DNS_LABEL = safe_probes.DNS_LABEL
MAX_TRACES = 100


def parse_jaeger(value: Optional[str]) -> Optional[Tuple[str, str, int]]:
    """'default/jaeger:16686' -> ('default', 'jaeger', 16686). Raises ValueError on bad input."""
    if not value:
        return None
    m = JAEGER_RE.match(value.strip())
    if not m or not 1 <= int(m.group(5)) <= 65535:
        raise ValueError("--jaeger must look like NAMESPACE/SERVICE:PORT, e.g. default/jaeger:16686")
    return m.group(1), m.group(3), int(m.group(5))


def validate_telemetry_spec(spec: Any, target_service: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Server-sent telemetry settings -> (clean spec, None) or (None, reason). Never contains an endpoint."""
    if not isinstance(spec, dict):
        return None, "telemetry must be an object"
    extra = set(spec) - {"enabled", "service_name", "max_allowed_p95_ms", "verify_error_spike", "traffic"}
    if extra:
        return None, f"unsupported telemetry field(s): {', '.join(sorted(extra))}"
    name = spec.get("service_name") or target_service
    if not isinstance(name, str) or not SERVICE_NAME_RE.match(name):
        return None, "telemetry.service_name is invalid"
    p95 = spec.get("max_allowed_p95_ms", 1000)
    if isinstance(p95, bool) or not isinstance(p95, (int, float)) or not 1 <= p95 <= 60000:
        return None, "telemetry.max_allowed_p95_ms must be 1-60000"
    verify = spec.get("verify_error_spike", False)
    if not isinstance(verify, bool):
        return None, "telemetry.verify_error_spike must be true or false"
    clean: Dict[str, Any] = {"service_name": name, "max_allowed_p95_ms": p95, "verify_error_spike": verify}
    traffic = spec.get("traffic")
    if traffic is not None:
        if not isinstance(traffic, dict) or set(traffic) - {"service", "port", "path"}:
            return None, "telemetry.traffic: unsupported fields"
        if not isinstance(traffic.get("service"), str) or not DNS_LABEL.match(traffic["service"]):
            return None, "telemetry.traffic.service is invalid"
        http, err = safe_probes._validate_http({"port": traffic.get("port"), "path": traffic.get("path")}, "telemetry.traffic")
        if err:
            return None, err
        clean["traffic"] = {"service": traffic["service"], "port": http["port"], "path": http["path"]}
    if verify and "traffic" not in clean:
        return None, "telemetry.verify_error_spike needs traffic during the fault (telemetry.traffic)"
    return clean, None


# ---------------------------------------------------------------------------------------------- traffic during the fault
class TrafficDriver:
    """GET once per second to a Ready pod of the caller service while the fault is active (bounded)."""

    def __init__(self, namespace: str, traffic: Dict[str, Any], kubectl: Callable, forward_factory: Optional[Callable] = None,
                 interval: float = 1.0, max_seconds: float = 180.0):
        self.ns, self.traffic, self.kubectl = namespace, traffic, kubectl
        self.forward_factory = forward_factory or safe_probes.PortForward
        self.interval, self.max_seconds = interval, max_seconds
        self.stats = {"sent": 0, "ok": 0, "errors": 0, "service": traffic["service"], "path": traffic["path"]}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> Dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        return dict(self.stats)

    def _run(self) -> None:
        deadline = time.time() + self.max_seconds
        while not self._stop.is_set() and time.time() < deadline:
            pods = safe_probes.ready_pods(self.ns, self.traffic["service"], self.kubectl)
            if not pods:
                self._stop.wait(self.interval)
                continue
            try:
                with self.forward_factory(self.ns, pods[0], self.traffic["port"]) as local_port:
                    while not self._stop.is_set() and time.time() < deadline:
                        r = safe_probes.http_get(local_port, self.traffic["path"], timeout=5)
                        self.stats["sent"] += 1
                        if "error" in r or r.get("status", 500) >= 500:
                            self.stats["errors"] += 1
                            if "error" in r:
                                break                       # forward broke (pod gone): re-resolve a pod
                        else:
                            self.stats["ok"] += 1
                        self._stop.wait(self.interval)
            except safe_probes.ProbeError:
                self._stop.wait(self.interval)


# ---------------------------------------------------------------------------------------------- Jaeger queries
def _iso_us(ts: Optional[str]) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(ts).timestamp() * 1_000_000) if ts else None
    except ValueError:
        return None


def _query(local_port: int, service: str, start_us: int, end_us: int) -> List[Dict[str, Any]]:
    path = "/api/traces?" + urllib.parse.urlencode({"service": service, "start": start_us, "end": end_us, "limit": MAX_TRACES})
    r = safe_probes.http_get(local_port, path, timeout=15, max_body=8 * 1024 * 1024)
    if "error" in r:
        raise RuntimeError(f"Jaeger query failed: {r['error']}")
    if r["status"] != 200:
        raise RuntimeError(f"Jaeger query returned HTTP {r['status']}")
    try:
        return json.loads(r["body"]).get("data") or []
    except ValueError:
        raise RuntimeError("Jaeger returned a response that is not JSON (response too large or wrong port?)")


def collect(jaeger: Optional[Tuple[str, str, int]], spec: Dict[str, Any], report: Dict[str, Any], analyze: Callable,
            traffic_stats: Optional[Dict[str, Any]] = None, forward_factory: Optional[Callable] = None,
            now_us: Optional[int] = None) -> Dict[str, Any]:
    """
    Evidence in the shape the Oracle's telemetry rules read:
      traces_count / spans_count / latency_ms   from the post-recovery window of the target service
      error_spans_count / fault_window_errors   errors during the fault window (target + traffic caller)
    """
    base = {"enabled": True, "service": spec["service_name"], "source": "agent", "traffic": traffic_stats}
    if not jaeger:
        return {**base, "available": False,
                "collection_error": "this agent has no tracing source configured (start it with --jaeger NAMESPACE/SERVICE:PORT)"}
    ns, svc, port = jaeger
    ts = report.get("timestamps") or {}
    now_us = now_us or int(time.time() * 1_000_000)
    fault_start = _iso_us(ts.get("fault_injected")) or (now_us - 120_000_000)
    recovered = _iso_us(ts.get("replacement_ready")) or _iso_us(ts.get("experiment_end")) or now_us
    factory = forward_factory or safe_probes.PortForward
    try:
        with factory(ns, svc, port, kind="svc") as local_port:
            post = _query(local_port, spec["service_name"], recovered - 2_000_000, now_us + 2_000_000)
            if not post:                    # nothing after recovery yet: widen to the last two minutes
                post = _query(local_port, spec["service_name"], now_us - 120_000_000, now_us + 2_000_000)
            fault_target = _query(local_port, spec["service_name"], fault_start - 1_000_000, recovered + 2_000_000)
            fault_caller: List[Dict[str, Any]] = []
            if spec.get("traffic"):
                fault_caller = _query(local_port, spec["traffic"]["service"], fault_start - 1_000_000, recovered + 2_000_000)
    except (safe_probes.ProbeError, RuntimeError) as e:
        return {**base, "available": False, "collection_error": f"could not query Jaeger at {ns}/{svc}:{port}: {e}"}
    analysis = analyze(post)
    target_errors = analyze(fault_target).get("error_spans_count", 0)
    caller_errors = analyze(fault_caller).get("error_spans_count", 0) if fault_caller else 0
    return {**analysis, **base, "available": True,
            "error_spans_count": target_errors + caller_errors,
            "fault_window_errors": target_errors + caller_errors,
            "fault_window": {"target_traces": len(fault_target), "target_error_spans": target_errors,
                             "caller_traces": len(fault_caller), "caller_error_spans": caller_errors},
            "jaeger": f"{ns}/{svc}:{port}"}
