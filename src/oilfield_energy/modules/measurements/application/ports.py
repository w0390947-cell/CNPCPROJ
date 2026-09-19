"""Input-side ports owned by dataset capture."""

from pathlib import Path
from typing import Protocol

from ..contracts import DatasetManifest


class DatasetReader(Protocol):
    def read_manifest(self, path: Path) -> bytes: ...

    def read_file(self, manifest_path: Path, relative_path: str) -> bytes: ...


class ManifestDecoder(Protocol):
    def decode(self, content: bytes) -> DatasetManifest: ...
