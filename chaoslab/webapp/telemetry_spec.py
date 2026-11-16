"""
Tracing (telemetry) checks for applications in connected clusters (server side)
==============================================================================
The user names WHAT to check: which traced service, the post-recovery p95 latency limit, and optionally a caller to
send traffic through during the fault so failures show up as error spans. WHERE Jaeger runs is configured on the
agent only (`--jaeger`); the server never sends network endpoints. The agent re-validates everything locally
(chaoslab/agent/safe_telemetry.py; a parity test keeps the two validators in sync).
"""

import re
from typing import Any, Dict, Iterable, Optional, Tuple

from . import probe_spec

SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def validate(spec: Any, target_service: str, namespace_services: Optional[Iterable[str]] = None
             ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """-> (clean spec, None) or (None, reason). `namespace_services` restricts the traffic caller to known services."""
    if not isinstance(spec, dict):
        return None, "telemetry must be an object"
    extra = set(spec) - {"service_name", "max_allowed_p95_ms", "verify_error_spike", "traffic"}
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
        svc = traffic.get("service")
        if not isinstance(svc, str) or not DNS_LABEL.match(svc):
            return None, "telemetry.traffic.service is invalid"
        if namespace_services is not None and svc not in set(namespace_services):
            return None, f"telemetry.traffic.service '{svc}' is not a service in this application's namespace"
        http, err = probe_spec._http({"port": traffic.get("port"), "path": traffic.get("path")}, "telemetry.traffic")
        if err:
            return None, err
        clean["traffic"] = {"service": svc, "port": http["port"], "path": http["path"]}
    if verify and "traffic" not in clean:
        return None, "telemetry.verify_error_spike needs traffic during the fault (telemetry.traffic)"
    return clean, None


def to_engine_telemetry(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The job's `telemetry` block: the agent executes it; the server's Oracle reads the same keys."""
    return {"enabled": True, **spec}
