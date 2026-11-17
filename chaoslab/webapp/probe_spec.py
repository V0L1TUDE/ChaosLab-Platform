"""
Declarative verification probes for applications in connected clusters (server side)
===================================================================================
The user describes WHAT to check (never how): a health check (mandatory: HTTP GET, or the standard gRPC health
check for gRPC-only services), an optional functional GET check, and optional log substring checks. The server validates the spec with the same strict whitelist the agent
enforces locally (see chaoslab/agent/safe_probes.py; a parity test keeps the two in sync). The agent executes the
probes without any code execution: GET through a local port-forward, `kubectl logs`, substring matching.
"""

import re
from typing import Any, Dict, Optional, Tuple

PATH_RE = re.compile(r"^/[A-Za-z0-9._~/%:@+=&?,-]{0,199}$")
MAX_TEXT = 100
MAX_PROHIBITED = 10
PRINTABLE = re.compile(r"^[^\x00-\x1f\x7f]*$")
GRPC_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.]{1,100}$")


def _int(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _text(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v) <= MAX_TEXT and bool(PRINTABLE.match(v))


def _http(spec: Any, where: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(spec, dict):
        return None, f"{where}: must be an object"
    extra = set(spec) - {"port", "path", "expected_status", "expected_body_contains"}
    if extra:
        return None, f"{where}: unsupported field(s): {', '.join(sorted(extra))}"
    if not _int(spec.get("port"), 1, 65535):
        return None, f"{where}.port: must be 1-65535"
    path = spec.get("path")
    if not isinstance(path, str) or not PATH_RE.match(path) or ".." in path.split("?")[0].split("/"):
        return None, f"{where}.path: must start with '/' and use only URL-safe characters (no host, no '..')"
    status = spec.get("expected_status", 200)
    if not _int(status, 100, 599):
        return None, f"{where}.expected_status: must be 100-599"
    clean: Dict[str, Any] = {"port": spec["port"], "path": path, "expected_status": status}
    if spec.get("expected_body_contains") not in (None, ""):
        if not _text(spec["expected_body_contains"]):
            return None, f"{where}.expected_body_contains: 1-{MAX_TEXT} printable characters"
        clean["expected_body_contains"] = spec["expected_body_contains"]
    return clean, None


def _health(spec: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """HTTP GET (default) or {"type": "grpc", "port": N[, "service": NAME]}: the standard grpc.health.v1 check,
    which the agent sends through the same port-forward (no exec)."""
    if isinstance(spec, dict) and spec.get("type") == "grpc":
        extra = set(spec) - {"type", "port", "service"}
        if extra:
            return None, f"health: unsupported field(s) for a gRPC check: {', '.join(sorted(extra))}"
        if not _int(spec.get("port"), 1, 65535):
            return None, "health.port: must be 1-65535"
        clean: Dict[str, Any] = {"type": "grpc", "port": spec["port"]}
        if spec.get("service") not in (None, ""):
            if not isinstance(spec["service"], str) or not GRPC_SERVICE_RE.match(spec["service"]):
                return None, "health.service: a gRPC service name (letters, digits, '.', '_'; up to 100)"
            clean["service"] = spec["service"]
        return clean, None
    if isinstance(spec, dict) and "type" in spec:
        if spec["type"] != "http":
            return None, "health.type: must be 'http' or 'grpc'"
        spec = {k: v for k, v in spec.items() if k != "type"}
    return _http(spec, "health")


def validate(spec: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """-> (clean spec, None) or (None, reason). Health is mandatory; functional and logs are optional."""
    if not isinstance(spec, dict):
        return None, "probes must be an object"
    extra = set(spec) - {"health", "functional", "logs"}
    if extra:
        return None, f"unsupported probe section(s): {', '.join(sorted(extra))}"
    health, err = _health(spec.get("health"))
    if err:
        return None, err
    clean: Dict[str, Any] = {"health": health}
    if spec.get("functional") is not None:
        functional, err = _http(spec["functional"], "functional")
        if err:
            return None, err
        clean["functional"] = functional
    logs = spec.get("logs")
    if logs is not None:
        if not isinstance(logs, dict) or set(logs) - {"expected_log_pattern", "prohibited_patterns"}:
            return None, "logs: unsupported fields"
        l: Dict[str, Any] = {}
        if logs.get("expected_log_pattern") not in (None, ""):
            if not _text(logs["expected_log_pattern"]):
                return None, f"logs.expected_log_pattern: 1-{MAX_TEXT} printable characters"
            l["expected_log_pattern"] = logs["expected_log_pattern"]
        pp = logs.get("prohibited_patterns", [])
        if not (isinstance(pp, list) and len(pp) <= MAX_PROHIBITED and all(_text(x) for x in pp)):
            return None, f"logs.prohibited_patterns: up to {MAX_PROHIBITED} strings of 1-{MAX_TEXT} printable characters"
        l["prohibited_patterns"] = pp
        clean["logs"] = l
    return clean, None


def to_engine_application(probes: Dict[str, Any]) -> Dict[str, Any]:
    """
    The `application` block of the job's engine config. The agent executes `probes`; the server's Oracle reads
    the remaining keys (the same schema the Oracle already uses) to decide which invariants apply.
    """
    h = probes["health"]
    health_probe = ({"type": "grpc", "port": h["port"], **({"service": h["service"]} if "service" in h else {})}
                    if h.get("type") == "grpc" else
                    {"type": "http", "port": h["port"], "path": h["path"], "expected_status": h["expected_status"]})
    app: Dict[str, Any] = {"enabled": True, "probes": probes, "health_probe": health_probe}
    if "functional" in probes:
        app["functional_check"] = {"enabled": True, "type": "http", **probes["functional"]}
    if "logs" in probes:
        app["verify_logs"] = True
        app["expected_log_pattern"] = probes["logs"].get("expected_log_pattern", "started")
    return app
