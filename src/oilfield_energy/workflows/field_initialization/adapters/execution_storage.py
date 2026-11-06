"""Atomic execution ledger and immutable messages, sharing the field transaction."""

import sqlite3
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from ..contracts import (
    InitializationEvidence,
    InitializationFailure,
    ObservationCursor,
    SnapshotAssessment,
    SubmitSnapshotRequest,
)
from ..contracts_execution import ExecutionBatch, ExecutionLedger, ExecutionResponse
from ..contracts_recovery import PrepareResynchronizationRequest, ResumeRunRequest
from ..execution_context import ExecutionContext, ExecutionDecision, ExecutionRequest
from ..messages import canonical
from ..recovery_evidence import ResynchronizationEvidence
from ..snapshot_ordering import snapshot_identity
from .cycle_invalidation import invalidate_prepared_cycle
from .session_views import current_inputs, current_target, save_target_head
from .storage_schema import session_connection


def reject_execution_message(db: sqlite3.Connection, grid: str, message: str) -> None:
    if db.execute(
        "SELECT 1 FROM execution_requests WHERE microgrid_id=? AND message_id=?", (grid, message)
    ).fetchone():
        raise InitializationFailure(
            "DUPLICATE_ID_CONFLICT", "Message ID belongs to an execution operation"
        )


def save_ledger(db: sqlite3.Connection, run: str, ledger: ExecutionLedger) -> None:
    db.execute(
        "INSERT INTO execution_sessions VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET ledger=excluded.ledger",
        (run, ledger.model_dump_json()),
    )


def save_batch(db: sqlite3.Connection, run: str, batch: ExecutionBatch) -> None:
    db.execute(
        "INSERT INTO execution_batches VALUES (?, ?, ?) ON CONFLICT(batch_id) DO UPDATE SET batch=excluded.batch",
        (batch.evidence.command_batch_id, run, batch.model_dump_json()),
    )


