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

# Deploying uploaded manifests into per-application sandbox namespaces. OFF by default (fail closed): running
# workloads from untrusted users needs the isolated tenant runtime below. Even when this is true, every deploy is
# refused until tenant_isolation.preflight() confirms the sandbox runtime, tenant nodes, admission policies and a
# least-privilege identity (see chaoslab/deploy/tenant/README.md).
DEPLOY_ENABLED = _env_bool("CHAOSLAB_DEPLOY_ENABLED", False)
# Tenant cluster: the ONLY credentials the web app uses for uploaded workloads (a dedicated, least-privilege
# ServiceAccount; never cluster-admin). The kubeconfig file stays on the server and is never sent to the browser.
# When set, it is also the server process's ONLY cluster identity (KUBECONFIG for every kubectl the server runs:
# discovery, Live runs on sandbox apps, deploys), so no broader credential is ever in reach. Its current-context must
# point at the tenant cluster.
TENANT_KUBECONFIG = os.environ.get("CHAOSLAB_TENANT_KUBECONFIG", "").strip()
if TENANT_KUBECONFIG:
    os.environ["KUBECONFIG"] = TENANT_KUBECONFIG
# RuntimeClass of a sandboxed runtime (gVisor `runsc` or Kata Containers) installed on the tenant nodes.
TENANT_RUNTIME_CLASS = os.environ.get("CHAOSLAB_TENANT_RUNTIME_CLASS", "").strip()
# Label (key=value) of the dedicated tenant node pool; those nodes must also carry the NoSchedule taint key=value.
TENANT_NODE_LABEL = os.environ.get("CHAOSLAB_TENANT_NODE_LABEL", "chaoslab.io/tenant-pool=true").strip()
# Operator attestation that chaoslab/deploy/tenant/verify_isolation.py passed against this cluster for the current
# policy version (network-policy enforcement and the sandbox kernel cannot be confirmed through the API alone).
TENANT_ISOLATION_VERIFIED = os.environ.get("CHAOSLAB_TENANT_ISOLATION_VERIFIED", "").strip()
DEPLOY_TTL_MINUTES = _env_int("CHAOSLAB_DEPLOY_TTL_MINUTES", 120)        # auto-undeploy after this long
DEPLOY_TIMEOUT_SECONDS = _env_int("CHAOSLAB_DEPLOY_TIMEOUT_SECONDS", 180)
MAX_DEPLOYED_PER_USER = _env_int("CHAOSLAB_MAX_DEPLOYED_PER_USER", 2)
MAX_DEPLOYED_TOTAL = _env_int("CHAOSLAB_MAX_DEPLOYED_TOTAL", 6)
MAX_REPLICAS = _env_int("CHAOSLAB_MAX_REPLICAS", 3)
# Registries uploaded images may come from (comma separated). Bare names like `nginx:1.27` mean docker.io.
ALLOWED_REGISTRIES = tuple(r.strip().lower() for r in os.environ.get(
    "CHAOSLAB_ALLOWED_REGISTRIES",
    "docker.io,ghcr.io,gcr.io,quay.io,registry.k8s.io,public.ecr.aws,mcr.microsoft.com").split(",") if r.strip())
# The admission policy enforces the same list again inside the cluster, so the two must match: regenerate the
# policy (python -m webapp.tenant_isolation render-policy) after changing it.

# Connected customer clusters (ChaosLab agent, "bring your own cluster")
AGENT_ONLINE_SECONDS = _env_int("CHAOSLAB_AGENT_ONLINE_SECONDS", 45)           # offline if no poll within this
AGENT_QUEUE_TIMEOUT_SECONDS = _env_int("CHAOSLAB_AGENT_QUEUE_TIMEOUT_SECONDS", 120)
ENROLL_TTL_MINUTES = _env_int("CHAOSLAB_ENROLL_TTL_MINUTES", 60)
MAX_CLUSTERS_PER_USER = _env_int("CHAOSLAB_MAX_CLUSTERS_PER_USER", 5)
AGENT_MAX_WAIT_SECONDS = 25                                                     # long-poll ceiling
PUBLIC_URL = os.environ.get("CHAOSLAB_PUBLIC_URL", "").strip().rstrip("/")      # shown in agent install commands

# Running behind a reverse proxy (Caddy) on the same host: take the client IP / scheme from X-Forwarded-*
# headers, but ONLY when the TCP peer is the local proxy. Never enable this without a proxy in front.
TRUST_PROXY = _env_bool("CHAOSLAB_TRUST_PROXY", False)
# "auto" = publicly trusted certificate; "internal" = self-signed CA (agents need --ca-cert, served at /chaoslab-ca.crt)
TLS_MODE = os.environ.get("CHAOSLAB_TLS_MODE", "auto").strip().lower()

# Built-in demo applications (Online Boutique, LMS, Project Platform). OFF by default: users only see their own
# applications (uploads, sandbox apps, connected clusters). Set CHAOSLAB_SHOW_DEMO_APPS=true to expose the demos.
SHOW_DEMO_APPS = _env_bool("CHAOSLAB_SHOW_DEMO_APPS", False)

# Shared Live demos, chosen by the site operator: applications registered on the operator's connected clusters
# that every signed-in user may run experiments against (CHAOSLAB_SHARED_APP_ID=7 or a list: 7,8,9). Other users can only view it and run experiments
# (their experiments and reports stay private to them); they cannot change its probes/tracing, delete it, or touch the
# cluster. 0 = off. Fair use: Live runs per user per hour, and a shorter maximum fault duration for guests.
SHARED_APP_IDS = tuple(int(x) for x in os.environ.get("CHAOSLAB_SHARED_APP_ID", "").replace(" ", "").split(",")
                       if x.isdigit() and int(x) > 0)
SHARED_LIVE_RUNS_PER_HOUR = _env_int("CHAOSLAB_SHARED_LIVE_RUNS_PER_HOUR", 6)
SHARED_MAX_DURATION_SECONDS = _env_int("CHAOSLAB_SHARED_MAX_DURATION_SECONDS", 60)

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
