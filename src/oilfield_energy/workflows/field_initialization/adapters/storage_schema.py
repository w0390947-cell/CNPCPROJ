"""Backward-compatible additive schema and the single field-session transaction."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path


@contextmanager
def session_connection(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for sql in (
            "CREATE TABLE IF NOT EXISTS recovery_attempts (run_id TEXT NOT NULL, recovery_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, snapshot_identity TEXT NOT NULL, input_revision TEXT NOT NULL, evidence TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id,recovery_id), UNIQUE(run_id,snapshot_id))",
            "CREATE TABLE IF NOT EXISTS recovery_heads (run_id TEXT PRIMARY KEY, evidence TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS execution_sessions (run_id TEXT PRIMARY KEY, ledger TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS execution_batches (batch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, batch TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS execution_requests (microgrid_id TEXT NOT NULL, message_id TEXT NOT NULL, run_id TEXT NOT NULL, operation TEXT NOT NULL, fingerprint TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(microgrid_id, message_id))",
            "CREATE TABLE IF NOT EXISTS cycle_requests (microgrid_id TEXT NOT NULL, message_id TEXT NOT NULL, run_id TEXT NOT NULL, cycle_id TEXT NOT NULL, fingerprint TEXT NOT NULL, identity TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, evidence TEXT, PRIMARY KEY (microgrid_id, message_id))",
            "CREATE INDEX IF NOT EXISTS cycle_identity ON cycle_requests(run_id, microgrid_id, cycle_id)",
            "CREATE TABLE IF NOT EXISTS prepared_cycle_heads (run_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE)",
            "CREATE TABLE IF NOT EXISTS cycle_invalidations (batch_id TEXT PRIMARY KEY, operation TEXT NOT NULL, message_id TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS initialization_requests (microgrid_id TEXT NOT NULL, message_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY (microgrid_id, message_id))",
            "CREATE TABLE IF NOT EXISTS initialization_sessions (run_id TEXT PRIMARY KEY, microgrid_id TEXT NOT NULL UNIQUE, context_id TEXT NOT NULL, evidence TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS planning_updates (microgrid_id TEXT NOT NULL, message_id TEXT NOT NULL, run_id TEXT NOT NULL, fingerprint TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, reserved_revision TEXT, evidence TEXT, PRIMARY KEY (microgrid_id, message_id), UNIQUE (run_id, reserved_revision))",
            "CREATE TABLE IF NOT EXISTS planning_heads (run_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, microgrid_id TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pcc_target_requests (microgrid_id TEXT NOT NULL, message_id TEXT NOT NULL, run_id TEXT NOT NULL, target_id TEXT NOT NULL, fingerprint TEXT NOT NULL, identity TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, evidence TEXT, PRIMARY KEY (microgrid_id, message_id))",
            "CREATE INDEX IF NOT EXISTS pcc_target_identity ON pcc_target_requests(run_id, microgrid_id, target_id)",
            "CREATE TABLE IF NOT EXISTS pcc_target_heads (run_id TEXT PRIMARY KEY, evidence TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pcc_target_revalidations (run_id TEXT NOT NULL, planning_message_id TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY (run_id, planning_message_id))",
            "CREATE TABLE IF NOT EXISTS snapshot_requests (microgrid_id TEXT NOT NULL, message_id TEXT NOT NULL, run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, fingerprint TEXT NOT NULL, identity TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, assessment TEXT, PRIMARY KEY (microgrid_id, message_id))",
            "CREATE INDEX IF NOT EXISTS snapshot_identity ON snapshot_requests(run_id, microgrid_id, snapshot_id)",
            "CREATE TABLE IF NOT EXISTS snapshot_heads (run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS snapshot_valid_heads (run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS observation_barriers (run_id TEXT PRIMARY KEY, barrier TEXT NOT NULL)",
        ):
            connection.execute(sql)
        yield connection
