"""Atomic observation history and guards; never write confirmed/planning heads."""

from collections.abc import Callable
from pathlib import Path

from ..contracts import (
    InitializationEvidence,
    InitializationFailure,
    ObservationCursor,
    PccTargetEvidence,
    SnapshotDecision,
    SubmitSnapshotRequest,
    SubmitSnapshotResponse,
)
from .cycle_invalidation import invalidate_prepared_cycle
from .execution_storage import reject_execution_message
from .session_views import current_inputs, current_target
from .storage_schema import session_connection


class SqliteSnapshots:
    def __init__(self, path: Path) -> None:
        self.path = path

    def submit_once(
        self,
        request: SubmitSnapshotRequest,
        fingerprint: str,
        identity: str,
        prepare: Callable[
            [
                InitializationEvidence | None,
                PccTargetEvidence | None,
                ObservationCursor | None,
                SubmitSnapshotResponse | None,
            ],
            SnapshotDecision,
        ],
    ) -> SubmitSnapshotResponse:
        grid, message, run = request.microgrid_id, request.message_id, request.run_id
        with session_connection(self.path) as db:
            reject_execution_message(db, grid, message)
            if db.execute(
                "SELECT 1 FROM cycle_requests WHERE microgrid_id=? AND message_id=?",
                (grid, message),
            ).fetchone():
                raise InitializationFailure(
                    "DUPLICATE_ID_CONFLICT", "Message ID belongs to PrepareControlCycle"
                )
            if db.execute(
                "SELECT 1 FROM initialization_requests WHERE microgrid_id=? AND message_id=? "
                "UNION ALL SELECT 1 FROM planning_updates WHERE microgrid_id=? AND message_id=? "
                "UNION ALL SELECT 1 FROM pcc_target_requests WHERE microgrid_id=? AND message_id=?",
                (grid, message, grid, message, grid, message),
            ).fetchone():
                raise InitializationFailure(
                    "DUPLICATE_ID_CONFLICT",
                    "Message ID belongs to another field operation",
                )
            row = db.execute(
                "SELECT fingerprint, response FROM snapshot_requests WHERE microgrid_id=? AND message_id=?",
                (grid, message),
            ).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise InitializationFailure(
                        "DUPLICATE_ID_CONFLICT", "Message ID has different content"
                    )
                return SubmitSnapshotResponse.model_validate_json(row[1])
            if db.execute(
                "SELECT 1 FROM recovery_attempts WHERE run_id=? AND snapshot_id=?",
                (run, request.payload.snapshot_id),
            ).fetchone():
                raise InitializationFailure(
                    "SNAPSHOT_ID_CONFLICT",
                    "Snapshot identity belongs to an explicit recovery attempt",
                )
            current, original = current_inputs(db, run, grid)
            if (
                original
                and request.payload.snapshot_id
                == original.request.payload.initial_snapshot.snapshot_id
            ):
                raise InitializationFailure(
                    "SNAPSHOT_ID_CONFLICT", "Snapshot ID is reserved by initialization"
                )
            row = db.execute(
                "SELECT identity, response FROM snapshot_requests WHERE run_id=? AND microgrid_id=? AND snapshot_id=? ORDER BY rowid LIMIT 1",
                (run, grid, request.payload.snapshot_id),
            ).fetchone()
            recorded = None
            if row:
                if row[0] != identity:
                    raise InitializationFailure(
                        "SNAPSHOT_ID_CONFLICT",
                        "Snapshot ID has different immutable content",
                    )
                recorded = SubmitSnapshotResponse.model_validate_json(row[1])
            latest = (
                db.execute("SELECT snapshot FROM snapshot_heads WHERE run_id=?", (run,)).fetchone()
                if current
                else None
            )
            decision = prepare(
                current,
                current_target(db, run) if current else None,
                ObservationCursor.model_validate_json(latest[0]) if latest else None,
                recorded,
            )
            response = decision.response
            if decision.advance_observation and (current is None or recorded is not None):
                raise ValueError(
                    "Observation promotion requires a known session and a new snapshot"
                )
            db.execute(
                "INSERT INTO snapshot_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    grid,
                    message,
                    run,
                    request.payload.snapshot_id,
                    fingerprint,
                    identity,
                    request.model_dump_json(),
                    response.model_dump_json(),
                    decision.assessment.model_dump_json() if decision.assessment else None,
                ),
            )
            if decision.advance_observation:
                invalidate_prepared_cycle(db, run, request.operation, message)
                if decision.cursor is None or decision.cursor.snapshot != request.payload:
                    raise ValueError("Observation head requires matching cursor evidence")
                db.execute(
                    "INSERT INTO snapshot_heads VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET snapshot=excluded.snapshot",
                    (run, decision.cursor.model_dump_json()),
                )
                data = response.payload.data
                if data.input_status == "valid" and data.state_status == "consistent":
                    db.execute(
                        "INSERT INTO snapshot_valid_heads VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET snapshot=excluded.snapshot",
                        (run, request.payload.model_dump_json()),
                    )
            if decision.barrier is not None:
                if not decision.advance_observation:
                    raise ValueError("A historical snapshot cannot change the observation barrier")
                db.execute(
                    "INSERT INTO observation_barriers VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET barrier=excluded.barrier",
                    (run, decision.barrier.model_dump_json()),
                )
            return response
