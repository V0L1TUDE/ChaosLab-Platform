"""Local-only application registry and experiment generator for ChaosLab.

No container uploads, arbitrary manifest apply, shell execution, or public auth.
The configured namespace allowlist must be kept narrow. Always use an isolated
Kubernetes cluster to run generated experiments.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import uuid

import yaml

from verifier.engine import validate_experiment_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "onboarding" / "data" / "applications.sqlite3"
EXPERIMENTS_DIR = ROOT / "onboarding" / "data" / "experiments"
K8S_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
PROTECTED_NS = {"default", "kube-system", "kube-public", "kube-node-lease", "chaos-mesh", "chaoslab-system", "cert-manager"}


class ApplicationError(ValueError):
    """Invalid application, configuration, or disallowed target."""


def allowed_namespaces() -> set[str]:
    """Local demo defaults to a single namespace, not arbitrary customer clusters."""
    raw = os.environ.get("CHAOSLAB_ALLOWED_NAMESPACES", "chaoslab-demo")
    return {x.strip() for x in raw.split(",") if x.strip() and x.strip() not in PROTECTED_NS and not x.strip().startswith("kube-")}


def clean_name(value: object, field: str) -> str:
    if not isinstance(value, str) or not K8S_NAME.fullmatch(value) or len(value) > 63:
        raise ApplicationError(f"{field} must be a lowercase Kubernetes name (letters, digits, hyphens; max 63 chars)")
    return value


def validate_registration(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ApplicationError("JSON object expected")
    name = payload.get("name", "")
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= 80):
        raise ApplicationError("Application name must contain 1–80 characters")
    description = payload.get("description", "")
    if not isinstance(description, str) or len(description) > 500:
        raise ApplicationError("Description must contain at most 500 characters")
    namespace = clean_name(payload.get("namespace"), "Namespace")
    if namespace not in allowed_namespaces():
        raise ApplicationError("Namespace not permitted. Configure CHAOSLAB_ALLOWED_NAMESPACES for an isolated test namespace")
    app_label = clean_name(payload.get("app_label"), "app label")
    replicas = payload.get("expected_replicas", 1)
    if type(replicas) is not int or not (1 <= replicas <= 5):
        raise ApplicationError("Expected replicas must be an integer from 1 to 5")
    return {"name": name.strip(), "description": description.strip(),
            "namespace": namespace, "app_label": app_label, "expected_replicas": replicas}


class ApplicationRegistry:
    def __init__(self, database_path: str | Path = DEFAULT_DB, experiments_dir: str | Path = EXPERIMENTS_DIR):
        self.database_path = Path(database_path)
        self.experiments_dir = Path(experiments_dir)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    def _connect(self):
        db = sqlite3.connect(str(self.database_path), timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def _create_schema(self):
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                namespace TEXT NOT NULL, app_label TEXT NOT NULL,
                expected_replicas INTEGER NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(namespace, app_label))""")

    def register(self, payload: dict) -> dict:
        item = validate_registration(payload)
        item["id"] = uuid.uuid4().hex[:12]
        item["created_at"] = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as db:
                db.execute("""INSERT INTO applications
                    (id,name,description,namespace,app_label,expected_replicas,created_at)
                    VALUES (:id,:name,:description,:namespace,:app_label,:expected_replicas,:created_at)""", item)
        except sqlite3.IntegrityError as err:
            raise ApplicationError("That namespace and app label are already registered") from err
        return item

    def list_apps(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def get(self, app_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{12}", app_id):
            raise ApplicationError("Invalid application ID")
        with self._connect() as db:
            row = db.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        if row is None:
            raise ApplicationError("Application not found")
        return dict(row)

    def discover_pods(self, app_id: str) -> dict:
        app = self.get(app_id)
        if app["namespace"] not in allowed_namespaces():
            raise ApplicationError("Namespace no longer permitted")
        command = ["kubectl", "get", "pods", "-n", app["namespace"],
                   "-l", f"app={app['app_label']}", "-o", "json", "--request-timeout=6s"]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return {"connected": False, "pods": [], "error": f"kubectl unavailable or timed out: {exc}"}
        if completed.returncode:
            return {"connected": False, "pods": [], "error": completed.stderr.strip()[:300] or "kubectl returned an error"}
        try:
            pods = json.loads(completed.stdout)["items"]
            if not isinstance(pods, list):
                raise ValueError("Pod list expected")
            summaries = []
            for pod in pods:
                metadata = pod.get("metadata", {})
                status = pod.get("status", {})
                ready = (status.get("phase") == "Running"
                         and not metadata.get("deletionTimestamp")
                         and any(c.get("type") == "Ready" and c.get("status") == "True"
                                 for c in status.get("conditions", [])))
                summaries.append({"name": metadata.get("name", ""),
                                  "phase": status.get("phase", "Unknown"), "ready": ready})
        except (ValueError, KeyError, TypeError, AttributeError):
            return {"connected": False, "pods": [], "error": "Invalid response from Kubernetes"}
        ready_count = sum(p["ready"] for p in summaries)
        return {"connected": True, "count": len(pods), "pods": summaries[:50],
                "ready_count": ready_count, "expected_replicas": app["expected_replicas"],
                "workload_ready": ready_count >= app["expected_replicas"]}

    def generate_experiment(self, app_id: str) -> dict:
        app = self.get(app_id)
        if app["namespace"] not in allowed_namespaces():
            raise ApplicationError("Namespace no longer permitted")
        # A draft configuration, NOT a fault-execution command.
        name = f"registered-{app['id']}-pod-kill"
        payload = {
            "name": name,
            "application_group": app["name"],
            "target": {"namespace": app["namespace"], "label": {"app": app["app_label"]}},
            "fault": {"type": "pod-kill", "mode": "one"},
            "recovery": {"timeout_seconds": 60, "expected_ready_replicas": app["expected_replicas"]},
            "application": {"enabled": False},
            "telemetry": {"enabled": False},
        }
        validate_experiment_config(payload)
        self.experiments_dir.mkdir(parents=True, exist_ok=True)
        target = self.experiments_dir / f"{name}.yaml"
        # Atomic replacement: generator owns only its own registered-<uuid> name.
        temporary = self.experiments_dir / f".{name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                yaml.safe_dump(payload, stream, sort_keys=False)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"name": name, "relative_path": f"chaoslab/onboarding/data/experiments/{target.name}",
                "status": "DRAFT_NOT_EXECUTED", "warning": "Only execute on an isolated, approved Kubernetes test cluster."}
