"""Atomic external target requests, identities, evidence and active pointer."""

from collections.abc import Callable
from pathlib import Path

from ..contracts import (
    InitializationEvidence,
    InitializationFailure,
    PccTargetEvidence,
    RequestPccTargetRequest,
    RequestPccTargetResponse,
)
from .cycle_invalidation import invalidate_prepared_cycle
from .execution_storage import reject_execution_message
from .session_views import current_inputs, current_target, save_target_head
from .storage_schema import session_connection


class SqlitePccTargets:
    def __init__(self, path: Path) -> None:
        self.path = path

    def request_once(
        self,
        request: RequestPccTargetRequest,
        fingerprint: str,
        identity: str,
        prepare: Callable[
            [InitializationEvidence | None, PccTargetEvidence | None, str | None],
            tuple[RequestPccTargetResponse, PccTargetEvidence | None],
        ],
    ) -> RequestPccTargetResponse:
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
                "UNION ALL SELECT 1 FROM snapshot_requests WHERE microgrid_id=? AND message_id=?",
                (grid, message, grid, message, grid, message),
            ).fetchone():
                raise InitializationFailure(
                    "DUPLICATE_ID_CONFLICT", "Message ID belongs to another operation"
                )
            row = db.execute(
                "SELECT fingerprint, response FROM pcc_target_requests WHERE microgrid_id=? AND message_id=?",
                (grid, message),
            ).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise InitializationFailure(
                        "DUPLICATE_ID_CONFLICT", "Message ID has different content"
                    )
                return RequestPccTargetResponse.model_validate_json(row[1])
            current, _ = current_inputs(db, run, grid)
            previous = current_target(db, run) if current else None
            row = db.execute(
                "SELECT identity FROM pcc_target_requests WHERE run_id=? AND microgrid_id=? AND target_id=? ORDER BY rowid LIMIT 1",
                (run, grid, request.payload.target_id),
            ).fetchone()
            response, evidence = prepare(current, previous, row[0] if row else None)
            if (response.payload.outcome == "accepted") != (evidence is not None):
                raise ValueError("Target acceptance requires evidence")
            if evidence is not None and (
                current is None
                or evidence.context_id != current.domain.context_id
                or evidence.request != request
                or response.payload.data.guaranteed_until != evidence.guaranteed_until
            ):
                raise ValueError("Target evidence does not match the current request and domain")
            db.execute(
                "INSERT INTO pcc_target_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    grid,
                    message,
                    run,
                    request.payload.target_id,
                    fingerprint,
                    identity,
                    request.model_dump_json(),
                    response.model_dump_json(),
                    evidence.model_dump_json() if evidence else None,
                ),
            )
            if evidence is not None:
                invalidate_prepared_cycle(db, run, request.operation, message)
                save_target_head(db, run, evidence)
            return response
