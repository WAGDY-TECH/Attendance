"""SQLite persistence helpers."""
from __future__ import annotations

import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    admission_no TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embeddings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    engine     TEXT NOT NULL,
    vector     BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_engine ON embeddings(engine);
CREATE INDEX IF NOT EXISTS idx_embeddings_student ON embeddings(student_id);
CREATE TABLE IF NOT EXISTS attendance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    day        TEXT NOT NULL,      -- YYYY-MM-DD (local time)
    check_in   TEXT NOT NULL,      -- HH:MM:SS   (local time)
    source     TEXT NOT NULL DEFAULT 'face',
    confidence REAL,
    UNIQUE (student_id, day)
);
CREATE INDEX IF NOT EXISTS idx_attendance_day ON attendance(day);
"""


def init_db(path) -> None:
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DB_PATH"], timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def mark_attendance(db, student_id: int, day: str, check_in: str, source: str,
                    confidence: float | None) -> bool:
    """Insert today's record once. Returns True only when a new row was created."""
    cur = db.execute(
        "INSERT OR IGNORE INTO attendance(student_id, day, check_in, source, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (student_id, day, check_in, source, confidence),
    )
    db.commit()
    return cur.rowcount == 1
