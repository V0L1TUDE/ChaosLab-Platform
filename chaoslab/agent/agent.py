#!/usr/bin/env python3
"""
ChaosLab Agent: runs resilience experiments inside YOUR Kubernetes environment for the ChaosLab web app.

Design (security first)
  * OUTBOUND ONLY. The agent polls the ChaosLab server over HTTPS; nothing connects in to your cluster.
  * Jobs are DATA, never commands. The server sends a declarative experiment config; the agent re-validates it
    against its OWN local policy (allowed namespaces, faults, max duration) and rebuilds a minimal config.
    A compromised or buggy server cannot make the agent touch other namespaces or run arbitrary commands.
  * Application probes are declarative and read-only (HTTP GET through a local port-forward, log substring checks);
    the engine's own exec-based probes stay disabled. Probes run BEFORE the fault (so a misconfigured probe never
    causes an outage for nothing) and again after recovery.
  * Least privilege: it only needs kubectl access with the Role in agent-rbac.yaml (see README).
  * It sends back evidence (pod names, readiness, Chaos Mesh status, Kubernetes events), never kubeconfig,
    secrets or workload data. The server verifies the evidence with its own Verification Oracle.
  * Kill switches: Ctrl+C aborts the current fault; the server-side "emergency stop" is honoured within seconds.

Usage
  python agent.py --server https://chaoslab.example.com --token cle_XXXX --allowed-namespaces shop,payments
  (the token is single-use; the agent stores a revocable credential in --state-dir afterwards)
"""

import argparse
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

VERSION = "1.1.0"
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
DURATION_RE = re.compile(r"^(\d{1,3})s$")
LATENCY_RE = re.compile(r"^\d{1,4}(ms|s)$")
ENGINE_FAULTS = ("pod-kill", "network-delay", "network-loss")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_probes  # noqa: E402  (HTTP GET / log probes: no exec, no scripts)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================================ local policy (authoritative)
class Policy:
    def __init__(self, namespaces: List[str], faults: Tuple[str, ...] = ENGINE_FAULTS, max_duration: int = 120,
                 allow_probes: bool = True):
        bad = [n for n in namespaces if not DNS_LABEL.match(n)]
        if not namespaces:
            raise ValueError("allowed namespaces must be a non-empty list of valid namespace names")
        if bad:
            raise ValueError("invalid namespace name(s): " + ", ".join(repr(n) for n in bad)
                             + " (use lowercase letters, digits and '-' only; no dots, spaces or trailing punctuation)")
        self.namespaces = sorted(set(namespaces))
        self.faults = tuple(f for f in faults if f in ENGINE_FAULTS)
        self.max_duration = max_duration
        self.allow_probes = allow_probes


