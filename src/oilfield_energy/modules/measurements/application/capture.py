"""Capture all pinned bytes before validation or solving."""

from hashlib import sha256
from pathlib import Path

from ..contracts import CapturedDataset, CapturedFile, DatasetError
from ..domain import validate_manifest
from .ports import DatasetReader, ManifestDecoder


class CaptureDataset:
    def __init__(self, reader: DatasetReader, decoder: ManifestDecoder) -> None:
        self._reader = reader
        self._decoder = decoder

    def capture(self, path: Path, *, refresh_hashes: bool = False) -> CapturedDataset:
        raw = self._reader.read_manifest(path)
        manifest = self._decoder.decode(raw)
        validate_manifest(manifest, require_hashes=not refresh_hashes)
        files: list[CapturedFile] = []
        for ref in manifest.files:
            content = self._reader.read_file(path, ref.path)
            digest = sha256(content).hexdigest()
            if not refresh_hashes and digest != ref.sha256:
                raise DatasetError(f"HASH_MISMATCH:{ref.file_id}")
            files.append(CapturedFile(ref, content, digest))
        if refresh_hashes:
            for item in files:
                if self._reader.read_file(path, item.reference.path) != item.content:
                    raise DatasetError(f"FILE_CHANGED_DURING_CAPTURE:{item.reference.file_id}")
        if self._reader.read_manifest(path) != raw:
            raise DatasetError("MANIFEST_CHANGED_DURING_CAPTURE")
        return CapturedDataset(manifest, raw, tuple(files))
