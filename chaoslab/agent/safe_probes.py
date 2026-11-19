"""
Safe application probes for the ChaosLab agent
==============================================
Declarative, read-only verification of a customer's application, with NO code execution:

  * HTTP GET only, to a port of a Ready pod of the target service in an allowed namespace, through a
    short-lived `kubectl port-forward` bound to 127.0.0.1 (no `kubectl exec`, no scripts, no user-supplied host)
  * or, for gRPC-only services, the standard read-only `grpc.health.v1.Health/Check` call through the same
    port-forward (a minimal HTTP/2 client from the standard library: one fixed request, nothing user-supplied but
    the port and an optional health service name)
  * every field is validated against a strict whitelist (port range, path charset, status range, bounded strings)
  * redirects are not followed, response bodies are capped, every call has a timeout
  * log check = `kubectl logs --tail` plus plain substring matching (no regex, no commands)

Contrast with the engine's own probes, which paste config values into `python -c` scripts run inside the
container: those stay disabled for customer clusters.
"""

import http.client
import queue
import re
import socket
import struct
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
GRPC_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.]{1,100}$")      # e.g. hipstershop.PaymentService ("" = whole server)


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


def _validate_health(spec: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """HTTP GET (default) or {"type": "grpc", "port": N[, "service": NAME]} for the standard gRPC health check."""
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
    return _validate_http(spec, "health")


def validate_probe_spec(spec: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """-> (clean spec, None) or (None, reason). Health is mandatory; functional and logs are optional."""
    if not isinstance(spec, dict):
        return None, "probes must be an object"
    extra = set(spec) - {"health", "functional", "logs"}
    if extra:
        return None, f"unsupported probe section(s): {', '.join(sorted(extra))}"
    health, err = _validate_health(spec.get("health"))
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
    """`kubectl port-forward pod/NAME :PORT` (or svc/NAME) on 127.0.0.1, torn down on exit."""

    def __init__(self, namespace: str, pod: str, port: int, startup_timeout: float = 10.0, kind: str = "pod"):
        if (kind not in ("pod", "svc") or not DNS_LABEL.match(namespace)
                or not re.match(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$", pod) or not _int(port, 1, 65535)):
            raise ProbeError("invalid forward target")
        self.args = ["kubectl", "port-forward", "-n", namespace, f"{kind}/{pod}", f":{port}", "--address", "127.0.0.1"]
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


def http_get(local_port: int, path: str, timeout: float = 5.0, max_body: int = MAX_BODY) -> Dict[str, Any]:
    """One GET to 127.0.0.1:local_port. Redirects are NOT followed; the body is capped."""
    started = time.time()
    conn = http.client.HTTPConnection("127.0.0.1", local_port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": "localhost", "User-Agent": "chaoslab-agent-probe", "Connection": "close"})
        resp = conn.getresponse()
        body = resp.read(max_body + 1)[:max_body]
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
    if "error" in result:
        # The forward worked but nothing answered HTTP: almost always the wrong port (or not an HTTP server).
        detail = (f"no HTTP response from port {spec['port']} of pod {pod} ({result['error'][:80]}). "
                  f"Is the application listening on port {spec['port']}? Check the container ports with: "
                  f"kubectl get pod {pod} -n {namespace} -o jsonpath='{{.spec.containers[*].ports[*].containerPort}}'")
    return {"passed": passed, "status_code": result.get("status"), "latency_ms": result.get("latency_ms"),
            "status": f"{result['status']} OK" if passed else (f"HTTP {result['status']}" if "status" in result else "UNREACHABLE"),
            "detail": detail, "path": spec["path"], "port": spec["port"]}


# ============================================================================ gRPC health check (grpc.health.v1)
GRPC_HEALTH_STATUS = {0: "UNKNOWN", 1: "SERVING", 2: "NOT_SERVING", 3: "SERVICE_UNKNOWN"}
_H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
_DATA, _HEADERS, _RST, _SETTINGS, _PING, _GOAWAY = 0x0, 0x1, 0x3, 0x4, 0x6, 0x7
_END_STREAM, _ACK, _PADDED = 0x1, 0x1, 0x8
MAX_GRPC_READ = 64 * 1024


def _frame(ftype: int, flags: int, stream: int, payload: bytes = b"") -> bytes:
    return struct.pack(">I", len(payload))[1:] + bytes([ftype, flags]) + struct.pack(">I", stream) + payload


def _hpack_literal(name: str, value: str) -> bytes:
    """'Literal header field without indexing, new name', no Huffman: every name/value here is < 127 bytes."""
    n, v = name.encode(), value.encode()
    return b"\x00" + bytes([len(n)]) + n + bytes([len(v)]) + v


def _health_request(service: str) -> Tuple[bytes, bytes]:
    headers = b"".join(_hpack_literal(k, v) for k, v in (
        (":method", "POST"), (":scheme", "http"), (":path", "/grpc.health.v1.Health/Check"), (":authority", "localhost"),
        ("content-type", "application/grpc"), ("te", "trailers"), ("user-agent", "chaoslab-agent-probe")))
    msg = (b"\x0a" + bytes([len(service)]) + service.encode()) if service else b""   # HealthCheckRequest{service}
    return headers, b"\x00" + struct.pack(">I", len(msg)) + msg


def _health_status(msg: bytes) -> int:
    """HealthCheckResponse{ status = 1 (enum) } -> the status number (0 when absent, as protobuf defines)."""
    i, status = 0, 0
    while i < len(msg):
        key, i = msg[i], i + 1
        if key & 0x07 != 0:                       # only varint fields are expected in this message
            break
        value, shift = 0, 0
        while i < len(msg):
            b, i = msg[i], i + 1
            value |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        if key == 0x08:
            status = value
    return status


def grpc_health_check(local_port: int, service: str = "", timeout: float = 5.0) -> Dict[str, Any]:
    """One unary grpc.health.v1.Health/Check over cleartext HTTP/2 to 127.0.0.1:local_port.
    -> {"serving_status": "SERVING"|..., "latency_ms"} or {"error": "..."}. Reads at most 64 KB."""
    started = time.time()
    deadline = started + timeout
    headers, body = _health_request(service)
    try:
        sock = socket.create_connection(("127.0.0.1", local_port), timeout=timeout)
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"[:160]}
    state = {"buf": b"", "received": 0}

    def read_exact(n: int) -> bytes:
        while len(state["buf"]) < n:
            if time.time() > deadline:
                raise TimeoutError("no gRPC response in time")
            sock.settimeout(max(0.1, deadline - time.time()))
            chunk = sock.recv(16384)
            if not chunk:
                raise ConnectionError("connection closed by the server")
            state["received"] += len(chunk)
            if state["received"] > MAX_GRPC_READ:
                raise ConnectionError("response too large")
            state["buf"] += chunk
        out, state["buf"] = state["buf"][:n], state["buf"][n:]
        return out

    data = b""
    try:
        sock.sendall(_H2_PREFACE + _frame(_SETTINGS, 0, 0) + _frame(_HEADERS, 0x4, 1, headers)
                     + _frame(_DATA, _END_STREAM, 1, body))
        while True:
            head = read_exact(9)
            length, ftype, flags = int.from_bytes(head[:3], "big"), head[3], head[4]
            stream = int.from_bytes(head[5:], "big") & 0x7FFFFFFF
            payload = read_exact(length)
            if ftype == _SETTINGS and not flags & _ACK:
                sock.sendall(_frame(_SETTINGS, _ACK, 0))
            elif ftype == _PING and not flags & _ACK:
                sock.sendall(_frame(_PING, _ACK, 0, payload))
            elif ftype == _GOAWAY:
                return {"error": "the server closed the HTTP/2 connection (GOAWAY): is this port a gRPC server?"}
            elif stream == 1 and ftype == _RST:
                return {"error": f"the server reset the call (HTTP/2 error {int.from_bytes(payload[:4], 'big')})"}
            elif stream == 1 and ftype == _DATA:
                if flags & _PADDED and payload:
                    payload = payload[1:len(payload) - payload[0]]
                data += payload
                if len(data) >= 5 and len(data) >= 5 + int.from_bytes(data[1:5], "big"):
                    break
            elif stream == 1 and ftype == _HEADERS and flags & _END_STREAM:
                # trailers and no message: the call failed (e.g. UNIMPLEMENTED: no health service, or NOT_FOUND)
                return {"error": "the server answered without a health status: it may not implement "
                                 "grpc.health.v1 (or does not know this service name)"}
    except (OSError, ConnectionError, TimeoutError) as e:
        return {"error": f"no gRPC response ({type(e).__name__}: {e})"[:160]}
    finally:
        sock.close()
    if data[0] != 0:
        return {"error": "compressed gRPC responses are not supported"}
    status = _health_status(data[5:5 + int.from_bytes(data[1:5], "big")])
    return {"serving_status": GRPC_HEALTH_STATUS.get(status, f"STATUS_{status}"),
            "latency_ms": round((time.time() - started) * 1000, 1)}


def probe_grpc(spec: Dict[str, Any], namespace: str, pod: str, forward_factory: Optional[Callable] = None,
               timeout: float = 5.0) -> Dict[str, Any]:
    forward_factory = forward_factory or PortForward          # resolved at call time (patchable in tests)
    service = spec.get("service", "")
    try:
        with forward_factory(namespace, pod, spec["port"]) as local_port:
            result = grpc_health_check(local_port, service, timeout)
    except ProbeError as e:
        return {"passed": False, "status": "UNREACHABLE", "detail": str(e), "port": spec["port"]}
    if "error" in result:
        return {"passed": False, "status": "UNREACHABLE", "port": spec["port"],
                "detail": f"gRPC health check on port {spec['port']} of pod {pod} failed: {result['error']}"}
    passed = result["serving_status"] == "SERVING"
    what = f"service '{service}'" if service else "server"
    return {"passed": passed, "status": result["serving_status"], "latency_ms": result["latency_ms"],
            "port": spec["port"], "detail": f"gRPC health ({what}): {result['serving_status']} in {result['latency_ms']} ms"}


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
        "enabled": True, "probe_type": "grpc" if spec["health"].get("type") == "grpc" else "http",
        "safe_probes": True, "probed_pod": None,
        "health_check_passed": False, "health_status": "UNKNOWN", "functional_test_passed": False,
        "transaction_id": None, "logs_verified": False, "startup_log_detected": False, "log_error_count": 0}
    deadline = clock() + wait_seconds
    health = None
    while True:
        pods = ready_pods(namespace, service, kubectl)
        if pods:
            evidence["probed_pod"] = pods[0]
            check = probe_grpc if spec["health"].get("type") == "grpc" else probe_http
            health = check(spec["health"], namespace, pods[0], forward_factory)
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
