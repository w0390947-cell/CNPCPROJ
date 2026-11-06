"""Serialized preparation, idempotent history and a single current shadow batch."""

from collections.abc import Callable
from pathlib import Path

from ..contracts import (
    ControlCycleEvidence,
    InitializationEvidence,
    InitializationFailure,
    ObservationCursor,
    PccTargetEvidence,
    PrepareControlCycleRequest,
    PrepareControlCycleResponse,
)
from ..contracts_execution import ExecutionBatch, ExecutionLedger
from .execution_storage import reject_execution_message, save_batch, save_ledger
from .session_views import current_inputs, current_target
from .storage_schema import session_connection


class SqliteControlCycles:
    def __init__(self, path: Path) -> None:
        self.path = path

    def prepare_once(
        self,
        request: PrepareControlCycleRequest,
        fingerprint: str,
        identity: str,
        prepare: Callable[
            [
                InitializationEvidence | None,
                PccTargetEvidence | None,
                ObservationCursor | None,
                str | None,
                PrepareControlCycleResponse | None,
            ],
            tuple[PrepareControlCycleResponse, ControlCycleEvidence | None],
        ],
    ) -> PrepareControlCycleResponse:
        grid, message, run = request.microgrid_id, request.message_id, request.run_id
        with session_connection(self.path) as db:
            reject_execution_message(db, grid, message)
            if db.execute(
                "SELECT 1 FROM initialization_requests WHERE microgrid_id=? AND message_id=? "
                "UNION ALL SELECT 1 FROM planning_updates WHERE microgrid_id=? AND message_id=? "
                "UNION ALL SELECT 1 FROM pcc_target_requests WHERE microgrid_id=? AND message_id=? "
                "UNION ALL SELECT 1 FROM snapshot_requests WHERE microgrid_id=? AND message_id=?",
                (grid, message) * 4,
            ).fetchone():
                raise InitializationFailure(
                    "DUPLICATE_ID_CONFLICT",
                    "Message ID belongs to another field operation",
                )
            row = db.execute(
                "SELECT fingerprint, response FROM cycle_requests WHERE microgrid_id=? AND message_id=?",
                (grid, message),
            ).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise InitializationFailure(
                        "DUPLICATE_ID_CONFLICT", "Message ID has different content"
                    )
                return PrepareControlCycleResponse.model_validate_json(row[1])
            row = db.execute(
                "SELECT identity, response FROM cycle_requests WHERE run_id=? AND microgrid_id=? AND cycle_id=? ORDER BY rowid LIMIT 1",
                (run, grid, request.payload.cycle_id),
            ).fetchone()
            recorded = None
            if row:
                if row[0] != identity:
                    raise InitializationFailure(
                        "CYCLE_ID_CONFLICT", "Cycle ID has different immutable content"
                    )
                recorded = PrepareControlCycleResponse.model_validate_json(row[1])
            current, _ = current_inputs(db, run, grid)
            cursor = (
                db.execute("SELECT snapshot FROM snapshot_heads WHERE run_id=?", (run,)).fetchone()
                if current
                else None
            )
            pending = (
                db.execute(
                    "SELECT batch_id FROM prepared_cycle_heads WHERE run_id=?", (run,)
                ).fetchone()
                if current
                else None
            )
            response, evidence = prepare(
                current,
                current_target(db, run) if current else None,
                ObservationCursor.model_validate_json(cursor[0]) if cursor else None,
                pending[0] if pending else None,
                recorded,
            )
            if recorded is None and (response.payload.outcome == "accepted") != (
                evidence is not None
            ):
                raise ValueError("New preparation acceptance requires immutable evidence")
            if evidence is not None and (
                recorded is not None
                or pending is not None
                or current is None
                or evidence.request != request
                or evidence.domain.context_id != current.domain.context_id
                or evidence.command_batch_id != response.payload.data.command_batch_id
            ):
                raise ValueError("Shadow batch does not match its current preparation")
            db.execute(
                "INSERT INTO cycle_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    grid,
                    message,
                    run,
                    request.payload.cycle_id,
                    fingerprint,
                    identity,
                    request.model_dump_json(),
                    response.model_dump_json(),
                    evidence.model_dump_json() if evidence else None,
                ),
            )
            if evidence:
                assert current is not None
                db.execute(
                    "INSERT INTO prepared_cycle_heads VALUES (?, ?)",
                    (run, evidence.command_batch_id),
                )
                if current.request.payload.mode == "closed_loop":
                    row = db.execute(
                        "SELECT ledger FROM execution_sessions WHERE run_id=?", (run,)
                    ).fetchone()
                    ledger = (
                        ExecutionLedger.model_validate_json(row[0]) if row else ExecutionLedger()
                    )
                    data = response.payload.data
                    assert (
                        data.dispatch_deadline_at is not None and data.feedback_due_at is not None
                    )
                    batch = ExecutionBatch(
                        evidence=evidence,
                        commands=data.commands,
                        dispatch_deadline_at=data.dispatch_deadline_at,
                        feedback_due_at=data.feedback_due_at,
                    )
                    save_batch(db, run, batch)
                    save_ledger(
                        db,
                        run,
                        ledger.model_copy(
                            update={
                                "run_state": "awaiting_feedback",
                                "pending_batch_id": evidence.command_batch_id,
                            }
                        ),
                    )
            return response