def validate_job(job: Dict[str, Any], policy: Policy) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Returns (sanitized_engine_config, None) or (None, reason). The sanitized config is REBUILT from whitelisted
    fields; anything else the server sent is dropped. The engine's own application probes and telemetry stay
    disabled (they can carry exec commands); only the validated declarative `probes` run, via safe_probes.
    """
    try:
        cfg = job["engine_config"]
        if not isinstance(cfg, dict):
            return None, "engine_config must be an object"
        name = cfg.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            return None, "invalid experiment name"
        target = cfg.get("target") or {}
        ns, label = target.get("namespace"), target.get("label") or {}
        if ns not in policy.namespaces:
            return None, f"namespace '{ns}' is not in this agent's allow-list ({', '.join(policy.namespaces)})"
        if not (isinstance(label, dict) and set(label) == {"app"} and isinstance(label["app"], str) and DNS_LABEL.match(label["app"])):
            return None, "target must select pods by a single 'app' label"
        fault = cfg.get("fault") or {}
        ftype = fault.get("type")
        if ftype not in policy.faults:
            return None, f"fault '{ftype}' is not allowed by this agent (allowed: {', '.join(policy.faults)})"
        if fault.get("mode", "one") != "one":
            return None, "only mode 'one' is allowed"
        clean_fault: Dict[str, Any] = {"type": ftype, "mode": "one"}
        if ftype != "pod-kill":
            m = DURATION_RE.match(str(fault.get("duration", "")))
            if not m or not 1 <= int(m.group(1)) <= policy.max_duration:
                return None, f"fault duration must be 1-{policy.max_duration}s"
            clean_fault["duration"] = fault["duration"]
        if ftype == "network-delay":
            for k, default in (("latency", "200ms"), ("jitter", "10ms")):
                v = str(fault.get(k, default))
                if not LATENCY_RE.match(v):
                    return None, f"invalid {k}"
                clean_fault[k] = v
        if ftype == "network-loss":
            pct = str(fault.get("loss_pct", "100"))
            if not (pct.isdigit() and 1 <= int(pct) <= 100):
                return None, "invalid loss_pct"
            clean_fault["loss_pct"] = pct
        rec = cfg.get("recovery") or {}
        timeout, replicas = rec.get("timeout_seconds", 60), rec.get("expected_ready_replicas", 1)
        if not (isinstance(timeout, int) and 1 <= timeout <= 600 and isinstance(replicas, int) and 1 <= replicas <= 10):
            return None, "invalid recovery settings"
        if ftype == "pod-kill" and timeout > policy.max_duration:
            return None, f"pod-kill recovery timeout must be <= {policy.max_duration}s"
        runtime = job.get("max_runtime_seconds", 300)
        if not (isinstance(runtime, int) and 30 <= runtime <= 1800):
            return None, "invalid max_runtime_seconds"
        # Declarative probes (HTTP GET + logs) are validated against the agent's own strict whitelist. The engine's
        # exec-based probes are never enabled; `probes` is an agent-internal key executed by safe_probes.
        probes = None
        app_cfg = cfg.get("application")
        if policy.allow_probes and isinstance(app_cfg, dict) and app_cfg.get("enabled") and "probes" in app_cfg:
            probes, perr = safe_probes.validate_probe_spec(app_cfg["probes"])
            if perr:
                return None, f"invalid probe spec: {perr}"
        return {"name": name, "target": {"namespace": ns, "label": {"app": label["app"]}}, "fault": clean_fault,
                "recovery": {"timeout_seconds": timeout, "expected_ready_replicas": replicas},
                "application": {"enabled": False}, "telemetry": {"enabled": False}, "probes": probes}, None
    except (KeyError, TypeError, ValueError) as e:
        return None, f"malformed job: {type(e).__name__}"


# ============================================================================ server API client
class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class Api:
    def __init__(self, server: str, allow_http: bool = False):
        parsed = urllib.parse.urlparse(server)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("--server must be a URL like https://chaoslab.example.com")
        if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS and not allow_http:
            raise ValueError("Refusing to send credentials over plain HTTP. Use https:// (or --insecure-allow-http for testing).")
        self.base = server.rstrip("/")

    def post(self, path: str, body: Dict[str, Any], bearer: Optional[str] = None, timeout: float = 40) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": f"chaoslab-agent/{VERSION}"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", e.reason)
            except Exception:
                msg = str(e.reason)
            raise ApiError(e.code, msg)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            raise ApiError(0, f"cannot reach server: {getattr(e, 'reason', e)}")


# ============================================================================ kubectl helpers
def kubectl(args: List[str], timeout: int = 25) -> Tuple[bool, str]:
    try:
        res = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)
        return res.returncode == 0, (res.stdout if res.returncode == 0 else res.stderr).strip()
    except FileNotFoundError:
        return False, "kubectl not found"
    except Exception as e:
        return False, str(e)


def cluster_identity() -> Tuple[Optional[str], str]:
    """(kube-system namespace UID, server version): a stable identity for this cluster."""
    ok, uid = kubectl(["get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"])
    ok2, ver = kubectl(["version", "-o", "json"])
    version = ""
    if ok2:
        try:
            version = json.loads(ver).get("serverVersion", {}).get("gitVersion", "")
        except ValueError:
            pass
    return (uid if ok and uid else None), version


def collect_inventory(namespaces: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    inv: Dict[str, List[Dict[str, Any]]] = {}
    for ns in namespaces:
        ok, out = kubectl(["get", "deployments", "-n", ns, "-o", "json"])
        items = []
        if ok:
            try:
                for d in json.loads(out).get("items", []):
                    spec, status = d.get("spec", {}), d.get("status", {})
                    labels = spec.get("selector", {}).get("matchLabels") or {}
                    items.append({"deployment": d["metadata"]["name"], "app_label": labels.get("app"),
                                  "replicas": spec.get("replicas", 1), "ready": status.get("readyReplicas", 0) or 0})
            except (ValueError, KeyError):
                pass
        inv[ns] = items
    return inv


# ============================================================================ job execution
class Reporter:
    """Buffers progress for a job and flushes it to the server; learns about cancellation from the replies."""

    def __init__(self, api: Api, job: Dict[str, Any]):
        self.api, self.job = api, job
        self.events: List[Dict[str, Any]] = []
        self.samples: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.cancelled = threading.Event()

    def event(self, stage: str, message: str, percent: Optional[int] = None, level: str = "info") -> None:
        with self.lock:
            self.events.append({"stage": stage, "message": message[:480], "percent": percent, "level": level})

    def sample(self, point: Dict[str, Any]) -> None:
        with self.lock:
            self.samples.append(point)

    def flush(self) -> None:
        with self.lock:
            events, samples = self.events[:150], self.samples[:40]
            self.events, self.samples = self.events[150:], self.samples[40:]
        if not events and not samples:
            return
        try:
            reply = self.api.post(f"/agent/v1/jobs/{self.job['job_id']}/events", {"events": events, "samples": samples},
                                  bearer=self.job["job_token"], timeout=20)
            if reply.get("cancel"):
                self.cancelled.set()
        except ApiError as e:
            if e.status == 401:        # token expired / job finished server-side
                self.cancelled.set()
            else:
                with self.lock:         # keep for retry
                    self.events = events + self.events
                    self.samples = samples + self.samples


PHASE_TO_STAGE = {"baseline": "baseline", "injecting": "fault_injection", "waiting_impact": "impact",
                  "waiting_recovery": "recovery", "probing_app": "verification", "telemetry": "verification",
                  "oracle": "oracle", "completed": "completed"}


def _engine(verifier_dir: Optional[str]):
    cand = [verifier_dir, os.environ.get("CHAOSLAB_VERIFIER_DIR"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "verifier"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "verifier")]
    for c in cand:
        if c and os.path.isfile(os.path.join(c, "engine.py")):
            path = os.path.abspath(c)
            if path not in sys.path:
                sys.path.insert(0, path)
            import engine  # flat import, as in the ChaosLab repo
            return engine
    raise RuntimeError("ChaosLab engine not found (expected ../verifier/engine.py or CHAOSLAB_VERIFIER_DIR).")


def engine_executor(job: Dict[str, Any], cfg: Dict[str, Any], reporter: Reporter, verifier_dir: Optional[str] = None) -> Dict[str, Any]:
    """Run the real ChaosLab engine against the local cluster. Returns {report}|{error}|{cancelled}."""
    import yaml
    eng = _engine(verifier_dir)
    probes = cfg.get("probes")
    cfg = {k: v for k, v in cfg.items() if k != "probes"}          # the engine never sees probe settings
    ns_, svc_ = cfg["target"]["namespace"], cfg["target"]["label"]["app"]
    pre_probe: Dict[str, Any] = {}
    if probes:
        # Pre-flight: a broken probe config or an unhealthy app must be reported BEFORE any fault is injected.
        reporter.event("baseline", "Pre-flight: probing the application before injecting any fault...", 5)
        reporter.flush()
        pre_probe = safe_probes.run_probes(probes, ns_, svc_, kubectl, wait_seconds=10)
        func_ok = pre_probe.get("functional_test_passed") or pre_probe.get("functional_test_skipped")
        if not (pre_probe["health_check_passed"] and func_ok):
            h = pre_probe.get("health_details", {}).get("detail") or pre_probe.get("health_status")
            f = (pre_probe.get("functional_details") or {}).get("detail") or ""
            return {"error": "Pre-flight probe failed, so NO fault was injected. "
                             f"Health: {h}. " + (f"Functional: {f}. " if f and not pre_probe.get("functional_test_passed") else "")
                             + "Fix the probe settings or the application and try again."}
        reporter.event("baseline", f"Pre-flight probes passed (health {pre_probe['health_details'].get('latency_ms')} ms).", 8)
    work = tempfile.mkdtemp(prefix="chaoslab-agent-")
    cfg_path = os.path.join(work, "experiment.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)
    eng.REPORTS_DIR = os.path.join(work, "reports")
    ns = cfg["target"]["namespace"]
    selector = f"app={cfg['target']['label']['app']}"
    started = time.time()
    stop = threading.Event()
    state: Dict[str, Any] = {"report": None, "failure": None}

    def sampler():
        while not stop.is_set():
            try:
                pods = eng.get_pods(ns, selector)
                ready = [p for p in pods if p["status"] == "Running" and p["total_count"] > 0 and p["ready_count"] == p["total_count"]]
                reporter.sample({"t": round(time.time() - started, 1), "replicas_ready": len(ready), "replicas_total": len(pods)})
            except Exception:
                pass
            stop.wait(2.0)

    def on_progress(phase, percent, message, data=None):
        if phase == "failed":
            state["failure"] = message
            reporter.event("failed", message, None, "error")
            return
        if data:
            state["report"] = data
        reporter.event(PHASE_TO_STAGE.get(phase, "baseline"), message, percent)

    runner = threading.Thread(target=lambda: _safe_run(eng, cfg_path, on_progress, state), daemon=True)
    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()
    runner.start()
    try:
        outcome = _supervise(eng, cfg, job, reporter, runner, sampler_thread, stop, state, started)
        if probes and outcome.get("report") and not reporter.cancelled.is_set():
            reporter.event("verification", "Probing the application after the experiment (waiting for it to recover)...", 85)
            reporter.flush()
            post = safe_probes.run_probes(probes, ns_, svc_, kubectl, wait_seconds=25)
            post["baseline"] = {"health_passed": pre_probe.get("health_check_passed"),
                                "latency_ms": (pre_probe.get("health_details") or {}).get("latency_ms")}
            outcome["report"]["application"] = post
            reporter.event("verification", f"Post-experiment probes: health {post['health_status']}"
                           f"{', functional ' + ('OK' if post['functional_test_passed'] else 'FAILED') if 'functional' in probes else ''}.", 92)
        return outcome
    finally:
        # the engine writes reports into `work`; they are not needed (evidence was sent to the server)
        threading.Timer(15.0, shutil.rmtree, args=(work,), kwargs={"ignore_errors": True}).start()


def _supervise(eng, cfg, job, reporter, runner, sampler_thread, stop, state, started) -> Dict[str, Any]:
    deadline = started + job["max_runtime_seconds"]
    while runner.is_alive():
        reporter.flush()
        if reporter.cancelled.is_set():
            abort_chaos(eng, cfg)
            stop.set()
            return {"cancelled": True}
        if time.time() > deadline:
            abort_chaos(eng, cfg)
            stop.set()
            return {"error": "The experiment exceeded its maximum runtime and was aborted."}
        runner.join(timeout=1.0)
    stop.set()
    sampler_thread.join(timeout=5)
    reporter.flush()
    if state.get("error"):
        return {"error": f"engine error: {state['error']}"}
    if state["report"]:
        return {"report": state["report"]}
    return {"error": state["failure"] or "The engine finished without producing a result."}


def _safe_run(eng, cfg_path, on_progress, state) -> None:
    try:
        eng.verify_experiment(cfg_path, progress_callback=on_progress)
    except Exception as e:
        state["error"] = str(e)[:300]


def abort_chaos(eng, cfg: Dict[str, Any]) -> None:
    """Remove the fault immediately (emergency stop / timeout / Ctrl+C)."""
    try:
        name, crd_type, _ = eng.generate_chaos_manifest(cfg)
        kubectl(["delete", crd_type, name, "-n", cfg["target"]["namespace"], "--ignore-not-found=true", "--wait=false"])
        log(f"Aborted: deleted {crd_type}/{name}.")
    except Exception as e:
        log(f"Could not delete chaos resource: {e}")


# ============================================================================ agent main loop
class Agent:
    def __init__(self, api: Api, policy: Policy, state_file: str, executor: Callable = engine_executor,
                 cluster_probe: Callable = cluster_identity, inventory_fn: Callable = collect_inventory,
                 poll_wait: float = 20.0):
        self.api, self.policy, self.state_file = api, policy, state_file
        self.executor, self.cluster_probe, self.inventory_fn = executor, cluster_probe, inventory_fn
        self.poll_wait = poll_wait
        self.paused_backoff = 3.0
        self.cred: Optional[Dict[str, Any]] = None
        self.cluster_uid: Optional[str] = None
        self.kube_version = ""
        self.job_thread: Optional[threading.Thread] = None
        self.current_reporter: Optional[Reporter] = None
        self.last_inventory = 0.0
        self.stop = threading.Event()

    # -- credential persistence
    def load_state(self) -> None:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("server") == self.api.base:       # never replay a credential to a different server
                self.cred = data
        except (OSError, ValueError):
            self.cred = None

    def save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        fd = os.open(self.state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.cred, f)
        try:
            os.chmod(self.state_file, 0o600)
        except OSError:
            pass

    def identity(self) -> None:
        if not self.cluster_uid:
            self.cluster_uid, self.kube_version = self.cluster_probe()
        if not self.cluster_uid:
            raise RuntimeError("Cannot read the cluster identity (kubectl get namespace kube-system). Is kubectl configured?")

    def enroll(self, token: str) -> None:
        self.identity()
        reply = self.api.post("/agent/v1/enroll", {
            "token": token, "agent_version": VERSION, "cluster_uid": self.cluster_uid, "kube_version": self.kube_version,
            "allowed_namespaces": self.policy.namespaces, "hostname": socket.gethostname()[:60]})
        self.cred = {"server": self.api.base, "credential": reply["credential"], "cluster_id": reply["cluster_id"]}
        self.save_state()
        log(f"Enrolled as cluster '{reply.get('name')}'. Allowed namespaces: {', '.join(self.policy.namespaces)}")

    # -- one iteration of the poll loop; returns False when the agent must stop
    def step(self) -> bool:
        self.identity()
        busy = bool(self.job_thread and self.job_thread.is_alive())
        body: Dict[str, Any] = {"agent_version": VERSION, "cluster_uid": self.cluster_uid,
                                "allowed_namespaces": self.policy.namespaces, "busy": busy,
                                "wait": 0 if busy else self.poll_wait}
        if time.time() - self.last_inventory > 30:
            body["inventory"] = self.inventory_fn(self.policy.namespaces)
            self.last_inventory = time.time()
        reply = self.api.post("/agent/v1/poll", body, bearer=self.cred["credential"], timeout=self.poll_wait + 20)
        if self.current_reporter and self.job_thread and self.job_thread.is_alive():
            if self.current_reporter.job["job_id"] in reply.get("cancel", []):
                log("Emergency stop received: aborting the running experiment.")
                self.current_reporter.cancelled.set()
        paused = bool(reply.get("paused"))
        if paused and not getattr(self, "_was_paused", False):
            log("Cluster is paused (emergency stop active). Not accepting jobs until it is resumed.")
        elif not paused and getattr(self, "_was_paused", False):
            log("Cluster resumed. Accepting jobs again.")
        self._was_paused = paused
        if paused:
            self.stop.wait(self.paused_backoff)            # never spin against the server while paused
        job = reply.get("job")
        if job and not busy:
            self.start_job(job)
        return True

    def start_job(self, job: Dict[str, Any]) -> None:
        cfg, reason = validate_job(job, self.policy)
        reporter = Reporter(self.api, job)
        if reason:
            log(f"REJECTED job {job.get('job_id')}: {reason}")
            try:
                self.api.post(f"/agent/v1/jobs/{job['job_id']}/result", {"error": f"Rejected by the agent's local policy: {reason}"},
                              bearer=job["job_token"])
            except ApiError as e:
                log(f"could not report rejection: {e}")
            return
        log(f"Job {job['job_id']}: {cfg['fault']['type']} on {cfg['target']['namespace']}/{cfg['target']['label']['app']}")
        self.current_reporter = reporter
        self.job_thread = threading.Thread(target=self._run_job, args=(job, cfg, reporter), daemon=True)
        self.job_thread.start()

    def _run_job(self, job, cfg, reporter: Reporter) -> None:
        try:
            outcome = self.executor(job, cfg, reporter)
        except Exception as e:
            outcome = {"error": f"agent error: {type(e).__name__}: {str(e)[:200]}"}
        reporter.flush()
        try:
            self.api.post(f"/agent/v1/jobs/{job['job_id']}/result", outcome, bearer=job["job_token"], timeout=60)
            log(f"Job {job['job_id']} finished: {'cancelled' if outcome.get('cancelled') else 'error' if outcome.get('error') else 'ok'}")
        except ApiError as e:
            log(f"could not deliver result for job {job['job_id']}: {e}")

    def run_forever(self) -> int:
        backoff = 2
        while not self.stop.is_set():
            try:
                self.step()
                backoff = 2
                if self.job_thread and self.job_thread.is_alive():
                    time.sleep(2)
            except ApiError as e:
                if e.status in (401, 403):
                    log(f"Server rejected this agent ({e}). The credential was revoked or the cluster changed. Re-enroll with a new token.")
                    return 2
                log(f"{e}; retrying in {backoff}s")
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                log(f"unexpected error: {type(e).__name__}: {e}; retrying in {backoff}s")
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="ChaosLab agent")
    p.add_argument("--server", default=os.environ.get("CHAOSLAB_SERVER"), help="ChaosLab server URL (https://...)")
    p.add_argument("--token", default=os.environ.get("CHAOSLAB_ENROLL_TOKEN"), help="one-time enrollment token (first start only)")
    p.add_argument("--allowed-namespaces", default=os.environ.get("CHAOSLAB_ALLOWED_NAMESPACES"),
                   help="REQUIRED comma-separated namespaces this agent may test")
    p.add_argument("--allowed-faults", default=os.environ.get("CHAOSLAB_ALLOWED_FAULTS", ",".join(ENGINE_FAULTS)))
    p.add_argument("--max-duration", type=int, default=int(os.environ.get("CHAOSLAB_MAX_DURATION", "120")))
    p.add_argument("--state-dir", default=os.environ.get("CHAOSLAB_STATE_DIR", os.path.join(os.path.expanduser("~"), ".chaoslab-agent")))
    p.add_argument("--no-probes", action="store_true", default=os.environ.get("CHAOSLAB_ALLOW_PROBES", "1") in ("0", "false", "no"),
                   help="never run application probes (jobs asking for them just get no application evidence)")
    p.add_argument("--insecure-allow-http", action="store_true", help="allow plain HTTP to a non-local server (testing only)")
    p.add_argument("--verifier-dir", default=None, help="path to chaoslab/verifier (default: auto-detected)")
    args = p.parse_args(argv)

    if not args.server or not args.allowed_namespaces:
        p.error("--server and --allowed-namespaces are required")
    try:
        policy = Policy([n.strip() for n in args.allowed_namespaces.split(",") if n.strip()],
                        tuple(f.strip() for f in args.allowed_faults.split(",") if f.strip()), args.max_duration,
                        allow_probes=not args.no_probes)
        api = Api(args.server, args.insecure_allow_http)
    except ValueError as e:
        p.error(str(e))

    state_file = os.path.join(args.state_dir, "agent.json")
    agent = Agent(api, policy, state_file,
                  executor=lambda job, cfg, rep: engine_executor(job, cfg, rep, args.verifier_dir))
    agent.load_state()
    if not agent.cred:
        if not args.token:
            p.error("not enrolled yet: provide --token (create one under Clusters in the ChaosLab web app)")
        try:
            agent.enroll(args.token)
        except (ApiError, RuntimeError) as e:
            log(f"Enrollment failed: {e}")
            return 1
    else:
        log(f"Using stored credential for cluster {agent.cred['cluster_id']} (state: {state_file}).")

    def shutdown(*_):
        log("Shutting down...")
        if agent.current_reporter and agent.job_thread and agent.job_thread.is_alive():
            agent.current_reporter.cancelled.set()
            agent.job_thread.join(timeout=15)
        agent.stop.set()
    signal.signal(signal.SIGINT, shutdown)
    try:
        signal.signal(signal.SIGTERM, shutdown)
    except (AttributeError, ValueError):
        pass
    log(f"ChaosLab agent {VERSION} running. Server: {api.base}. Namespaces: {', '.join(policy.namespaces)}. "
        f"Faults: {', '.join(policy.faults)}. Max duration: {policy.max_duration}s. "
        f"Probes: {'safe HTTP GET/log probes' if policy.allow_probes else 'disabled'}. Outbound-only.")
    return agent.run_forever()


if __name__ == "__main__":
    sys.exit(main())
