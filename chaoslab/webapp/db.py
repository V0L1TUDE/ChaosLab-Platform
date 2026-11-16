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
CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name              TEXT    NOT NULL,
    description       TEXT    NOT NULL DEFAULT '',
    file_path         TEXT,
    original_filename TEXT,
    file_size         INTEGER,
    upload_type       TEXT    NOT NULL,       -- 'builtin' | file extension of the upload
    status            TEXT    NOT NULL,       -- 'ready' (built-in) | 'registered' (upload)
    slug              TEXT UNIQUE,            -- stable key for built-in apps
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
    completed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_experiments_user ON experiments(user_id, id DESC);

CREATE TABLE IF NOT EXISTS experiment_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    ts            TEXT    NOT NULL,
    stage         TEXT    NOT NULL,
    level         TEXT    NOT NULL DEFAULT 'info',
    message       TEXT    NOT NULL,
    simulated     INTEGER NOT NULL DEFAULT 0
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
