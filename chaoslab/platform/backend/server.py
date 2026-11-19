"""
ChaosLab Hosted SaaS Platform API Server & Control Plane
========================================================
High-performance REST API and single-page web server.
Implements:
- Multi-tenant authentication (PBKDF2 + JWT).
- Application and Cluster management.
- Job queuing and outbound Agent communication.
- Verification Report ingestion and Oracle visualization.
- Complete audit logging and tenant isolation.
"""

from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import mimetypes
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PLATFORM_DIR = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
CHAOSLAB_DIR = os.path.abspath(os.path.join(PLATFORM_DIR, ".."))
BASE_DIR = os.path.abspath(os.path.join(CHAOSLAB_DIR, ".."))
STATIC_DIR = os.path.join(PLATFORM_DIR, "frontend", "static")
EXPERIMENTS_DIR = os.path.join(CHAOSLAB_DIR, "experiments")
REPORTS_DIR = os.path.join(CHAOSLAB_DIR, "reports")

for p in [CURRENT_DIR, PLATFORM_DIR, CHAOSLAB_DIR, BASE_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from database import Database
from auth import (
    hash_password, verify_password,
    create_jwt_token, verify_jwt_token,
    extract_bearer_token
)

class SaaSPlatformRequestHandler(BaseHTTPRequestHandler):
    """Multi-threaded HTTP request dispatcher with JWT middleware and routing."""

    db: Database = Database.get_instance()

    def _set_headers(self, status: int = 200, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

    def _send_json(self, data: Any, status: int = 200):
        self._set_headers(status, "application/json")
        self.wfile.write(json.dumps(data, default=str).encode("utf-8"))

    def _send_error(self, message: str, status: int = 400):
        self._send_json({"error": message, "status": status}, status=status)

    def _read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _authenticate(self) -> Optional[Dict[str, Any]]:
        """Verify JWT from Authorization header and return user dict or None."""
        auth_header = self.headers.get("Authorization")
        token = extract_bearer_token(auth_header)
        if not token:
            return None
        payload = verify_jwt_token(token)
        if not payload or "sub" not in payload:
            return None
        return self.db.get_user_by_id(payload["sub"])

    def _authenticate_agent(self) -> Optional[Dict[str, Any]]:
        """Verify Agent JWT and return cluster dict or None."""
        auth_header = self.headers.get("Authorization")
        token = extract_bearer_token(auth_header)
        if not token:
            return None
        payload = verify_jwt_token(token)
        if not payload or payload.get("role") != "agent" or "cluster_id" not in payload:
            return None
        return self.db.get_cluster_by_id_internal(payload["cluster_id"])

    def do_OPTIONS(self):
        self._set_headers(204)

    # -------------------------------------------------------------
    # GET Handlers
    # -------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Health
        if path == "/api/v1/health":
            self._send_json({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})
            return

        # Current User
        if path == "/api/v1/auth/me":
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized: Invalid or missing token.", 401)
                return
            self._send_json({"user": user})
            return

        # Applications List
        if path == "/api/v1/apps":
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            apps = self.db.list_applications(user["id"])
            self._send_json({"applications": apps})
            return

        # Application Detail: /api/v1/apps/{id}
        app_match = re.match(r"^/api/v1/apps/([a-zA-Z0-9-]+)$", path)
        if app_match:
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            app_id = app_match.group(1)
            app = self.db.get_application(user["id"], app_id)
            if not app:
                self._send_error("Application not found", 404)
                return
            self._send_json({"application": app})
            return

        # Clusters List
        if path == "/api/v1/clusters":
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            clusters = self.db.list_clusters(user["id"])
            self._send_json({"clusters": clusters})
            return

        # Cluster Detail: /api/v1/clusters/{id}
        cluster_match = re.match(r"^/api/v1/clusters/([a-zA-Z0-9-]+)$", path)
        if cluster_match:
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            cluster_id = cluster_match.group(1)
            cluster = self.db.get_cluster(user["id"], cluster_id)
            if not cluster:
                self._send_error("Cluster not found", 404)
                return
            self._send_json({"cluster": cluster})
            return

        # Available Experiment Templates
        if path == "/api/v1/experiments/templates":
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            from runner import discover_experiments
            templates = discover_experiments()
            self._send_json({"templates": templates})
            return

        # Experiment Runs List
        if path == "/api/v1/experiments":
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            runs = self.db.list_experiment_runs(user["id"])
            self._send_json({"runs": runs})
            return

        # Experiment Run Detail: /api/v1/experiments/{id}
        exp_match = re.match(r"^/api/v1/experiments/([a-zA-Z0-9-]+)$", path)
        if exp_match:
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            run_id = exp_match.group(1)
            run = self.db.get_experiment_run(user["id"], run_id)
            if not run:
                self._send_error("Experiment run not found", 404)
                return
            report = self.db.get_report_by_run_id(run_id)
            self._send_json({"run": run, "report": report})
            return

        # Experiment Report View / Download: /api/v1/experiments/{id}/report
        rep_match = re.match(r"^/api/v1/experiments/([a-zA-Z0-9-]+)/report$", path)
        if rep_match:
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            run_id = rep_match.group(1)
            run = self.db.get_experiment_run(user["id"], run_id)
            if not run:
                self._send_error("Unauthorized or experiment not found", 404)
                return
            report = self.db.get_report_by_run_id(run_id)
            if not report:
                self._send_error("Report not found", 404)
                return
            self._send_json({"report": report})
            return

        # Direct Report File Download: /api/v1/reports/{run_id}/download?format=json|html|md
        download_match = re.match(r"^/api/v1/reports/([a-zA-Z0-9-]+)/download$", path)
        if download_match:
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            run_id = download_match.group(1)
            run = self.db.get_experiment_run(user["id"], run_id)
            if not run:
                self._send_error("Unauthorized or experiment run not found", 404)
                return

            report = self.db.get_report_by_run_id(run_id)
            if not report:
                self._send_error("Report not found", 404)
                return

            fmt = query.get("format", ["json"])[0].lower()
            self.db.log_audit(user["id"], "REPORT_DOWNLOAD", "report", report["id"], {"format": fmt})

            if fmt == "html":
                html_path = os.path.join(REPORTS_DIR, f"{run['experiment_name'].replace('-', '_')}_report.html")
                if not os.path.exists(html_path):
                    html_path = os.path.join(REPORTS_DIR, "payment_service_pod_kill_report.html")
                if os.path.exists(html_path):
                    self._set_headers(200, "text/html; charset=utf-8")
                    with open(html_path, "rb") as f:
                        self.wfile.write(f.read())
                    return
                else:
                    self._send_error("HTML report artifact not found", 404)
                    return
            elif fmt == "md":
                md_path = os.path.join(REPORTS_DIR, f"{run['experiment_name'].replace('-', '_')}_report.md")
                if not os.path.exists(md_path):
                    md_path = os.path.join(REPORTS_DIR, "payment_service_pod_kill_report.md")
                if os.path.exists(md_path):
                    self._set_headers(200, "text/markdown; charset=utf-8")
                    with open(md_path, "rb") as f:
                        self.wfile.write(f.read())
                    return
                else:
                    self._send_error("Markdown report artifact not found", 404)
                    return
            else:
                self._send_json(report["evidence_json"] if report.get("evidence_json") else report)
                return

        # Audit Logs
        if path == "/api/v1/audit":
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            logs = self.db.list_audit_logs(user["id"])
            self._send_json({"audit_logs": logs})
            return

        # Agent: Poll for Pending Jobs
        if path == "/api/v1/agent/jobs/poll":
            cluster = self._authenticate_agent()
            if not cluster:
                self._send_error("Unauthorized: Invalid Agent credentials", 401)
                return
            job = self.db.poll_pending_job_for_cluster(cluster["id"])
            self._send_json({"job": job})
            return

        # Static Assets & SPA Root
        if path in ["/", "/index.html", "/login", "/register", "/dashboard", "/apps", "/clusters", "/experiments"]:
            index_path = os.path.join(STATIC_DIR, "index.html")
            if os.path.exists(index_path):
                self._set_headers(200, "text/html; charset=utf-8")
                with open(index_path, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self._send_error("Hosted frontend static index.html not found.", 404)
                return

        if path.startswith("/static/"):
            rel_path = path.replace("/static/", "").lstrip("/\\")
            asset_path = os.path.join(STATIC_DIR, rel_path)
            if os.path.exists(asset_path) and os.path.isfile(asset_path):
                mime, _ = mimetypes.guess_type(asset_path)
                self._set_headers(200, mime or "application/octet-stream")
                with open(asset_path, "rb") as f:
                    self.wfile.write(f.read())
                return

        self._send_error("Endpoint not found", 404)

    # -------------------------------------------------------------
    # POST Handlers
    # -------------------------------------------------------------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        # Registration
        if path == "/api/v1/auth/register":
            email = body.get("email", "").strip()
            password = body.get("password", "")
            full_name = body.get("full_name", "").strip()

            if not email or "@" not in email:
                self._send_error("Valid email is required.")
                return
            if len(password) < 6:
                self._send_error("Password must be at least 6 characters.")
                return
            if not full_name:
                self._send_error("Full name is required.")
                return

            if self.db.get_user_by_email(email):
                self._send_error("An account with this email already exists.", 409)
                return

            hashed = hash_password(password)
            user = self.db.create_user(email, hashed, full_name)
            token = create_jwt_token({"sub": user["id"], "email": user["email"], "role": user["role"]})
            self.db.log_audit(user["id"], "USER_REGISTER", "user", user["id"], {"email": email})
            self._send_json({"user": user, "token": token}, 201)
            return

        # Login
        if path == "/api/v1/auth/login":
            email = body.get("email", "").strip()
            password = body.get("password", "")

            user_record = self.db.get_user_by_email(email)
            if not user_record or not verify_password(password, user_record["hashed_password"]):
                self._send_error("Invalid email or password.", 401)
                return

            user = {"id": user_record["id"], "email": user_record["email"],
                    "full_name": user_record["full_name"], "role": user_record["role"]}
            token = create_jwt_token({"sub": user["id"], "email": user["email"], "role": user["role"]})
            self.db.log_audit(user["id"], "USER_LOGIN", "user", user["id"])
            self._send_json({"user": user, "token": token})
            return

        # Create Application
        if path == "/api/v1/apps":
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            if user.get("role") == "viewer":
                self._send_error("Forbidden: Viewer role cannot create applications.", 403)
                return
            name = body.get("name", "").strip()
            if not name:
                self._send_error("Application name is required.")
                return
            app = self.db.create_application(
                user_id=user["id"],
                name=name,
                description=body.get("description", ""),
                repo_url=body.get("repo_url", ""),
                target_namespace=body.get("target_namespace", "default"),
                health_endpoint=body.get("health_endpoint", "")
            )
            self.db.log_audit(user["id"], "APP_CREATE", "application", app["id"], {"name": name})
            self._send_json({"application": app}, 201)
            return

        # Create Cluster
        if path == "/api/v1/clusters":
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            if user.get("role") == "viewer":
                self._send_error("Forbidden: Viewer role cannot create clusters.", 403)
                return
            name = body.get("name", "").strip()
            if not name:
                self._send_error("Cluster name is required.")
                return
            cluster = self.db.create_cluster(
                user_id=user["id"],
                name=name,
                environment=body.get("environment", "development")
            )
            token = self.db.create_enrollment_token(cluster["id"])
            self.db.log_audit(user["id"], "CLUSTER_CREATE", "cluster", cluster["id"], {"name": name})
            self._send_json({"cluster": cluster, "enrollment_token": token}, 201)
            return

        # Generate Enrollment Token: /api/v1/clusters/{id}/enrollment-token
        tok_match = re.match(r"^/api/v1/clusters/([a-zA-Z0-9-]+)/enrollment-token$", path)
        if tok_match:
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            cluster_id = tok_match.group(1)
            cluster = self.db.get_cluster(user["id"], cluster_id)
            if not cluster:
                self._send_error("Cluster not found", 404)
                return
            token = self.db.create_enrollment_token(cluster_id)
            self.db.log_audit(user["id"], "TOKEN_GENERATE", "cluster", cluster_id)
            self._send_json({"enrollment_token": token})
            return

        # Launch Experiment
        if path == "/api/v1/experiments/launch":
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            if user.get("role") == "viewer":
                self._send_error("Forbidden: Viewer role cannot launch chaos experiments.", 403)
                return
            app_id = body.get("application_id")
            cluster_id = body.get("cluster_id")
            exp_name = body.get("experiment_name")

            if not app_id or not cluster_id or not exp_name:
                self._send_error("application_id, cluster_id, and experiment_name are required.")
                return

            app = self.db.get_application(user["id"], app_id)
            cluster = self.db.get_cluster(user["id"], cluster_id)
            if not app:
                self._send_error("Application not found", 404)
                return
            if not cluster:
                self._send_error("Cluster not found", 404)
                return

            run = self.db.create_experiment_run(user["id"], app_id, cluster_id, exp_name)
            self.db.log_audit(user["id"], "EXPERIMENT_LAUNCH", "experiment_run", run["id"], {
                "experiment_name": exp_name,
                "cluster_name": cluster["name"],
                "app_name": app["name"]
            })
            self._send_json({"run": run}, 201)
            return

        # Agent: Enroll Cluster Handshake
        if path == "/api/v1/agent/enroll":
            enrollment_token = body.get("enrollment_token")
            if not enrollment_token:
                self._send_error("enrollment_token is required.")
                return

            cluster_id = self.db.validate_and_consume_enrollment_token(enrollment_token)
            if not cluster_id:
                self._send_error("Invalid, expired, or already consumed enrollment token.", 403)
                return

            cluster = self.db.get_cluster_by_id_internal(cluster_id)
            # Create long-lived Agent JWT
            agent_jwt = create_jwt_token({
                "sub": f"agent-{cluster_id}",
                "cluster_id": cluster_id,
                "role": "agent"
            }, expires_in=86400 * 365)  # 1 year

            self.db.log_audit(cluster["user_id"], "AGENT_ENROLLED", "cluster", cluster_id)
            self._send_json({
                "message": "Cluster enrolled successfully.",
                "cluster_id": cluster_id,
                "agent_token": agent_jwt
            })
            return

        # Agent: Heartbeat
        if path == "/api/v1/agent/heartbeat":
            cluster = self._authenticate_agent()
            if not cluster:
                self._send_error("Unauthorized: Invalid Agent credentials", 401)
                return
            agent_version = body.get("agent_version", "1.0.0")
            cluster_info = body.get("cluster_info", {})
            self.db.update_cluster_heartbeat(cluster["id"], agent_version, cluster_info)
            self._send_json({"status": "acknowledged"})
            return

        # Agent: Update Job Status: /api/v1/agent/jobs/{id}/status
        status_match = re.match(r"^/api/v1/agent/jobs/([a-zA-Z0-9-]+)/status$", path)
        if status_match:
            cluster = self._authenticate_agent()
            if not cluster:
                self._send_error("Unauthorized", 401)
                return
            job_id = status_match.group(1)
            status = body.get("status", "running")
            error_msg = body.get("error_message")
            self.db.update_experiment_status(job_id, status=status, error_message=error_msg)
            self._send_json({"status": "updated"})
            return

        # Agent: Post Execution Results: /api/v1/agent/jobs/{id}/results
        results_match = re.match(r"^/api/v1/agent/jobs/([a-zA-Z0-9-]+)/results$", path)
        if results_match:
            cluster = self._authenticate_agent()
            if not cluster:
                self._send_error("Unauthorized", 401)
                return
            job_id = results_match.group(1)
            verdict = body.get("verdict", "UNKNOWN")
            duration = body.get("recovery_duration_seconds")
            inv_total = body.get("invariants_total", 0)
            inv_passed = body.get("invariants_passed", 0)
            inv_failed = body.get("invariants_failed", 0)
            evidence = body.get("evidence", {})
            html_report = body.get("html_report_path", "")

            self.db.update_experiment_status(job_id, status="completed", verdict=verdict, recovery_duration=duration)
            report = self.db.store_report(
                run_id=job_id,
                invariants_total=inv_total,
                invariants_passed=inv_passed,
                invariants_failed=inv_failed,
                evidence_json=evidence,
                html_path=html_report
            )
            self._send_json({"status": "stored", "report_id": report["id"]})
            return

        self._send_error("Endpoint not found", 404)

    # -------------------------------------------------------------
    # PUT Handlers (Update Application)
    # -------------------------------------------------------------
    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        app_match = re.match(r"^/api/v1/apps/([a-zA-Z0-9-]+)$", path)
        if app_match:
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            app_id = app_match.group(1)
            updated = self.db.update_application(user["id"], app_id, **body)
            if not updated:
                self._send_error("Application not found or unauthorized", 404)
                return
            self.db.log_audit(user["id"], "APP_UPDATE", "application", app_id)
            self._send_json({"application": updated})
            return

        self._send_error("Endpoint not found", 404)

    # -------------------------------------------------------------
    # DELETE Handlers (Delete Application)
    # -------------------------------------------------------------
    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        app_match = re.match(r"^/api/v1/apps/([a-zA-Z0-9-]+)$", path)
        if app_match:
            user = self._authenticate()
            if not user:
                self._send_error("Unauthorized", 401)
                return
            app_id = app_match.group(1)
            success = self.db.delete_application(user["id"], app_id)
            if not success:
                self._send_error("Application not found or unauthorized", 404)
                return
            self.db.log_audit(user["id"], "APP_DELETE", "application", app_id)
            self._send_json({"message": "Application deleted successfully"})
            return

        self._send_error("Endpoint not found", 404)

    def log_message(self, format, *args):
        """Suppress standard logging in console."""
        pass

def run_platform_server(host: str = "0.0.0.0", port: int = 8000):
    """Start multi-threaded HTTP server for ChaosLab Hosted SaaS Platform."""
    server_address = (host, port)
    httpd = HTTPServer(server_address, SaaSPlatformRequestHandler)
    print("====================================================================================================")
    print("CHAOSLAB HOSTED SAAS CONTROL PLANE & REST API")
    print("====================================================================================================")
    print(f"Platform API listening at: http://localhost:{port}/api/v1/health")
    print(f"Hosted Web Dashboard at:   http://localhost:{port}/")
    print("====================================================================================================")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down ChaosLab SaaS Control Plane...")
        httpd.server_close()

if __name__ == "__main__":
    port_arg = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
    run_platform_server(port=port_arg)
