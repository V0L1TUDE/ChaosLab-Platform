"""
ChaosLab Platform Database Engine & Multi-Tenant Data Access Layer
=================================================================
Supports SQLite (development/local) and PostgreSQL (production).
Enforces foreign key integrity, UUID primary keys, and tenant-scoped queries.
"""

from datetime import datetime, timezone
import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import uuid

DB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
DEFAULT_DB_PATH = os.path.join(DB_DIR, "chaoslab_platform.db")

class Database:
    """Thread-safe SQLite database manager with connection pooling and schema migrations."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.environ.get("CHAOSLAB_DB_PATH", DEFAULT_DB_PATH)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(db_path)
            return cls._instance

    def get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self):
        """Initialize database tables with relational foreign keys and indexes."""
        conn = self.get_connection()
        with conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'developer',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

            CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                repo_url TEXT,
                target_namespace TEXT NOT NULL DEFAULT 'default',
                health_endpoint TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_apps_user ON applications(user_id);

            CREATE TABLE IF NOT EXISTS clusters (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                environment TEXT NOT NULL DEFAULT 'development',
                status TEXT NOT NULL DEFAULT 'pending',
                agent_version TEXT,
                last_heartbeat TEXT,
                cluster_info TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_clusters_user ON clusters(user_id);

            CREATE TABLE IF NOT EXISTS enrollment_tokens (
                id TEXT PRIMARY KEY,
                cluster_id TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_used INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (cluster_id) REFERENCES clusters(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_enrollment_token ON enrollment_tokens(token);

            CREATE TABLE IF NOT EXISTS experiment_runs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                application_id TEXT NOT NULL,
                cluster_id TEXT NOT NULL,
                experiment_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                verdict TEXT DEFAULT 'UNKNOWN',
                recovery_duration_seconds REAL,
                started_at TEXT,
                completed_at TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE,
                FOREIGN KEY (cluster_id) REFERENCES clusters(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_runs_user ON experiment_runs(user_id);
            CREATE INDEX IF NOT EXISTS idx_runs_cluster ON experiment_runs(cluster_id);
            CREATE INDEX IF NOT EXISTS idx_runs_status ON experiment_runs(status);

            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                experiment_run_id TEXT UNIQUE NOT NULL,
                invariants_total INTEGER NOT NULL DEFAULT 0,
                invariants_passed INTEGER NOT NULL DEFAULT 0,
                invariants_failed INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT,
                html_report_path TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (experiment_run_id) REFERENCES experiment_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT,
                details TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id);
            """)

    # -------------------------------------------------------------
    # User Management
    # -------------------------------------------------------------
    def create_user(self, email: str, hashed_password: str, full_name: str, role: str = "developer") -> Dict[str, Any]:
        user_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute(
                "INSERT INTO users (id, email, hashed_password, full_name, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, email.lower().strip(), hashed_password, full_name.strip(), role, created_at)
            )
        return self.get_user_by_id(user_id)

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT id, email, full_name, role, created_at FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------
    # Application Management (Tenant Isolated)
    # -------------------------------------------------------------
    def create_application(self, user_id: str, name: str, description: str = "",
                           repo_url: str = "", target_namespace: str = "default",
                           health_endpoint: str = "") -> Dict[str, Any]:
        app_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute(
                """INSERT INTO applications 
                   (id, user_id, name, description, repo_url, target_namespace, health_endpoint, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (app_id, user_id, name.strip(), description, repo_url, target_namespace, health_endpoint, created_at)
            )
        return self.get_application(user_id, app_id)

    def list_applications(self, user_id: str) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM applications WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        return [dict(row) for row in cur.fetchall()]

    def get_application(self, user_id: str, app_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM applications WHERE id = ? AND user_id = ?", (app_id, user_id))
        row = cur.fetchone()
        return dict(row) if row else None

    def update_application(self, user_id: str, app_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        allowed = {"name", "description", "repo_url", "target_namespace", "health_endpoint"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_application(user_id, app_id)

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        params = list(updates.values()) + [app_id, user_id]

        conn = self.get_connection()
        with conn:
            cur = conn.execute(f"UPDATE applications SET {set_clause} WHERE id = ? AND user_id = ?", params)
            if cur.rowcount == 0:
                return None
        return self.get_application(user_id, app_id)

    def delete_application(self, user_id: str, app_id: str) -> bool:
        conn = self.get_connection()
        with conn:
            cur = conn.execute("DELETE FROM applications WHERE id = ? AND user_id = ?", (app_id, user_id))
            return cur.rowcount > 0

    # -------------------------------------------------------------
    # Cluster Management
    # -------------------------------------------------------------
    def create_cluster(self, user_id: str, name: str, environment: str = "development") -> Dict[str, Any]:
        cluster_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute(
                """INSERT INTO clusters (id, user_id, name, environment, status, created_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (cluster_id, user_id, name.strip(), environment, created_at)
            )
        return self.get_cluster(user_id, cluster_id)

    def list_clusters(self, user_id: str) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM clusters WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        clusters = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("cluster_info"):
                try:
                    d["cluster_info"] = json.loads(d["cluster_info"])
                except Exception:
                    pass
            clusters.append(d)
        return clusters

    def get_cluster(self, user_id: str, cluster_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM clusters WHERE id = ? AND user_id = ?", (cluster_id, user_id))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("cluster_info"):
            try:
                d["cluster_info"] = json.loads(d["cluster_info"])
            except Exception:
                pass
        return d

    def get_cluster_by_id_internal(self, cluster_id: str) -> Optional[Dict[str, Any]]:
        """Internal lookup without user_id restriction (used by Agent API)."""
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM clusters WHERE id = ?", (cluster_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def update_cluster_heartbeat(self, cluster_id: str, agent_version: str, cluster_info: Dict[str, Any]):
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute(
                """UPDATE clusters 
                   SET status = 'active', agent_version = ?, last_heartbeat = ?, cluster_info = ?
                   WHERE id = ?""",
                (agent_version, now, json.dumps(cluster_info), cluster_id)
            )

    # -------------------------------------------------------------
    # Enrollment Tokens
    # -------------------------------------------------------------
    def create_enrollment_token(self, cluster_id: str, expires_in_seconds: int = 900) -> str:
        token_id = str(uuid.uuid4())
        token_str = f"cl-enroll-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        expires_at = datetime.fromtimestamp(now.timestamp() + expires_in_seconds, tz=timezone.utc).isoformat()

        conn = self.get_connection()
        with conn:
            conn.execute(
                """INSERT INTO enrollment_tokens (id, cluster_id, token, created_at, expires_at, is_used)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (token_id, cluster_id, token_str, created_at, expires_at)
            )
        return token_str

    def validate_and_consume_enrollment_token(self, token_str: str) -> Optional[str]:
        """Validate token exists, is not used, and has not expired. If valid, mark used and return cluster_id."""
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM enrollment_tokens WHERE token = ?", (token_str,))
        row = cur.fetchone()
        if not row:
            return None

        if row["is_used"]:
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            return None

        cluster_id = row["cluster_id"]
        with conn:
            conn.execute("UPDATE enrollment_tokens SET is_used = 1 WHERE id = ?", (row["id"],))
            conn.execute("UPDATE clusters SET status = 'active' WHERE id = ?", (cluster_id,))

        return cluster_id

    # -------------------------------------------------------------
    # Experiment Runs & Jobs
    # -------------------------------------------------------------
    def create_experiment_run(self, user_id: str, application_id: str, cluster_id: str,
                              experiment_name: str) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute(
                """INSERT INTO experiment_runs 
                   (id, user_id, application_id, cluster_id, experiment_name, status, verdict, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', 'UNKNOWN', ?)""",
                (run_id, user_id, application_id, cluster_id, experiment_name, created_at)
            )
        return self.get_experiment_run(user_id, run_id)

    def get_experiment_run(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute(
            """SELECT r.*, a.name as application_name, c.name as cluster_name 
               FROM experiment_runs r
               JOIN applications a ON r.application_id = a.id
               JOIN clusters c ON r.cluster_id = c.id
               WHERE r.id = ? AND r.user_id = ?""",
            (run_id, user_id)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_experiment_runs(self, user_id: str) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute(
            """SELECT r.*, a.name as application_name, c.name as cluster_name 
               FROM experiment_runs r
               JOIN applications a ON r.application_id = a.id
               JOIN clusters c ON r.cluster_id = c.id
               WHERE r.user_id = ?
               ORDER BY r.created_at DESC""",
            (user_id,)
        )
        return [dict(row) for row in cur.fetchall()]

    def poll_pending_job_for_cluster(self, cluster_id: str) -> Optional[Dict[str, Any]]:
        """Agent query: grab next pending job for this cluster and transition to 'dispatched'."""
        conn = self.get_connection()
        with conn:
            cur = conn.execute(
                """SELECT * FROM experiment_runs 
                   WHERE cluster_id = ? AND status = 'pending'
                   ORDER BY created_at ASC LIMIT 1""",
                (cluster_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            job = dict(row)
            started_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE experiment_runs SET status = 'dispatched', started_at = ? WHERE id = ?",
                (started_at, job["id"])
            )
            job["status"] = "dispatched"
            job["started_at"] = started_at
            return job

    def update_experiment_status(self, run_id: str, status: str, verdict: Optional[str] = None,
                                 recovery_duration: Optional[float] = None, error_message: Optional[str] = None):
        completed_at = datetime.now(timezone.utc).isoformat() if status in ["completed", "failed"] else None
        conn = self.get_connection()
        with conn:
            conn.execute(
                """UPDATE experiment_runs 
                   SET status = ?, verdict = COALESCE(?, verdict), 
                       recovery_duration_seconds = COALESCE(?, recovery_duration_seconds),
                       completed_at = COALESCE(?, completed_at),
                       error_message = COALESCE(?, error_message)
                   WHERE id = ?""",
                (status, verdict, recovery_duration, completed_at, error_message, run_id)
            )

    # -------------------------------------------------------------
    # Reports & Evidence
    # -------------------------------------------------------------
    def store_report(self, run_id: str, invariants_total: int, invariants_passed: int,
                     invariants_failed: int, evidence_json: Dict[str, Any], html_path: str = "") -> Dict[str, Any]:
        report_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute(
                """INSERT INTO reports 
                   (id, experiment_run_id, invariants_total, invariants_passed, invariants_failed, evidence_json, html_report_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (report_id, run_id, invariants_total, invariants_passed, invariants_failed, json.dumps(evidence_json), html_path, created_at)
            )
        return self.get_report_by_run_id(run_id)

    def get_report_by_run_id(self, run_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM reports WHERE experiment_run_id = ?", (run_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("evidence_json"):
            try:
                d["evidence_json"] = json.loads(d["evidence_json"])
            except Exception:
                pass
        return d

    # -------------------------------------------------------------
    # Audit Logging
    # -------------------------------------------------------------
    def log_audit(self, user_id: str, action: str, resource_type: str, resource_id: str = "", details: Optional[Dict[str, Any]] = None):
        log_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute(
                """INSERT INTO audit_logs (id, user_id, action, resource_type, resource_id, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (log_id, user_id, action, resource_type, resource_id, json.dumps(details or {}), created_at)
            )

    def list_audit_logs(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute(
            "SELECT * FROM audit_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        )
        logs = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("details"):
                try:
                    d["details"] = json.loads(d["details"])
                except Exception:
                    pass
            logs.append(d)
        return logs
