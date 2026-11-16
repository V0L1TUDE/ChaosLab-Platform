"""
ChaosLab Online MVP - configuration
===================================
All settings come from environment variables with safe local-development defaults.
No secrets are hard-coded or exposed to the frontend.
"""

import os

WEBAPP_DIR = os.path.dirname(os.path.abspath(__file__))
CHAOSLAB_DIR = os.path.dirname(WEBAPP_DIR)
REPO_ROOT = os.path.dirname(CHAOSLAB_DIR)
VERIFIER_DIR = os.path.join(CHAOSLAB_DIR, "verifier")
EXPERIMENTS_DIR = os.path.join(CHAOSLAB_DIR, "experiments")
STATIC_DIR = os.path.join(WEBAPP_DIR, "static")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Persistent data (SQLite database, uploaded files, generated reports)
DATA_DIR = os.path.abspath(os.environ.get("CHAOSLAB_DATA_DIR", os.path.join(WEBAPP_DIR, "data")))
DB_PATH = os.path.abspath(os.environ.get("CHAOSLAB_DB_PATH", os.path.join(DATA_DIR, "chaoslab.db")))
UPLOAD_DIR = os.path.abspath(os.environ.get("CHAOSLAB_UPLOAD_DIR", os.path.join(DATA_DIR, "uploads")))
REPORT_DIR = os.path.abspath(os.environ.get("CHAOSLAB_REPORT_DIR", os.path.join(DATA_DIR, "reports")))

# Server
HOST = os.environ.get("CHAOSLAB_HOST", "127.0.0.1")
PORT = _env_int("CHAOSLAB_PORT", 8090)

# Auth
SESSION_TTL_SECONDS = _env_int("CHAOSLAB_SESSION_TTL_SECONDS", 7 * 24 * 3600)
SESSION_COOKIE = "chaoslab_session"
# Set to true when serving over HTTPS so the session cookie gets the Secure flag.
COOKIE_SECURE = _env_bool("CHAOSLAB_COOKIE_SECURE", False)
MIN_PASSWORD_LENGTH = 8

# Uploads
MAX_UPLOAD_BYTES = _env_int("CHAOSLAB_MAX_UPLOAD_MB", 10) * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".yaml", ".yml", ".json", ".md", ".txt")

# Storage of uploaded files: "local" (default) or "s3". S3 credentials come from the standard boto3
# chain (IAM role, AWS_* env vars, profile); they are never configured here or exposed to the browser.
STORAGE_BACKEND = os.environ.get("CHAOSLAB_STORAGE", "local").strip().lower()
S3_BUCKET = os.environ.get("CHAOSLAB_S3_BUCKET", "").strip()
S3_REGION = os.environ.get("CHAOSLAB_S3_REGION", "").strip()
S3_ENDPOINT_URL = os.environ.get("CHAOSLAB_S3_ENDPOINT_URL", "").strip()          # MinIO / LocalStack
_prefix = os.environ.get("CHAOSLAB_S3_PREFIX", "uploads/").strip().lstrip("/")
S3_PREFIX = _prefix if (not _prefix or _prefix.endswith("/")) else _prefix + "/"
S3_SSE = os.environ.get("CHAOSLAB_S3_SSE", "AES256").strip()                       # AES256 | aws:kms | none
S3_KMS_KEY_ID = os.environ.get("CHAOSLAB_S3_KMS_KEY_ID", "").strip()

# Cluster service discovery (read-only `kubectl get deployments`); falls back to a static list.
DISCOVERY_ENABLED = _env_bool("CHAOSLAB_DISCOVERY_ENABLED", True)

# Execution
# Live execution drives the real ChaosLab engine against the configured kubectl context.
LIVE_ENABLED = _env_bool("CHAOSLAB_LIVE_ENABLED", True)
# Multiplier applied to simulated pacing (1.0 ~ 25s run; use 0.05 for fast tests).
SIM_SPEED = _env_float("CHAOSLAB_SIM_SPEED", 1.0)
MAX_ACTIVE_EXPERIMENTS_PER_USER = _env_int("CHAOSLAB_MAX_ACTIVE_PER_USER", 3)
MIN_DURATION_SECONDS = 10
MAX_DURATION_SECONDS = 120
