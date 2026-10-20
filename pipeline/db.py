"""Durable run state in SQLite: source rows, the work queue (one row per distinct text x label_config),
verifier results and the call ledger. One process writes; worker threads only make API calls."""

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
  row_idx INTEGER NOT NULL,
  review_id TEXT PRIMARY KEY,
  review_text TEXT NOT NULL,
  review_rating TEXT, review_likes TEXT, app_version TEXT, review_timestamp TEXT,
  source_sha256 TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  is_empty INTEGER NOT NULL,
  in_sample INTEGER NOT NULL DEFAULT 0,
  queue_rank INTEGER
);
CREATE INDEX IF NOT EXISTS ix_reviews_text ON reviews(text_sha256);
CREATE TABLE IF NOT EXISTS work (
  text_sha256 TEXT NOT NULL,
  label_config TEXT NOT NULL,
  rep_review_id TEXT NOT NULL,
  queue_rank INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | completed | quarantined
  attempts INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  result_json TEXT,
  final_label_config TEXT,
  request_id TEXT,
  phase TEXT,
  completed_at TEXT,
  PRIMARY KEY (text_sha256, label_config)
);
CREATE INDEX IF NOT EXISTS ix_work_status ON work(label_config, status, queue_rank);
CREATE TABLE IF NOT EXISTS calls (
  request_id TEXT PRIMARY KEY,
  run_id TEXT, role TEXT, phase TEXT, model TEXT, effort TEXT, label_config TEXT,
  review_ids TEXT, n_items INTEGER, attempt INTEGER, outcome TEXT, error TEXT,
  input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
  cost_usd REAL, started_at TEXT, duration_s REAL, provider_request_id TEXT
);
CREATE TABLE IF NOT EXISTS verify (
  review_id TEXT PRIMARY KEY,
  label_config TEXT, topic TEXT, intent TEXT, severity INTEGER, request_id TEXT, synthetic INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def spent(con, role=None):
    q = "SELECT COALESCE(SUM(cost_usd),0) s FROM calls" + (" WHERE role=?" if role else "")
    return con.execute(q, (role,) if role else ()).fetchone()["s"]
