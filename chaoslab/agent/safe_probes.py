"""
Safe application probes for the ChaosLab agent
==============================================
Declarative, read-only verification of a customer's application, with NO code execution:

  * HTTP GET only, to a port of a Ready pod of the target service in an allowed namespace, through a
    short-lived `kubectl port-forward` bound to 127.0.0.1 (no `kubectl exec`, no scripts, no user-supplied host)
  * every field is validated against a strict whitelist (port range, path charset, status range, bounded strings)
  * redirects are not followed, response bodies are capped, every call has a timeout
  * log check = `kubectl logs --tail` plus plain substring matching (no regex, no commands)

Contrast with the engine's own probes, which paste config values into `python -c` scripts run inside the
container: those stay disabled for customer clusters.
"""

import http.client
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

PATH_RE = re.compile(r"^/[A-Za-z0-9._~/%:@+=&?,-]{0,199}$")
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
MAX_BODY = 64 * 1024
MAX_TEXT = 100
MAX_PROHIBITED = 10
PRINTABLE = re.compile(r"^[^\x00-\x1f\x7f]*$")


class ProbeError(Exception):
    pass


# ============================================================================ spec validation
def _int(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _text(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v) <= MAX_TEXT and bool(PRINTABLE.match(v))


def _validate_http(spec: Any, where: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
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


def validate_probe_spec(spec: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """-> (clean spec, None) or (None, reason). Health is mandatory; functional and logs are optional."""
    if not isinstance(spec, dict):
        return None, "probes must be an object"
    extra = set(spec) - {"health", "functional", "logs"}
    if extra:
        return None, f"unsupported probe section(s): {', '.join(sorted(extra))}"
    health, err = _validate_http(spec.get("health"), "health")
    if err:
        return None, err
    clean: Dict[str, Any] = {"health": health}
    if spec.get("functional") is not None:
        functional, err = _validate_http(spec["functional"], "functional")
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


# ============================================================================ port-forward + HTTP GET
class PortForward:
    """`kubectl port-forward pod/NAME :PORT` on 127.0.0.1, torn down on exit."""

    def __init__(self, namespace: str, pod: str, port: int, startup_timeout: float = 10.0):
        if not DNS_LABEL.match(namespace) or not re.match(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$", pod) or not _int(port, 1, 65535):
            raise ProbeError("invalid forward target")
        self.args = ["kubectl", "port-forward", "-n", namespace, f"pod/{pod}", f":{port}", "--address", "127.0.0.1"]
        self.startup_timeout = startup_timeout
        self.proc: Optional[subprocess.Popen] = None
        self.local_port = 0

    def __enter__(self) -> int:
        try:
            self.proc = subprocess.Popen(self.args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except FileNotFoundError:
            raise ProbeError("kubectl not found")
        lines: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in self.proc.stdout], daemon=True).start()
        deadline = time.time() + self.startup_timeout
        seen: List[str] = []
        while time.time() < deadline:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
                continue
            seen.append(line.strip())
            m = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
            if m:
                self.local_port = int(m.group(1))
                return self.local_port
        self.__exit__()
        raise ProbeError("port-forward failed: " + (" | ".join(seen)[:200] or "no output"))

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def http_get(local_port: int, path: str, timeout: float = 5.0) -> Dict[str, Any]:
    """One GET to 127.0.0.1:local_port. Redirects are NOT followed; the body is capped."""
    started = time.time()
    conn = http.client.HTTPConnection("127.0.0.1", local_port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": "localhost", "User-Agent": "chaoslab-agent-probe", "Connection": "close"})
        resp = conn.getresponse()
        body = resp.read(MAX_BODY + 1)[:MAX_BODY]
        return {"status": resp.status, "body": body.decode("utf-8", errors="replace"),
                "latency_ms": round((time.time() - started) * 1000, 1)}
    except (OSError, http.client.HTTPException) as e:
        return {"error": f"{type(e).__name__}: {e}"[:160]}
    finally:
        conn.close()


def _check(spec: Dict[str, Any], result: Dict[str, Any]) -> Tuple[bool, str]:
    if "error" in result:
        return False, result["error"]
    expected = spec.get("expected_status", 200)
    if result["status"] != expected:
        return False, f"HTTP {result['status']} (expected {expected})"
    needle = spec.get("expected_body_contains")
    if needle and needle.lower() not in result["body"].lower():
        return False, f"HTTP {result['status']} but the body does not contain the expected text"
    return True, f"HTTP {result['status']} in {result['latency_ms']} ms"


def probe_http(spec: Dict[str, Any], namespace: str, pod: str, forward_factory: Optional[Callable] = None,
               timeout: float = 5.0) -> Dict[str, Any]:
    forward_factory = forward_factory or PortForward          # resolved at call time (patchable in tests)
    try:
        with forward_factory(namespace, pod, spec["port"]) as local_port:
            result = http_get(local_port, spec["path"], timeout)
    except ProbeError as e:
        return {"passed": False, "status": "UNREACHABLE", "detail": str(e)}
    passed, detail = _check(spec, result)
    return {"passed": passed, "status_code": result.get("status"), "latency_ms": result.get("latency_ms"),
            "status": f"{result['status']} OK" if passed else (f"HTTP {result['status']}" if "status" in result else "UNREACHABLE"),
            "detail": detail, "path": spec["path"], "port": spec["port"]}


# ============================================================================ pods + logs via injected kubectl
def ready_pods(namespace: str, service: str, kubectl: Callable) -> List[str]:
    """Ready, non-terminating pods of the service, newest first."""
    import json
    ok, out = kubectl(["get", "pods", "-n", namespace, "-l", f"app={service}", "-o", "json"])
    if not ok:
        return []
    pods = []
    try:
        for p in json.loads(out).get("items", []):
            if p["metadata"].get("deletionTimestamp") or p.get("status", {}).get("phase") != "Running":
                continue
            cs = p["status"].get("containerStatuses") or []
            if cs and all(c.get("ready") for c in cs):
                pods.append((p["metadata"].get("creationTimestamp", ""), p["metadata"]["name"]))
    except (ValueError, KeyError):
        return []
    return [name for _, name in sorted(pods, reverse=True)]


def check_logs(spec: Dict[str, Any], namespace: str, pod: str, kubectl: Callable) -> Dict[str, Any]:
    ok, out = kubectl(["logs", pod, "-n", namespace, "--tail=300"], 30)
    if not ok:
        return {"logs_verified": False, "startup_log_detected": False, "log_error_count": 1, "error": out[:200]}
    low = out.lower()
    expected = spec.get("expected_log_pattern")
    startup = (expected.lower() in low) if expected else True
    bad = [line.strip()[:200] for line in out.splitlines()
           if any(p.lower() in line.lower() for p in spec.get("prohibited_patterns", []))]
    return {"logs_verified": True, "startup_log_detected": startup, "log_error_count": len(bad), "fatal_errors": bad[:5],
            "details": f"expected pattern found: {startup}; prohibited matches: {len(bad)}"}


# ============================================================================ orchestration
def run_probes(spec: Dict[str, Any], namespace: str, service: str, kubectl: Callable,
               forward_factory: Optional[Callable] = None, wait_seconds: float = 0.0, interval: float = 2.0,
               sleep: Callable = time.sleep, clock: Callable = time.time) -> Dict[str, Any]:
    """
    Execute the validated probe spec against the service's newest Ready pod. Health/functional are retried
    until they pass or `wait_seconds` elapse (the replacement pod may still be warming up).
    Returns evidence in the same shape the ChaosLab Oracle consumes (`application` evidence).
    """
    evidence: Dict[str, Any] = {
        "enabled": True, "probe_type": "http", "safe_probes": True, "probed_pod": None,
        "health_check_passed": False, "health_status": "UNKNOWN", "functional_test_passed": False,
        "transaction_id": None, "logs_verified": False, "startup_log_detected": False, "log_error_count": 0}
    deadline = clock() + wait_seconds
    health = None
    while True:
        pods = ready_pods(namespace, service, kubectl)
        if pods:
            evidence["probed_pod"] = pods[0]
            health = probe_http(spec["health"], namespace, pods[0], forward_factory)
            if health["passed"]:
                break
        else:
            health = {"passed": False, "status": "NO_READY_POD", "detail": f"no Ready pod found for app={service}"}
        if clock() >= deadline:
            break
        sleep(interval)
    evidence["health_check_passed"] = health["passed"]
    evidence["health_status"] = health["status"]
    evidence["health_details"] = {"error": None if health["passed"] else health.get("detail"), **health}
    pod = evidence["probed_pod"]

    if "functional" in spec:
        if pod:
            func = probe_http(spec["functional"], namespace, pod, forward_factory)
            evidence["functional_test_passed"] = func["passed"]
            evidence["functional_details"] = {"error": None if func["passed"] else func.get("detail"), **func}
        else:
            evidence["functional_details"] = {"error": "no Ready pod to probe"}
    else:
        evidence["functional_test_skipped"] = True

    if "logs" in spec and pod:
        logs = check_logs(spec["logs"], namespace, pod, kubectl)
        evidence["logs_verified"] = logs.get("logs_verified", False)
        evidence["startup_log_detected"] = logs.get("startup_log_detected", False)
        evidence["log_error_count"] = logs.get("log_error_count", 0)
        evidence["log_details"] = logs
    return evidence
