"""SQLite storage.

Both source tables (respondent roster, population targets) are kept as
immutable versioned snapshots.  A separate pointer row tracks which version
is currently in service, so a failed replacement never disturbs the live
table and its existing results.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DEFAULT_DB_PATH = os.environ.get("WEIGHTING_DB_PATH", "/data/weighting.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS respondent_versions (
    version      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    row_count    INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS respondent_records (
    version      INTEGER NOT NULL,
    household_id TEXT NOT NULL,
    district     TEXT NOT NULL,
    size_band    TEXT NOT NULL,
    init_weight  REAL NOT NULL CHECK (init_weight > 0),
    PRIMARY KEY (version, household_id),
    FOREIGN KEY (version) REFERENCES respondent_versions(version)
);

CREATE TABLE IF NOT EXISTS target_versions (
    version      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    row_count    INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS target_records (
    version      INTEGER NOT NULL,
    dimension    TEXT NOT NULL CHECK (dimension IN ('district', 'size_band')),
    group_name   TEXT NOT NULL,
    target_count REAL NOT NULL CHECK (target_count >= 0),
    PRIMARY KEY (version, dimension, group_name),
    FOREIGN KEY (version) REFERENCES target_versions(version)
);

CREATE TABLE IF NOT EXISTS current_versions (
    table_name   TEXT PRIMARY KEY
                 CHECK (table_name IN ('respondents', 'targets')),
    version      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS weighting_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    respondent_version  INTEGER NOT NULL,
    target_version      INTEGER NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('running', 'balanced',
                                          'unbalanced', 'failed')),
    is_balanced         INTEGER,
    objective_gap       REAL,
    error_reason        TEXT,
    created_at          TEXT NOT NULL,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_versions
    ON weighting_jobs(respondent_version, target_version);

CREATE TABLE IF NOT EXISTS weight_results (
    job_id       INTEGER NOT NULL,
    household_id TEXT NOT NULL,
    weight       REAL NOT NULL,
    PRIMARY KEY (job_id, household_id),
    FOREIGN KEY (job_id) REFERENCES weighting_jobs(id)
);

CREATE TABLE IF NOT EXISTS diff_results (
    job_id       INTEGER NOT NULL,
    dimension    TEXT NOT NULL,
    group_name   TEXT NOT NULL,
    weighted     REAL NOT NULL,
    target_count REAL NOT NULL,
    difference   REAL NOT NULL,
    PRIMARY KEY (job_id, dimension, group_name),
    FOREIGN KEY (job_id) REFERENCES weighting_jobs(id)
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def db_session(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
