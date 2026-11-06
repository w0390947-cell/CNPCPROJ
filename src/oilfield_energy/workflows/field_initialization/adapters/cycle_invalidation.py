"""Invalidate only the current shadow pointer; preserve immutable batch history."""

import sqlite3


def invalidate_prepared_cycle(
    db: sqlite3.Connection, run: str, operation: str, message: str
) -> None:
    row = db.execute(
        "SELECT batch_id FROM prepared_cycle_heads WHERE run_id=?", (run,)
    ).fetchone()
    if row:
        db.execute(
            "INSERT INTO cycle_invalidations VALUES (?, ?, ?)",
            (row[0], operation, message),
        )
        db.execute("DELETE FROM prepared_cycle_heads WHERE run_id=?", (run,))
