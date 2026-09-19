"""Pure consistency and portable relative-path rules."""

from pathlib import PurePosixPath

from .contracts import DatasetError, DatasetManifest

DOCUMENT_ROLES = frozenset({"network", "devices", "point_mapping", "operating_modes", "settings"})


def validate_relative_path(value: str) -> None:
    if (
        not value.strip()
        or PurePosixPath(value).is_absolute()
        or "\\" in value
        or ":" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise DatasetError(f"PATH_NOT_PORTABLE_RELATIVE:{value}")


def validate_manifest(manifest: DatasetManifest, *, require_hashes: bool) -> None:
    ids = [ref.file_id.casefold() for ref in manifest.files]
    paths = [ref.path.casefold() for ref in manifest.files]
    if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
        raise DatasetError("DUPLICATE_FILE_ID_OR_PATH")
    roles = [ref.role for ref in manifest.files]
    if any(roles.count(role) != 1 for role in DOCUMENT_ROLES):
        raise DatasetError("REQUIRE_EXACTLY_ONE_OF_EACH_DOCUMENT_ROLE")
    if "measurements" not in roles:
        raise DatasetError("MEASUREMENT_FILE_MISSING")
    for ref in manifest.files:
        validate_relative_path(ref.path)
        if not ref.version.strip():
            raise DatasetError(f"FILE_VERSION_EMPTY:{ref.file_id}")
        if require_hashes and ref.sha256 is None:
            raise DatasetError(f"UNSEALED_FILE:{ref.file_id}")
    if not manifest.revision.strip() or not manifest.source.strip():
        raise DatasetError("MANIFEST_PROVENANCE_EMPTY")
