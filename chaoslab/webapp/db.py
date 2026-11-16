"""
ChaosLab Online MVP - SQLite persistence
========================================
Schema: users, sessions, applications, experiments, experiment_events, reports.
A new short-lived connection is opened per call, which keeps the stdlib threaded
HTTP server and the experiment worker threads safe.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
import sqlite3
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);

-- user_id is NULL for built-in applications (visible to every user, never deletable).
CREATE TABLE IF NOT EXISTS clusters (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name               TEXT    NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'pending',   -- pending | connected | revoked
    paused             INTEGER NOT NULL DEFAULT 0,           -- emergency stop
    enroll_token_hash  TEXT,
    enroll_expires_at  TEXT,
    credential_hash    TEXT UNIQUE,
    cluster_uid        TEXT,                                 -- kube-system namespace UID: one account per cluster
    kube_version       TEXT,
    agent_version      TEXT,
    allowed_namespaces TEXT,                                 -- JSON list, reported by the agent (its local allow-list)
    inventory_json     TEXT,                                 -- agent-reported deployments per allowed namespace
    inventory_at       TEXT,
    last_seen          TEXT,
    created_at         TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clusters_uid ON clusters(cluster_uid) WHERE cluster_uid IS NOT NULL;

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id    INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    cluster_id       INTEGER NOT NULL REFERENCES clusters(id),
    status           TEXT    NOT NULL,       -- queued | leased | running | done | failed | cancelled | expired
    spec_json        TEXT    NOT NULL,
    job_token_hash   TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    result_json      TEXT,
    error            TEXT,
    created_at       TEXT    NOT NULL,
    leased_at        TEXT,
    lease_expires_at TEXT,
    started_at       TEXT,
    finished_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_cluster ON jobs(cluster_id, status);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    user_id    INTEGER,
    cluster_id INTEGER,
    actor      TEXT    NOT NULL,        -- user | agent | system
    action     TEXT    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '',
    ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_cluster ON audit_log(cluster_id, id DESC);

CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name              TEXT    NOT NULL,
    description       TEXT    NOT NULL DEFAULT '',
    file_path         TEXT,                   -- local path or S3 object key (see storage_backend)
    storage_backend   TEXT    NOT NULL DEFAULT 'local',
    original_filename TEXT,
    file_size         INTEGER,
    upload_type       TEXT    NOT NULL,       -- 'builtin' | file extension of the upload
    status            TEXT    NOT NULL,       -- 'ready' (built-in) | 'registered' (upload)
    slug              TEXT UNIQUE,            -- stable key for built-in apps
    manifest_json     TEXT,                   -- sanitized, validated manifests (what gets deployed)
    validation_json   TEXT,                   -- {valid, errors, warnings, workloads}
    namespace         TEXT,                   -- sandbox namespace while deployed
    deployed_at       TEXT,
    deploy_error      TEXT,
    cluster_id        INTEGER REFERENCES clusters(id),   -- set for applications living in a connected cluster
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_user ON applications(user_id);

CREATE TABLE IF NOT EXISTS experiments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    application_id   INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    target_service   TEXT    NOT NULL,
    fault_type       TEXT    NOT NULL,
    duration_seconds INTEGER NOT NULL,
    config_json      TEXT    NOT NULL,        -- user-selected + resolved engine configuration
    mode             TEXT    NOT NULL,        -- 'live' | 'simulated'
    status           TEXT    NOT NULL,        -- pending | running | completed | failed
    current_stage    TEXT    NOT NULL DEFAULT 'pending',
    percent          INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    oracle_verdict   TEXT,                    -- PASS | FAIL
    recovery_seconds REAL,
    result_json      TEXT,                    -- normalized result (timeline, metrics, oracle)
    created_at       TEXT    NOT NULL,
    started_at       TEXT,
    completed_at     TEXT,
    cluster_id       INTEGER REFERENCES clusters(id)          -- set when executed by a connected cluster's agent
);
CREATE INDEX IF NOT EXISTS idx_experiments_user ON experiments(user_id, id DESC);

CREATE TABLE IF NOT EXISTS experiment_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    ts            TEXT    NOT NULL,
    stage         TEXT    NOT NULL,
    level         TEXT    NOT NULL DEFAULT 'info',
    message       TEXT    NOT NULL,
    simulated     INTEGER NOT NULL DEFAULT 0,
    data          TEXT                       -- JSON payload for metric samples
);
CREATE INDEX IF NOT EXISTS idx_events_experiment ON experiment_events(experiment_id, id);

CREATE TABLE IF NOT EXISTS reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    format         TEXT    NOT NULL,          -- 'json' | 'html'
    file_path      TEXT    NOT NULL,
    overall_result TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    UNIQUE (experiment_id, format)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Transactional connection: commits on success, rolls back on error, always closes."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    for d in (config.DATA_DIR, config.UPLOAD_DIR, config.REPORT_DIR):
        os.makedirs(d, exist_ok=True)
    with session() as conn:
        conn.executescript(SCHEMA)
        # Lightweight migration for databases created before storage_backend existed.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(applications)")}
        if "storage_backend" not in cols:
            conn.execute("ALTER TABLE applications ADD COLUMN storage_backend TEXT NOT NULL DEFAULT 'local'")
        for col in ("manifest_json", "validation_json", "namespace", "deployed_at", "deploy_error"):
            if col not in cols:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {col} TEXT")
        if "cluster_id" not in cols:
            conn.execute("ALTER TABLE applications ADD COLUMN cluster_id INTEGER REFERENCES clusters(id)")
        exp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(experiments)")}
        if "cluster_id" not in exp_cols:
            conn.execute("ALTER TABLE experiments ADD COLUMN cluster_id INTEGER REFERENCES clusters(id)")
