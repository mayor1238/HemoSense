"""HemoSense storage.

SQLite on a laptop (no setup), PostgreSQL on the Nobus server.
Set DATABASE_URL to choose:
    sqlite:///hemosense.db
    postgresql://user:password@db:5432/hemosense
Queries use :name placeholders; they are rewritten for PostgreSQL.
"""
import json
import os
import re
import sqlite3
import threading

TABLES = (
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
        password_hash TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS patients (
        id TEXT PRIMARY KEY, id_type TEXT NOT NULL, id_value TEXT NOT NULL, full_name TEXT NOT NULL,
        dob TEXT, sex TEXT, created_at TEXT NOT NULL, created_by TEXT,
        UNIQUE (id_type, id_value))""",
    """CREATE TABLE IF NOT EXISTS tests (
        id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, operator TEXT NOT NULL, user_id TEXT,
        cartridge_id TEXT NOT NULL, reagent_lot TEXT NOT NULL, reader_id TEXT NOT NULL,
        firmware TEXT, simulated INTEGER NOT NULL DEFAULT 0, facility TEXT,
        research_consent INTEGER NOT NULL DEFAULT 0, sweep_s REAL NOT NULL, interval_s REAL NOT NULL,
        status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        blood_group TEXT, result_valid INTEGER, confidence REAL, method TEXT, notes TEXT,
        analysis_settings TEXT, lab_group TEXT, saved_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS readings (
        test_id TEXT NOT NULL, seq INTEGER NOT NULL, t_ms INTEGER NOT NULL,
        z_a REAL, z_b REAL, z_d REAL, re_a REAL, re_b REAL, re_d REAL, im_a REAL, im_b REAL, im_d REAL,
        PRIMARY KEY (test_id, seq))""",
    """CREATE TABLE IF NOT EXISTS research (
        test_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, reader_id TEXT, firmware TEXT, simulated INTEGER,
        reagent_lot TEXT, age_band TEXT, sex TEXT, device_group TEXT, lab_group TEXT, features TEXT)""",
    """CREATE TABLE IF NOT EXISTS app_settings (
        name TEXT PRIMARY KEY, value TEXT NOT NULL)""",
)


class Store:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("DATABASE_URL", "sqlite:///hemosense.db")
        self._lock = threading.Lock()
        self.is_pg = self.url.startswith("postgres")
        self.conn = self._connect()
        for sql in TABLES:
            self.run(sql)

    def _connect(self):
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row
            return psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        conn = sqlite3.connect(self.url.removeprefix("sqlite:///"), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _exec(self, sql: str, args: dict | None):
        if self.is_pg:
            sql = re.sub(r"(?<![:\w]):([a-z_][a-z0-9_]*)", r"%(\1)s", sql)
        try:
            return self.conn.execute(sql, args) if args else self.conn.execute(sql)
        except Exception:
            if self.is_pg and self.conn.closed:  # database restarted: reconnect once
                self.conn = self._connect()
                return self.conn.execute(sql, args) if args else self.conn.execute(sql)
            raise

    def run(self, sql: str, **args) -> int:
        with self._lock:
            return self._exec(sql, args).rowcount

    def rows(self, sql: str, **args) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._exec(sql, args).fetchall()]

    def row(self, sql: str, **args) -> dict | None:
        found = self.rows(sql, **args)
        return found[0] if found else None


def to_json(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def from_json(text, fallback=None):
    try:
        return json.loads(text) if text else fallback
    except (TypeError, ValueError):
        return fallback
