"""Immutable dataset contracts; captured payload bytes require engine validation."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

FileRole = Literal[
    "network",
    "devices",
    "point_mapping",
    "operating_modes",
    "settings",
    "measurements",
    "attachment",
]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
Nonempty = Annotated[str, Field(min_length=1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class FileReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    file_id: Identifier
    role: FileRole
    path: Nonempty
    version: Nonempty
    sha256: Digest | None = None


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["field-dataset-v1"]
    dataset_id: Identifier
    revision: Nonempty
    source: Nonempty
    synthetic: bool
    files: tuple[FileReference, ...]


@dataclass(frozen=True)
class CapturedFile:
    reference: FileReference
    content: bytes
    sha256: str


@dataclass(frozen=True)
class CapturedDataset:
    manifest: DatasetManifest
    original_manifest: bytes
    files: tuple[CapturedFile, ...]


class DatasetError(ValueError):
    """Invalid or incomplete dataset; never replaced by synthetic input."""