class SqliteExecution:
    def __init__(self, path: Path) -> None:
        self.path = path

    def inspect(self, request: ExecutionRequest) -> ExecutionContext:
        with session_connection(self.path) as db:
            return self._load(db, request)

    def recorded(self, request: ExecutionRequest) -> ExecutionResponse | None:
        with session_connection(self.path) as db:
            for table in (
                "initialization_requests",
                "planning_updates",
                "pcc_target_requests",
                "snapshot_requests",
                "cycle_requests",
            ):
                if db.execute(
                    f"SELECT 1 FROM {table} WHERE microgrid_id=? AND message_id=?",
                    (request.microgrid_id, request.message_id),
                ).fetchone():
                    raise InitializationFailure(
                        "DUPLICATE_ID_CONFLICT", "Message ID belongs to another field operation"
                    )
            row = db.execute(
                "SELECT fingerprint,response FROM execution_requests WHERE microgrid_id=? AND message_id=?",
                (request.microgrid_id, request.message_id),
            ).fetchone()
            if row:
                if row[0] != sha256(canonical(request)).hexdigest():
                    raise InitializationFailure(
                        "DUPLICATE_ID_CONFLICT", "Message ID has different immutable content"
                    )
                return ExecutionResponse.model_validate_json(row[1])
            return None

    def _load(self, db: sqlite3.Connection, request: ExecutionRequest) -> ExecutionContext:
        grid, run = request.microgrid_id, request.run_id
        current, original = current_inputs(db, run, grid)
        row = (
            db.execute("SELECT ledger FROM execution_sessions WHERE run_id=?", (run,)).fetchone()
            if current
            else None
        )
        ledger = ExecutionLedger.model_validate_json(row[0]) if row else ExecutionLedger()
        batch_id = getattr(request.payload, "command_batch_id", None) or ledger.pending_batch_id
        row = (
            db.execute(
                "SELECT batch FROM execution_batches WHERE batch_id=? AND run_id=?", (batch_id, run)
            ).fetchone()
            if current
            else None
        )
        batch = ExecutionBatch.model_validate_json(row[0]) if row else None
        row = (
            db.execute("SELECT snapshot FROM snapshot_heads WHERE run_id=?", (run,)).fetchone()
            if current
            else None
        )
        cursor = ObservationCursor.model_validate_json(row[0]) if row else None
        snapshot_id = getattr(request.payload, "observed_snapshot_id", None)
        row = (
            db.execute(
                "SELECT request,assessment FROM snapshot_requests WHERE run_id=? AND microgrid_id=? AND snapshot_id=? ORDER BY rowid LIMIT 1",
                (run, grid, snapshot_id),
            ).fetchone()
            if current and snapshot_id
            else None
        )
        snapshot = None
        if row:
            snapshot = SubmitSnapshotRequest.model_validate_json(row[0]).payload
        assessment = SnapshotAssessment.model_validate_json(row[1]) if row and row[1] else None
        recovery_id = getattr(request.payload, "resynchronization_id", None)
        recovery_row = (
            db.execute(
                "SELECT evidence, consumed FROM recovery_attempts WHERE run_id=? AND recovery_id=?",
                (run, recovery_id),
            ).fetchone()
            if current
            else None
        )
        revisions = {
            r[0]
            for r in db.execute(
                "SELECT reserved_revision FROM planning_updates WHERE run_id=? AND reserved_revision IS NOT NULL",
                (run,),
            )
        }
        revisions.update(
            r[0]
            for r in db.execute(
                "SELECT input_revision FROM recovery_attempts WHERE run_id=?", (run,)
            )
        )
        if original:
            revisions.add(original.request.payload.planning_inputs.input_revision)
        unsettled: set[str] = set()
        if isinstance(request, (PrepareResynchronizationRequest, ResumeRunRequest)) and current:
            for item in db.execute("SELECT batch FROM execution_batches WHERE run_id=?", (run,)):
                action = ExecutionBatch.model_validate_json(item[0])
                if action.send_claimed_at is not None and action.status not in (
                    "confirmed",
                    "cancelled",
                    "reconciled",
                ):
                    unsettled.add(action.evidence.command_batch_id)
        return ExecutionContext(
            current=current,
            target=current_target(db, run) if current else None,
            ledger=ledger,
            batch=batch,
            cursor=cursor,
            snapshot=snapshot,
            assessment=assessment,
            recovery=ResynchronizationEvidence.model_validate_json(recovery_row[0])
            if recovery_row
            else None,
            recovery_consumed=bool(recovery_row[1]) if recovery_row else False,
            reserved_revisions=frozenset(revisions),
            unsettled_batch_ids=frozenset(unsettled),
        )

    def apply(
        self, request: ExecutionRequest, decide: Callable[[ExecutionContext], ExecutionDecision]
    ) -> tuple[ExecutionResponse, bool]:
        grid, run, message = request.microgrid_id, request.run_id, request.message_id
        fingerprint = sha256(canonical(request)).hexdigest()
        with session_connection(self.path) as db:
            for table in (
                "initialization_requests",
                "planning_updates",
                "pcc_target_requests",
                "snapshot_requests",
                "cycle_requests",
            ):
                if db.execute(
                    f"SELECT 1 FROM {table} WHERE microgrid_id=? AND message_id=?", (grid, message)
                ).fetchone():
                    raise InitializationFailure(
                        "DUPLICATE_ID_CONFLICT", "Message ID belongs to another field operation"
                    )
            old = db.execute(
                "SELECT fingerprint,response FROM execution_requests WHERE microgrid_id=? AND message_id=?",
                (grid, message),
            ).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise InitializationFailure(
                        "DUPLICATE_ID_CONFLICT", "Message ID has different immutable content"
                    )
                return ExecutionResponse.model_validate_json(old[1]), True
            ctx = self._load(db, request)
            current, ledger = ctx.current, ctx.ledger
            decision = decide(ctx)
            if decision.recovery is not None and not decision.activate_recovery:
                candidate = decision.recovery
                snap = candidate.request.payload.snapshot
                identity = snapshot_identity(snap)
                original = db.execute(
                    "SELECT evidence FROM initialization_sessions WHERE run_id=?", (run,)
                ).fetchone()
                if (
                    original
                    and InitializationEvidence.model_validate_json(
                        original[0]
                    ).request.payload.initial_snapshot.snapshot_id
                    == snap.snapshot_id
                ):
                    raise InitializationFailure(
                        "SNAPSHOT_ID_CONFLICT", "Initialization snapshot identity is reserved"
                    )
                prior = db.execute(
                    "SELECT identity FROM snapshot_requests WHERE run_id=? AND snapshot_id=?",
                    (run, snap.snapshot_id),
                ).fetchone()
                if prior and prior[0] != identity:
                    raise InitializationFailure(
                        "SNAPSHOT_ID_CONFLICT",
                        "Recovery snapshot conflicts with an observed snapshot",
                    )
                try:
                    db.execute(
                        "INSERT INTO recovery_attempts VALUES (?, ?, ?, ?, ?, ?, 0)",
                        (
                            run,
                            candidate.request.payload.resynchronization_id,
                            snap.snapshot_id,
                            identity,
                            candidate.request.payload.planning_inputs.input_revision,
                            candidate.model_dump_json(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise InitializationFailure(
                        "RECOVERY_ID_CONFLICT", "Recovery or snapshot identity is already reserved"
                    ) from exc
            if decision.activate_recovery:
                candidate = decision.recovery
                if candidate is None or ctx.recovery_consumed or ctx.recovery != candidate:
                    raise ValueError("Recovery activation requires its unconsumed stored admission")
                changed = db.execute(
                    "UPDATE recovery_attempts SET consumed=1 WHERE run_id=? AND recovery_id=? AND consumed=0",
                    (run, candidate.request.payload.resynchronization_id),
                ).rowcount
                if changed != 1:
                    raise InitializationFailure(
                        "RECOVERY_ALREADY_CONSUMED", "Recovery was already resumed"
                    )
                db.execute(
                    "INSERT INTO recovery_heads VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET evidence=excluded.evidence",
                    (run, candidate.inputs.model_dump_json()),
                )
                db.execute("DELETE FROM planning_heads WHERE run_id=?", (run,))
                db.execute("DELETE FROM observation_barriers WHERE run_id=?", (run,))
                db.execute(
                    "INSERT INTO snapshot_heads VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET snapshot=excluded.snapshot",
                    (run, candidate.cursor.model_dump_json()),
                )
                db.execute(
                    "INSERT INTO snapshot_valid_heads VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET snapshot=excluded.snapshot",
                    (run, candidate.cursor.snapshot.model_dump_json()),
                )
                if candidate.target is None:
                    db.execute("DELETE FROM pcc_target_heads WHERE run_id=?", (run,))
                else:
                    save_target_head(db, run, candidate.target)
                invalidate_prepared_cycle(db, run, request.operation, message)
            if current:
                save_ledger(db, run, decision.ledger)
                if decision.ledger.stopped_at is not None and ledger.stopped_at is None:
                    invalidate_prepared_cycle(db, run, request.operation, message)
                if decision.batch:
                    save_batch(db, run, decision.batch)
                if decision.ledger.confirmed != ledger.confirmed:
                    db.execute("DELETE FROM prepared_cycle_heads WHERE run_id=?", (run,))
            db.execute(
                "INSERT INTO execution_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    grid,
                    message,
                    run,
                    request.operation,
                    fingerprint,
                    request.model_dump_json(),
                    decision.response.model_dump_json(),
                ),
            )
            return decision.response, False
