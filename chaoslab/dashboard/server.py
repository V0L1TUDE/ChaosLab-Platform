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

# Ensure verifier and chaoslab directories are in sys.path
for path in [BASE_DIR, CHAOSLAB_DIR, VERIFIER_DIR]:
    if path not in sys.path:
        sys.path.insert(0, path)

from runner import discover_experiments, find_experiment_path
from payment_verifier import verify_experiment
from gate import QualityGate

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

def get_cluster_status(namespace: str = "all") -> Dict[str, Any]:
    """Retrieve structured Kubernetes pod status across applications with short 2s caching."""
    global _CLUSTER_CACHE
    now = time.time()
    cache_key = f"status_{namespace}"
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

            if namespace == "all" and pod_ns not in TARGET_NAMESPACES:
                continue

            status = item.get("status", {})
            spec = item.get("spec", {})
            labels = meta.get("labels", {})
            app_name = labels.get("app") or meta.get("name", "").split("-")[0]

            container_statuses = status.get("containerStatuses", [])
            total_containers = len(container_statuses)
            ready_containers = sum(1 for c in container_statuses if c.get("ready", False))
            ready_str = f"{ready_containers}/{total_containers}" if total_containers > 0 else "0/0"
            is_ready = (total_containers > 0) and (ready_containers == total_containers) and (status.get("phase") == "Running")

            restarts = 0
            if container_statuses:
                restarts = container_statuses[0].get("restartCount", 0)

            app_group = "Online Boutique"
            if pod_ns == "project-platform":
                app_group = "Project Platform"
            elif pod_ns == "learning-platform":
                app_group = "LMS Platform"

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

def get_historical_reports() -> List[Dict[str, Any]]:
    """List historical experiment reports with structured metadata."""
    if not os.path.exists(REPORTS_DIR):
        return []

    reports = []
    for filepath in glob.glob(os.path.join(REPORTS_DIR, "*_report.json")):
        basename = os.path.basename(filepath)
        clean_prefix = basename.replace("_report.json", "")
        try:
            mtime = os.path.getmtime(filepath)
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            reports.append({
                "filename": basename,
                "prefix": clean_prefix,
                "experiment": data.get("experiment", clean_prefix),
                "target": data.get("target", "unknown"),
                "fault": data.get("fault", "pod-kill"),
                "verdict": data.get("verdict", "UNKNOWN"),
                "recovery_time": data.get("recovery_time_seconds"),
                "p95_ms": data.get("telemetry", {}).get("latency_ms", {}).get("p95"),
                "timestamp": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "has_html": os.path.exists(os.path.join(REPORTS_DIR, f"{clean_prefix}_report.html")),
                "has_md": os.path.exists(os.path.join(REPORTS_DIR, f"{clean_prefix}_report.md")),
                "has_json": True
            })
        except Exception:
            pass

    reports.sort(key=lambda r: r["timestamp"], reverse=True)
    return reports

class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler providing REST API endpoints and static assets."""

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

        # API: Available Application Namespaces
        if path == "/api/namespaces":
            self._set_headers(200)
            self.wfile.write(json.dumps({
                "namespaces": [
                    {"id": "all", "name": "All Platforms (Universal Suite)"},
                    {"id": "default", "name": "Google Online Boutique (default)"},
                    {"id": "project-platform", "name": "Project Platform (project-platform)"},
                    {"id": "learning-platform", "name": "LMS Platform (learning-platform)"}
                ]
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
            filename = os.path.basename(urllib.parse.unquote(raw_filename))
            target_file = os.path.join(REPORTS_DIR, filename)

            if os.path.exists(target_file) and os.path.isfile(target_file):
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

        if path == "/api/run":
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

def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Start multi-threaded HTTP server for ChaosLab Web Dashboard."""
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
