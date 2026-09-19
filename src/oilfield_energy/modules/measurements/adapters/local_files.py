"""Read-only files restricted to the manifest directory tree."""

from pathlib import Path

from ..contracts import DatasetError
from ..domain import validate_relative_path


class LocalDatasetReader:
    def read_manifest(self, path: Path) -> bytes:
        return path.read_bytes()

    def read_file(self, manifest_path: Path, relative_path: str) -> bytes:
        validate_relative_path(relative_path)
        root = manifest_path.resolve().parent
        target = (root / relative_path).resolve()
        if not target.is_relative_to(root):
            raise DatasetError(f"FILE_OUTSIDE_DATASET:{relative_path}")
        return target.read_bytes()
