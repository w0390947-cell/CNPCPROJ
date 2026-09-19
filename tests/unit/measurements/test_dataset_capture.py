"""Pure capture behavior using an in-memory port, no solver or field files."""

import hashlib
import json
from pathlib import Path

import pytest

from oilfield_energy.modules.measurements.adapters.manifest_json import JsonManifestDecoder
from oilfield_energy.modules.measurements.api import CaptureDataset
from oilfield_energy.modules.measurements.contracts import DatasetError


class MemoryReader:
    def __init__(self):
        self.files = {
            role + ".json": role.encode()
            for role in (
                "network",
                "devices",
                "point_mapping",
                "operating_modes",
                "settings",
                "measurements",
            )
        }
        self.manifest = dict(
            schema_version="field-dataset-v1",
            dataset_id="test",
            revision="1",
            source="fixture",
            synthetic=True,
            files=[
                dict(
                    file_id=path[:-5],
                    role=path[:-5],
                    path=path,
                    version="1",
                    sha256=hashlib.sha256(value).hexdigest(),
                )
                for path, value in self.files.items()
            ],
        )

    def read_manifest(self, path):
        return json.dumps(self.manifest).encode()

    def read_file(self, manifest_path, relative_path):
        return self.files[relative_path]


def capture(reader, refresh=False):
    return CaptureDataset(reader, JsonManifestDecoder()).capture(
        Path("manifest.json"), refresh_hashes=refresh
    )


def test_captured_bytes_are_independent_of_later_source_edits():
    reader = MemoryReader()
    result = capture(reader)
    reader.files["network.json"] = b"changed"
    assert result.files[0].content == b"network"
    with pytest.raises(DatasetError, match="HASH_MISMATCH"):
        capture(reader)


def test_draft_may_omit_hash_only_for_explicit_seal():
    reader = MemoryReader()
    for reference in reader.manifest["files"]:
        reference.pop("sha256")
    with pytest.raises(DatasetError):
        capture(reader)
    assert all(item.sha256 for item in capture(reader, refresh=True).files)


def test_only_explicit_seal_can_refresh_hashes():
    reader = MemoryReader()
    reader.manifest["files"][0]["sha256"] = None
    with pytest.raises(DatasetError, match="UNSEALED_FILE"):
        capture(reader)
    assert capture(reader, refresh=True).files[0].sha256 == hashlib.sha256(b"network").hexdigest()


@pytest.mark.parametrize(
    "path", ["../network.json", "/network.json", "C:/network.json", "a\\b", "a//b", "./a"]
)
def test_reject_nonportable_or_escaping_paths(path):
    reader = MemoryReader()
    reader.manifest["files"][0]["path"] = path
    with pytest.raises(DatasetError, match="PATH_NOT_PORTABLE_RELATIVE"):
        capture(reader)


def test_reject_case_insensitive_duplicate_paths_and_missing_document():
    reader = MemoryReader()
    reader.manifest["files"][1]["path"] = "NETWORK.JSON"
    with pytest.raises(DatasetError, match="DUPLICATE"):
        capture(reader)
    reader.manifest["files"].pop(1)
    with pytest.raises(DatasetError, match="EXACTLY_ONE"):
        capture(reader)


def test_reject_duplicate_keys_unknown_keys_and_wrong_boolean():
    decoder = JsonManifestDecoder()
    with pytest.raises(ValueError, match="DUPLICATE_JSON_KEY"):
        decoder.decode(b'{"revision":"1","revision":"2"}')
    reader = MemoryReader()
    reader.manifest["extra"] = "typo"
    with pytest.raises(ValueError):
        capture(reader)
    del reader.manifest["extra"]
    reader.manifest["synthetic"] = "true"
    with pytest.raises(ValueError):
        capture(reader)


def test_reject_source_change_during_seal():
    class ChangingReader(MemoryReader):
        count = 0

        def read_file(self, manifest_path, relative_path):
            self.count += 1
            value = super().read_file(manifest_path, relative_path)
            return value if self.count <= 6 else value + b"changed"

    with pytest.raises(DatasetError, match="CHANGED_DURING_CAPTURE"):
        capture(ChangingReader(), refresh=True)
