"""Esquema SQLite. Los valores de métrica se guardan como TEXT decimal exacto."""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS capture (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL,
  role TEXT NOT NULL,                 -- origen | destino | otro
  source_path TEXT NOT NULL,
  format_version TEXT, collector_version TEXT, dbnode_host TEXT, remote_user TEXT,
  created_utc TEXT, metrics_mode TEXT,
  period_start_raw TEXT, period_end_raw TEXT,
  period_start_epoch INTEGER, period_end_epoch INTEGER,
  block_seconds INTEGER, block_count INTEGER,
  reported_status TEXT,               -- capture_status del manifiesto
  reported_exit_code TEXT,
  prevalidation_status TEXT,
  checksum_status TEXT,               -- OK | MISMATCH | MISSING
  import_status TEXT,                 -- COMPLETE | INCOMPLETE
  imported_utc TEXT,
  sample_count INTEGER DEFAULT 0,
  UNIQUE (dbnode_host, created_utc, label)
);

CREATE TABLE IF NOT EXISTS cell (
  capture_id INTEGER, host TEXT, cell_name TEXT, make_model TEXT, release_version TEXT,
  metric_history_days TEXT, status TEXT, inventory_ok INTEGER,
  PRIMARY KEY (capture_id, host)
);

CREATE TABLE IF NOT EXISTS celldisk (
  capture_id INTEGER, host TEXT, name TEXT, disk_type TEXT, size TEXT,
  PRIMARY KEY (capture_id, host, name)
);

CREATE TABLE IF NOT EXISTS metric_definition (
  capture_id INTEGER, host TEXT, name TEXT, object_type TEXT, metric_type TEXT, unit TEXT,
  unit_recognized INTEGER, selected INTEGER,
  PRIMARY KEY (capture_id, host, name)
);

CREATE TABLE IF NOT EXISTS block (
  id INTEGER PRIMARY KEY,
  capture_id INTEGER, host TEXT, object_type TEXT, metric TEXT, block_index INTEGER,
  start_raw TEXT, end_raw TEXT, start_epoch INTEGER, end_epoch INTEGER,
  status TEXT, rc TEXT, out_file TEXT, err_file TEXT,
  lines_read INTEGER DEFAULT 0, rows_stored INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_block ON block (capture_id, host, metric, block_index);

CREATE TABLE IF NOT EXISTS sample (
  capture_id INTEGER NOT NULL, host TEXT NOT NULL, metric TEXT NOT NULL, object_name TEXT NOT NULL,
  ts_epoch INTEGER NOT NULL, ts_utc TEXT NOT NULL, ts_raw TEXT NOT NULL,
  value TEXT,                 -- decimal exacto canónico; NULL si no numérico
  raw_value TEXT NOT NULL,
  unit TEXT, unit_status TEXT,  -- OK | MISMATCH | MISSING | UNRECOGNIZED | NO_DEFINITION
  block_id INTEGER, source_line INTEGER,
  PRIMARY KEY (capture_id, host, metric, object_name, ts_epoch)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS issue (
  id INTEGER PRIMARY KEY,
  capture_id INTEGER, severity TEXT, code TEXT, host TEXT, metric TEXT,
  message TEXT, source_file TEXT, source_line INTEGER
);
CREATE INDEX IF NOT EXISTS ix_issue ON issue (capture_id, severity, code);
"""


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    return con
