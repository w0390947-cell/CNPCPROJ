"""Internal read projections inside the field workflow's owned transaction."""

import sqlite3

from ..contracts import InitializationEvidence, ObservationBarrier, PccTargetEvidence
from ..contracts_execution import ExecutionBatch, ExecutionLedger


def current_inputs(
    db: sqlite3.Connection,
    run: str,
    grid: str,
) -> tuple[InitializationEvidence | None, InitializationEvidence | None]:
    row = db.execute(
        "SELECT COALESCE(u.evidence, r.evidence, s.evidence), s.evidence "
        "FROM initialization_sessions s LEFT JOIN planning_heads h ON h.run_id=s.run_id "
        "LEFT JOIN recovery_heads r ON r.run_id=s.run_id "
        "LEFT JOIN planning_updates u ON u.microgrid_id=h.microgrid_id AND u.message_id=h.message_id "
        "WHERE s.run_id=? AND s.microgrid_id=?",
        (run, grid),
    ).fetchone()
    if not row:
        return None, None
    current = InitializationEvidence.model_validate_json(row[0])
    execution = db.execute(
        "SELECT ledger FROM execution_sessions WHERE run_id=?", (run,)
    ).fetchone()
    if execution:
        ledger = ExecutionLedger.model_validate_json(execution[0])
        task = current.request.payload.planning_inputs.energy_task
        current = current.model_copy(
            update={
                "confirmed_boundary": ledger.confirmed,
                "field_run_state": ledger.run_state,
                "completed_energy_task": current.completed_energy_task
                or bool(
                    task
                    and ledger.confirmed
                    and ledger.energy_task_outcome == "met"
                    and ledger.confirmed.snapshot.snapshot_at >= task.deadline_at
                ),
            }
        )
        pending = db.execute(
            "SELECT batch FROM execution_batches WHERE batch_id=? AND run_id=?",
            (ledger.pending_batch_id, run),
        ).fetchone()
        if pending:
            batch = ExecutionBatch.model_validate_json(pending[0])
            current = current.model_copy(
                update={
                    "pending_cycle": batch.evidence,
                    "pending_sent": batch.send_claimed_at is not None,
                }
            )
    guard = db.execute("SELECT barrier FROM observation_barriers WHERE run_id=?", (run,)).fetchone()
    if guard:
        current = current.model_copy(
            update={"observation_barrier": ObservationBarrier.model_validate_json(guard[0])}
        )
    return current, InitializationEvidence.model_validate_json(row[1])


def current_target(db: sqlite3.Connection, run: str) -> PccTargetEvidence | None:
    row = db.execute("SELECT evidence FROM pcc_target_heads WHERE run_id=?", (run,)).fetchone()
    return PccTargetEvidence.model_validate_json(row[0]) if row else None


def save_target_head(db: sqlite3.Connection, run: str, target: PccTargetEvidence) -> None:
    db.execute(
        "INSERT INTO pcc_target_heads VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET evidence=excluded.evidence",
        (run, target.model_dump_json()),
    )
