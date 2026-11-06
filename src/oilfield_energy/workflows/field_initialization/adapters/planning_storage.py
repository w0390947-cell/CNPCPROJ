"""Atomic planning revisions; initialization history remains immutable."""

from collections.abc import Callable
from pathlib import Path

from ..contracts import (
    InitializationEvidence,
    InitializationFailure,
    PccTargetEvidence,
    UpdatePlanningRequest,
    UpdatePlanningResponse,
)
from .cycle_invalidation import invalidate_prepared_cycle
from .execution_storage import reject_execution_message
from .session_views import current_inputs, current_target, save_target_head
from .storage_schema import session_connection


class SqlitePlanningUpdates:
    def __init__(self, path: Path) -> None:
        self.path = path

    def update_once(
        self,
        request: UpdatePlanningRequest,
        fingerprint: str,
        prepare: Callable[
            [InitializationEvidence | None, frozenset[str], PccTargetEvidence | None],
            tuple[
                UpdatePlanningResponse,
                InitializationEvidence | None,
                PccTargetEvidence | None,
            ],
        ],
    ) -> UpdatePlanningResponse:
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
                "UNION ALL SELECT 1 FROM pcc_target_requests WHERE microgrid_id=? AND message_id=? "
                "UNION ALL SELECT 1 FROM snapshot_requests WHERE microgrid_id=? AND message_id=?",
                (grid, message, grid, message, grid, message),
            ).fetchone():
                raise InitializationFailure(
                    "DUPLICATE_ID_CONFLICT",
                    "Message ID belongs to another field operation",
                )
            row = db.execute(
                "SELECT fingerprint, response FROM planning_updates WHERE microgrid_id=? AND message_id=?",
                (grid, message),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise InitializationFailure(
                        "DUPLICATE_ID_CONFLICT", "Message ID has different content"
                    )
                return UpdatePlanningResponse.model_validate_json(row[1])
            current, original = current_inputs(db, run, grid)
            previous = current_target(db, run) if current else None
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
            response, evidence, target_evidence = prepare(current, frozenset(revisions), previous)
            activated = response.payload.data.activation_status == "activated"
            if activated != (evidence is not None):
                raise ValueError("Only activation may commit planning evidence")
            if (
                evidence is not None
                and response.payload.data.new_context_id != evidence.domain.context_id
            ):
                raise ValueError("Response and evidence contexts differ")
            revision = (
                request.payload.input_revision if response.payload.outcome == "accepted" else None
            )
            db.execute(
                "INSERT INTO planning_updates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    grid,
                    message,
                    run,
                    fingerprint,
                    request.model_dump_json(),
                    response.model_dump_json(),
                    revision,
                    evidence.model_dump_json() if evidence is not None else None,
                ),
            )
            if target_evidence is not None:
                if (
                    not activated
                    or evidence is None
                    or target_evidence.context_id != evidence.domain.context_id
                ):
                    raise ValueError("Target renewal must match the atomic planning activation")
                db.execute(
                    "INSERT INTO pcc_target_revalidations VALUES (?, ?, ?)",
                    (run, message, target_evidence.model_dump_json()),
                )
                save_target_head(db, run, target_evidence)
            if activated:
                invalidate_prepared_cycle(db, run, request.operation, message)
                if previous is not None and target_evidence is None:
                    raise ValueError("Planning activation cannot drop the current external target")
                db.execute(
                    "INSERT INTO planning_heads VALUES (?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET message_id=excluded.message_id, microgrid_id=excluded.microgrid_id",
                    (run, message, grid),
                )
            return response
