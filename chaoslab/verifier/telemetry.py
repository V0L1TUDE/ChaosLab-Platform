"""
ChaosLab OpenTelemetry Query & Verification Engine
==================================================
Interacts with in-cluster OpenTelemetry receiver / Jaeger REST API to collect,
analyze, and verify distributed tracing, latency distributions, and error spans.
"""

import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

JAEGER_DEFAULT_URL = "http://localhost:16686"
_PORT_FORWARD_PROC = None

def ensure_jaeger_connection(base_url: str = JAEGER_DEFAULT_URL, namespace: str = "default") -> bool:
    """Ensure Jaeger REST API is accessible on host, starting port-forward if needed."""
    global _PORT_FORWARD_PROC

    # Test if already responding
    try:
        req = urllib.request.Request(f"{base_url}/api/services", headers={"User-Agent": "ChaosLab"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass

    # Start background port-forward if not responding
    try:
        _PORT_FORWARD_PROC = subprocess.Popen(
            ["kubectl", "port-forward", "svc/jaeger", "16686:16686", "-n", namespace],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        # Give it a moment to bind
        for _ in range(5):
            time.sleep(1)
            try:
                req = urllib.request.Request(f"{base_url}/api/services", headers={"User-Agent": "ChaosLab"})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                continue
    except Exception as e:
        print(f"Warning: Failed to launch Jaeger port-forward: {e}")

    return False

def calculate_percentile(values: List[float], p: float) -> float:
    """Calculate the p-th percentile of a list of numeric values using linear interpolation."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c < len(sorted_vals):
        return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])
    return sorted_vals[f]

class TelemetryClient:
    """Client for querying and analyzing distributed traces from Jaeger."""

    def __init__(self, base_url: str = JAEGER_DEFAULT_URL, namespace: str = "default"):
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace

    def is_connected(self) -> bool:
        """Verify connectivity to Jaeger."""
        return ensure_jaeger_connection(self.base_url, self.namespace)

    def query_traces(
        self,
        service_name: str,
        start_time_us: Optional[int] = None,
        end_time_us: Optional[int] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Query traces for a specific service within an optional time range."""
        if not self.is_connected():
            return []

        url = f"{self.base_url}/api/traces?service={service_name}&limit={limit}"
        if start_time_us is not None:
            url += f"&start={start_time_us}"
        if end_time_us is not None:
            url += f"&end={end_time_us}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ChaosLab"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("data", []) or []
        except Exception as e:
            # Service name might have prefix or suffix, fallback search
            return []

    def analyze_traces(self, traces: List[Dict[str, Any]], target_service: str = None) -> Dict[str, Any]:
        """Analyze raw traces to calculate span counts, error rates, and latency percentiles."""
        if not traces:
            return {
                "available": True,
                "traces_count": 0,
                "spans_count": 0,
                "error_spans_count": 0,
                "error_rate_pct": 0.0,
                "latency_ms": {
                    "min": 0.0,
                    "avg": 0.0,
                    "p50": 0.0,
                    "p95": 0.0,
                    "max": 0.0
                },
                "sample_trace_ids": [],
                "operations": []
            }

        durations_ms: List[float] = []
        total_spans = 0
        error_spans = 0
        operations = set()
        trace_ids = []

        for trace in traces:
            t_id = trace.get("traceID")
            if t_id and t_id not in trace_ids and len(trace_ids) < 5:
                trace_ids.append(t_id)

            spans = trace.get("spans", [])
            for span in spans:
                total_spans += 1
                op = span.get("operationName", "")
                if op:
                    operations.add(op)

                # Duration is stored in microseconds in Jaeger
                duration_us = span.get("duration", 0)
                duration_ms = duration_us / 1000.0
                durations_ms.append(duration_ms)

                # Detect errors
                is_error = False
                for tag in span.get("tags", []):
                    k = tag.get("key", "")
                    v = tag.get("value")
                    if k == "error" and (v is True or str(v).lower() == "true"):
                        is_error = True
                        break
                    if k == "otel.status_code" and str(v).upper() == "ERROR":
                        is_error = True
                        break
                    if k == "rpc.grpc.status_code" and v not in [0, "0", None]:
                        is_error = True
                        break

                if is_error:
                    error_spans += 1

        error_rate_pct = round((error_spans / total_spans * 100.0), 2) if total_spans > 0 else 0.0
        avg_lat = sum(durations_ms) / len(durations_ms) if durations_ms else 0.0

        return {
            "available": True,
            "traces_count": len(traces),
            "spans_count": total_spans,
            "error_spans_count": error_spans,
            "error_rate_pct": error_rate_pct,
            "latency_ms": {
                "min": round(min(durations_ms), 2) if durations_ms else 0.0,
                "avg": round(avg_lat, 2),
                "p50": round(calculate_percentile(durations_ms, 50), 2),
                "p95": round(calculate_percentile(durations_ms, 95), 2),
                "max": round(max(durations_ms), 2) if durations_ms else 0.0
            },
            "sample_trace_ids": trace_ids,
            "operations": sorted(list(operations))
        }

    def get_service_telemetry(
        self,
        service_name: str,
        start_time_us: Optional[int] = None,
        end_time_us: Optional[int] = None,
        limit: int = 50
    ) -> Dict[str, Any]:
        """Fetch and analyze telemetry for a target service."""
        try:
            traces = self.query_traces(service_name, start_time_us, end_time_us, limit)
            result = self.analyze_traces(traces, service_name)
            result["service"] = service_name
            return result
        except Exception as e:
            return {
                "available": False,
                "error": str(e),
                "service": service_name,
                "traces_count": 0,
                "spans_count": 0,
                "error_spans_count": 0,
                "error_rate_pct": 0.0,
                "latency_ms": {"min": 0, "avg": 0, "p50": 0, "p95": 0, "max": 0},
                "sample_trace_ids": [],
                "operations": []
            }
