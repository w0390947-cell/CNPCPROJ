"""Atomic idempotent initialization records, independent of process lifetime."""

from collections.abc import Callable
from pathlib import Path

from ..contracts import (
    ArtifactReference,
    InitializationEvidence,
    InitializationFailure,
    InitializeResponse,
)
from .execution_storage import reject_execution_message
from .storage_schema import session_connection


class LocalArtifacts:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, reference: ArtifactReference) -> bytes:
        # User labels are never treated as paths; digest is validated by the contract.
        path = self.root / f"{reference.sha256}.json"
        try:
            with path.open("rb") as stream:
                content = stream.read(8 * 1024 * 1024 + 1)
        except OSError as exc:
            raise InitializationFailure(
                "ARTIFACT_UNAVAILABLE",
                "Referenced artifact is unavailable in the configured content store",
            ) from exc
        if len(content) > 8 * 1024 * 1024:
            raise InitializationFailure("ARTIFACT_INVALID", "Artifact exceeds 8 MiB")
        return content


class SqliteSessions:
    def __init__(self, path: Path) -> None:
        self.path = path

    def execute_once(
        self,
        microgrid_id: str,
        message_id: str,
        fingerprint: str,
        prepare: Callable[[], tuple[InitializeResponse, InitializationEvidence | None]],
    ) -> InitializeResponse:
        with session_connection(self.path) as connection:
            reject_execution_message(connection, microgrid_id, message_id)
            if connection.execute(
                "SELECT 1 FROM cycle_requests WHERE microgrid_id=? AND message_id=?",
                (microgrid_id, message_id),
            ).fetchone():
                raise InitializationFailure(
                    "DUPLICATE_ID_CONFLICT", "Message ID belongs to PrepareControlCycle"
                )
            if connection.execute(
                "SELECT 1 FROM planning_updates WHERE microgrid_id=? AND message_id=? "
                "UNION ALL SELECT 1 FROM pcc_target_requests WHERE microgrid_id=? AND message_id=? "
                "UNION ALL SELECT 1 FROM snapshot_requests WHERE microgrid_id=? AND message_id=?",
                (
                    microgrid_id,
                    message_id,
                    microgrid_id,
                    message_id,
                    microgrid_id,
                    message_id,
                ),
            ).fetchone():
                raise InitializationFailure(
                    "DUPLICATE_ID_CONFLICT",
                    "Message ID belongs to another field operation",
                )
            row = connection.execute(
                "SELECT fingerprint, response FROM initialization_requests WHERE microgrid_id=? AND message_id=?",
                (microgrid_id, message_id),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise InitializationFailure(
                        "DUPLICATE_ID_CONFLICT",
                        "The message ID was already used with different content",
                    )
                return InitializeResponse.model_validate_json(row[1])
            if connection.execute(
                "SELECT 1 FROM initialization_sessions WHERE microgrid_id=?",
                (microgrid_id,),
            ).fetchone():
                raise InitializationFailure(
                    "ACTIVE_RUN_EXISTS",
                    "This microgrid already has an initialized session; restart/stop interfaces are not implemented",
                )
            response, evidence = prepare()
            if evidence is not None:
                connection.execute(
                    "INSERT INTO initialization_sessions VALUES (?, ?, ?, ?)",
                    (
                        response.run_id,
                        microgrid_id,
                        evidence.domain.context_id,
                        evidence.model_dump_json(),
                    ),
                )
            connection.execute(
                "INSERT INTO initialization_requests VALUES (?, ?, ?, ?)",
                (microgrid_id, message_id, fingerprint, response.model_dump_json()),
            )
            return response
