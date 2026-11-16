"""
ChaosLab Web Dashboard & Real-Time Control Center Server
========================================================
Lightweight multi-threaded REST API and web dashboard server.
Enables real-time cluster monitoring, experiment triggering, live verification tracking,
Oracle scorecard evaluation, and OpenTelemetry trace inspection directly from the browser.
"""

from datetime import datetime, timezone
import glob
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.parse

# Setup paths
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAOSLAB_DIR = os.path.join(BASE_DIR, "chaoslab")
VERIFIER_DIR = os.path.join(CHAOSLAB_DIR, "verifier")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
REPORTS_DIR = os.path.join(CHAOSLAB_DIR, "reports")
EXPERIMENTS_DIR = os.path.join(CHAOSLAB_DIR, "experiments")
ONBOARDING_HTML = os.path.join(CHAOSLAB_DIR, "onboarding", "static", "index.html")

# Ensure verifier and chaoslab directories are in sys.path
for path in [BASE_DIR, CHAOSLAB_DIR, VERIFIER_DIR]:
    if path not in sys.path:
        sys.path.insert(0, path)

from runner import discover_experiments, find_experiment_path
from payment_verifier import verify_experiment
from gate import QualityGate
from onboarding.registry import ApplicationRegistry, ApplicationError

class ExecutionManager:
    """Thread-safe state manager for asynchronous experiment execution and progress tracking."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.experiment = None
        self.phase = "idle"
        self.percent = 0
        self.message = "System ready. Select an experiment to begin."
        self.logs = []
        self.verdict = None
        self.report = None
        self.error = None
        self.started_at = None
        self.completed_at = None

    def start(self, exp_name: str, exp_path: str):
        with self.lock:
            if self.running:
                return False, "An experiment is already in progress."
            self.running = True
            self.experiment = exp_name
            self.phase = "starting"
            self.percent = 5
            self.message = f"Starting experiment '{exp_name}'..."
            self.logs = [f"[{datetime.now().strftime('%H:%M:%S')}] Launching experiment '{exp_name}'..."]
            self.verdict = None
            self.report = None
            self.error = None
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.completed_at = None

        worker = threading.Thread(target=self._run_worker, args=(exp_path,), daemon=True)
        worker.start()
        return True, "Experiment launched successfully."

    def _run_worker(self, exp_path: str):
        try:
            def on_progress(phase, percent, msg, data=None):
                with self.lock:
                    self.phase = phase
                    self.percent = percent
                    self.message = msg
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    self.logs.append(f"[{timestamp}] {msg}")
                    if len(self.logs) > 300:
                        self.logs = self.logs[-300:]
                    if data:
                        self.report = data
                        self.verdict = data.get("verdict")

            passed = verify_experiment(exp_path, progress_callback=on_progress)
            with self.lock:
                self.running = False
                self.completed_at = datetime.now(timezone.utc).isoformat()
                if self.phase not in ["completed", "failed"]:
                    self.phase = "completed"
                    self.percent = 100
                    self.verdict = "PASS" if passed else "FAIL"
        except Exception as e:
            with self.lock:
                self.running = False
                self.phase = "failed"
                self.percent = 100
                self.error = str(e)
                self.verdict = "FAIL"
                self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {e}")
                self.completed_at = datetime.now(timezone.utc).isoformat()

    def get_state(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "experiment": self.experiment,
                "phase": self.phase,
                "percent": self.percent,
                "message": self.message,
                "logs": self.logs[-50:],
                "verdict": self.verdict,
                "report": self.report,
                "error": self.error,
                "started_at": self.started_at,
                "completed_at": self.completed_at
            }

_EXECUTION_MANAGER = ExecutionManager()
_CLUSTER_CACHE = {"timestamp": 0.0, "data": None}

TARGET_NAMESPACES = ["default", "project-platform", "learning-platform"]

LEGACY_GROUPS = {"default": "Online Boutique", "project-platform": "Project Platform", "learning-platform": "LMS Platform"}


def get_application_catalog():
    """Namespaces and display names from existing suites and local onboarding."""
    namespaces = set(TARGET_NAMESPACES)
    groups = {}
    for experiment in discover_experiments():
        ns = experiment.get("namespace")
        if isinstance(ns, str) and re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", ns):
            namespaces.add(ns)
            groups[(ns, experiment.get("target"))] = experiment.get("app_group") or ns
    for app in ApplicationRegistry().list_apps():
        namespaces.add(app["namespace"])
        groups[(app["namespace"], app["app_label"])] = app["name"]
    return namespaces, groups


def get_available_namespaces():
    namespaces, _ = get_application_catalog()
    return [{"id": "all", "name": "All Applications"}] + [
        {"id": ns, "name": f"{LEGACY_GROUPS.get(ns, ns)} ({ns})" if ns in LEGACY_GROUPS else ns}
        for ns in sorted(namespaces)]


def get_cluster_status(namespace: str = "all") -> Dict[str, Any]:
    """Retrieve structured Kubernetes pod status across applications with short 2s caching."""
    global _CLUSTER_CACHE
    now = time.time()
    namespaces, application_groups = get_application_catalog()
    cache_key = f"status_{namespace}_{','.join(sorted(namespaces))}"
    if _CLUSTER_CACHE.get(cache_key) and (now - _CLUSTER_CACHE[cache_key]["timestamp"] < 2.0):
        return _CLUSTER_CACHE[cache_key]["data"]

    try:
        if namespace == "all":
            proc = subprocess.run(
                ["kubectl", "get", "pods", "-A", "-o", "json"],
                capture_output=True,
                text=True,
                timeout=6
            )
        else:
            proc = subprocess.run(
                ["kubectl", "get", "pods", "-n", namespace, "-o", "json"],
                capture_output=True,
                text=True,
                timeout=5
            )

        if proc.returncode != 0:
            return {"connected": False, "error": proc.stderr.strip(), "pods": []}

        items = json.loads(proc.stdout).get("items", [])
        pods = []
        for item in items:
            meta = item.get("metadata", {})
            pod_ns = meta.get("namespace", "default")

            if namespace == "all" and pod_ns not in namespaces:
                continue

            status = item.get("status", {})
            spec = item.get("spec", {})
            labels = meta.get("labels", {})
            app_name = labels.get("app") or meta.get("name", "").split("-")[0]

            container_statuses = status.get("containerStatuses", [])
            total_containers = len(container_statuses)
            ready_containers = sum(1 for c in container_statuses if c.get("ready", False))
            ready_str = f"{ready_containers}/{total_containers}" if total_containers > 0 else "0/0"
            is_ready = ((total_containers > 0) and (ready_containers == total_containers)
                        and status.get("phase") == "Running" and not meta.get("deletionTimestamp")
                        and any(c.get("type") == "Ready" and c.get("status") == "True"
                                for c in status.get("conditions", [])))

            restarts = sum(c.get("restartCount", 0) for c in container_statuses)
            app_group = application_groups.get((pod_ns, app_name), LEGACY_GROUPS.get(pod_ns, pod_ns))

            pods.append({
                "name": meta.get("name"),
                "app": app_name,
                "namespace": pod_ns,
                "app_group": app_group,
                "ready": ready_str,
                "is_ready": is_ready,
                "status": status.get("phase", "Unknown"),
                "restarts": restarts,
                "ip": status.get("podIP", ""),
                "node": spec.get("nodeName", ""),
                "created_at": meta.get("creationTimestamp", "")
            })

        pods.sort(key=lambda p: (p["app_group"], p["app"]))
        total = len(pods)
        ready_count = sum(1 for p in pods if p["is_ready"])
        all_healthy = (total > 0) and (ready_count == total)

        result = {
            "connected": True,
            "all_healthy": all_healthy,
            "total_pods": total,
            "ready_pods": ready_count,
            "namespace": namespace,
            "pods": pods,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        _CLUSTER_CACHE[cache_key] = {"timestamp": now, "data": result}
        return result
    except Exception as e:
        return {"connected": False, "error": str(e), "pods": []}

def resolve_report_file(filename: str) -> Path | None:
    """Serve only report formats inside the configured report directory."""
    if not re.fullmatch(r"(?:runs/[0-9a-f]{32}/)?[A-Za-z0-9_.-]+_report\.(?:html|md|json)", filename):
        return None
    root = Path(REPORTS_DIR).resolve()
    target = (root / filename).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def get_historical_reports() -> List[Dict[str, Any]]:
    """List immutable run archives and legacy exports, without latest-copy duplicates."""
    root = Path(REPORTS_DIR)
    reports = []
    archived_ids = set()
    files = sorted(root.glob("runs/*/*_report.json")) + sorted(root.glob("*_report.json"))
    for filepath in files:
        relative = filepath.relative_to(root).as_posix()
        if resolve_report_file(relative) is None:
            continue
        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            run_id = data.get("run_id")
            archived = relative.startswith("runs/")
            if not archived and run_id in archived_ids:
                continue
            prefix = relative.removesuffix("_report.json")
            raw_time = data.get("timestamps", {}).get("experiment_start")
            timestamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00")) if raw_time else datetime.fromtimestamp(filepath.stat().st_mtime, timezone.utc)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            evaluation = data.get("oracle_evaluation") or {}
            reports.append({
                "filename": relative, "prefix": prefix, "run_id": run_id,
                "experiment": data.get("experiment", filepath.stem),
                "target": data.get("target", "unknown"), "fault": data.get("fault", "unknown"),
                "namespace": data.get("namespace", "unknown"), "verdict": data.get("verdict", "UNKNOWN"),
                "recovery_time": data.get("recovery_time_seconds"),
                "p95_ms": data.get("telemetry", {}).get("latency_ms", {}).get("p95"),
                "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
                "passed_checks": evaluation.get("passed_rules"),
                "failed_checks": evaluation.get("failed_rules"),
                "skipped_checks": evaluation.get("skipped_rules"),
                "has_html": resolve_report_file(prefix + "_report.html") is not None,
                "has_md": resolve_report_file(prefix + "_report.md") is not None, "has_json": True,
            })
            if archived and run_id:
                archived_ids.add(run_id)
        except (ValueError, TypeError, AttributeError, OSError):
            continue
    reports.sort(key=lambda row: row["timestamp"], reverse=True)
    return reports

class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler providing REST API endpoints and static assets."""

    def _write_json(self, status: int, payload: Dict[str, Any]):
        self._set_headers(status)
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _require_local_origin(self) -> bool:
        """Basic browser cross-site protection, not a substitute for auth."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        expected = f"http://{self.headers.get('Host', '')}"
        return origin == expected and self.headers.get("Host", "").split(":")[0] in {"localhost", "127.0.0.1"}

    def _read_registration_json(self) -> Dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise ApplicationError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ApplicationError("Invalid content length") from error
        if not 2 <= length <= 4096:
            raise ApplicationError("JSON body must be between 2 and 4096 bytes")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise ApplicationError("Invalid JSON") from error
        if not isinstance(payload, dict):
            raise ApplicationError("JSON object expected")
        return payload

    def _set_headers(self, status: int = 200, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers(204)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Root: Single Page Application
        if path in ["/", "/index.html"]:
            html_file = os.path.join(STATIC_DIR, "index.html")
            if os.path.exists(html_file):
                self._set_headers(200, "text/html; charset=utf-8")
                with open(html_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self._set_headers(404, "text/plain")
                self.wfile.write(b"Dashboard index.html not found.")
            return

        # Local-only application onboarding page (separate from existing dashboard).
        if path == "/onboarding":
            if not os.path.isfile(ONBOARDING_HTML):
                return self._write_json(404, {"error": "Onboarding page missing"})
            self._set_headers(200, "text/html; charset=utf-8")
            with open(ONBOARDING_HTML, "rb") as source:
                self.wfile.write(source.read())
            return

        if path == "/api/onboarding/apps":
            return self._write_json(200, {"applications": ApplicationRegistry().list_apps()})

        status_match = re.fullmatch(r"/api/onboarding/apps/([0-9a-f]{12})/pods", path)
        if status_match:
            try:
                result = ApplicationRegistry().discover_pods(status_match.group(1))
                return self._write_json(200, result)
            except ApplicationError as error:
                return self._write_json(400, {"error": str(error)})

        # API: Available Application Namespaces
        if path == "/api/namespaces":
            self._set_headers(200)
            self.wfile.write(json.dumps({
                "namespaces": get_available_namespaces()
            }).encode("utf-8"))
            return

        # API: Cluster Pod Status
        if path == "/api/status":
            query = urllib.parse.parse_qs(parsed.query)
            ns = query.get("namespace", ["all"])[0]
            data = get_cluster_status(namespace=ns)
            self._set_headers(200)
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        # API: Discovered Experiments
        if path == "/api/experiments":
            exps = discover_experiments()
            self._set_headers(200)
            self.wfile.write(json.dumps({"experiments": exps}).encode("utf-8"))
            return

        # API: Live Execution State
        if path == "/api/execution":
            state = _EXECUTION_MANAGER.get_state()
            self._set_headers(200)
            self.wfile.write(json.dumps(state).encode("utf-8"))
            return

        # API: Historical Reports List
        if path == "/api/reports":
            reports = get_historical_reports()
            self._set_headers(200)
            self.wfile.write(json.dumps({"reports": reports}).encode("utf-8"))
            return

        # API: Quality Gate Check
        if path == "/api/gate":
            gate = QualityGate()
            passed, policies, summary = gate.evaluate()
            payload = {
                "gate_passed": passed,
                "verdict": "APPROVED" if passed else "REJECTED",
                "policies": [p.to_dict() for p in policies],
                "summary": summary
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        # API: Serve Individual Report Files (/api/reports/<filename>)
        if path.startswith("/api/reports/"):
            raw_filename = path.replace("/api/reports/", "")
            filename = urllib.parse.unquote(raw_filename)
            target_file = resolve_report_file(filename)

            if target_file is not None:
                mime, _ = mimetypes.guess_type(target_file)
                if filename.endswith(".md"):
                    mime = "text/markdown; charset=utf-8"
                elif filename.endswith(".html"):
                    mime = "text/html; charset=utf-8"
                elif filename.endswith(".json"):
                    mime = "application/json"

                self._set_headers(200, mime or "text/plain")
                with open(target_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self._set_headers(404, "application/json")
                self.wfile.write(json.dumps({"error": f"Report '{filename}' not found."}).encode("utf-8"))
            return

        # Static asset serving (/static/...)
        if path.startswith("/static/"):
            clean_rel = path.replace("/static/", "").lstrip("/\\")
            static_file = os.path.join(STATIC_DIR, os.path.basename(clean_rel))
            if os.path.exists(static_file) and os.path.isfile(static_file):
                mime, _ = mimetypes.guess_type(static_file)
                self._set_headers(200, mime or "application/octet-stream")
                with open(static_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self._set_headers(404, "text/plain")
                self.wfile.write(b"Static asset not found.")
            return

        # Default 404
        self._set_headers(404, "application/json")
        self.wfile.write(json.dumps({"error": "Endpoint not found."}).encode("utf-8"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Local-only onboarding write endpoints. Nothing is deployed or executed here.
        if path == "/api/onboarding/apps" or re.fullmatch(r"/api/onboarding/apps/[0-9a-f]{12}/experiment", path):
            if not self._require_local_origin():
                return self._write_json(403, {"error": "Cross-origin write requests are not permitted"})
            try:
                payload = self._read_registration_json()
                registry = ApplicationRegistry()
                if path == "/api/onboarding/apps":
                    return self._write_json(201, {"application": registry.register(payload)})
                app_id = path.split("/")[4]
                if payload.get("fault_type", "pod-kill") != "pod-kill":
                    raise ApplicationError("Only pod-kill drafts are supported in this MVP")
                return self._write_json(201, {"experiment": registry.generate_experiment(app_id, health_probe=payload.get("health_probe"), functional_check=payload.get("functional_check"))})
            except ApplicationError as error:
                return self._write_json(400, {"error": str(error)})

        if path == "/api/run":
            # Explicit opt-in because the old endpoint lacks authorization.
            if os.environ.get("CHAOSLAB_ENABLE_FAULT_RUNS") != "1":
                return self._write_json(403, {"error": "Fault execution disabled by default. Use an isolated cluster and set CHAOSLAB_ENABLE_FAULT_RUNS=1 only after verifying the target."})
            if not self._require_local_origin():
                return self._write_json(403, {"error": "Cross-origin launch rejected"})
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"

            try:
                body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception:
                body = {}

            exp_name = body.get("name") or body.get("experiment")
            exp_path = None

            if exp_name:
                exp_path = find_experiment_path(exp_name)
            else:
                # Default to payment_kill
                default_file = os.path.join(EXPERIMENTS_DIR, "payment_kill.yaml")
                if os.path.exists(default_file):
                    exp_path = default_file
                    exp_name = "payment-service-pod-kill"

            if not exp_path or not os.path.exists(exp_path):
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": f"Experiment '{exp_name}' could not be resolved."}).encode("utf-8"))
                return

            started, message = _EXECUTION_MANAGER.start(exp_name, exp_path)
            if started:
                self._set_headers(200)
                self.wfile.write(json.dumps({
                    "success": True,
                    "message": message,
                    "experiment": exp_name
                }).encode("utf-8"))
            else:
                self._set_headers(409)
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": message
                }).encode("utf-8"))
            return

        self._set_headers(404)
        self.wfile.write(json.dumps({"error": "Endpoint not found."}).encode("utf-8"))

    def log_message(self, format, *args):
        """Suppress standard HTTP request output in CLI."""
        pass

def run_server(host: str = "127.0.0.1", port: int = 8080):
    """Start local-only development server. Not suitable for public hosting."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Local MVP must bind to loopback; public hosting requires authentication and CSRF protection")
    server_address = (host, port)
    httpd = HTTPServer(server_address, DashboardRequestHandler)
    print("====================================================================================================")
    print("CHAOSLAB INTERACTIVE WEB DASHBOARD & CONTROL CENTER")
    print("====================================================================================================")
    print(f"Server running at: http://localhost:{port}")
    print("Press Ctrl+C to stop the dashboard server.")
    print("====================================================================================================")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down ChaosLab dashboard server...")
        httpd.server_close()

if __name__ == "__main__":
    port_arg = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8080
    run_server(port=port_arg)
