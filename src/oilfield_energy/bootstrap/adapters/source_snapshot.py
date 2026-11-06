"""Stable digest of the backend Python sources used by a calculation."""
from hashlib import sha256
from pathlib import Path


def source_digest() -> str:
    source = Path(__file__).resolve().parents[2]
    digest = sha256()
    for path in sorted(source.rglob("*.py")):
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
