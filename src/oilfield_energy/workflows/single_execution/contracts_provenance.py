"""Optional run provenance; old archives must never acquire current metadata."""
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SingleInputRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["single-input-snapshot-v1"] = "single-input-snapshot-v1"
    recorded_at: str
    code_snapshot_phase: Literal["before_run", "before_execution"]
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_scope: Literal["reference_only", "reference_and_execution"]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_snapshot_json: str
    dataset_id: str | None = None
    dataset_revision: str | None = None
    dataset_sha256: str | None = None
    execution_version: str | None = None
    input_basis: str | None = None
    runtime_versions: dict[str, str]

    @model_validator(mode="after")
    def snapshot_digest_matches(self) -> "SingleInputRecord":
        if sha256(self.input_snapshot_json.encode("utf-8")).hexdigest() != self.input_sha256:
            raise ValueError("single input snapshot SHA-256 mismatch")
        return self
