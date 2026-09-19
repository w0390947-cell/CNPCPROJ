"""Exclusive run directories with a completion manifest published last."""

import json
from hashlib import sha256
from pathlib import Path

from ..contracts import Artifact


class DirectoryResultStore:
    def publish(self, output: Path, artifacts: tuple[Artifact, ...]) -> None:
        paths = [item.path.casefold() for item in artifacts]
        if len(paths) != len(set(paths)) or "completion.json" in paths:
            raise ValueError("DUPLICATE_OR_RESERVED_ARTIFACT_PATH")
        for item in artifacts:
            if (
                Path(item.path).is_absolute()
                or "\\" in item.path
                or ":" in item.path
                or any(part in ("", ".", "..") for part in item.path.split("/"))
            ):
                raise ValueError("INVALID_ARTIFACT_PATH")
        output.mkdir(parents=True, exist_ok=False)
        for item in artifacts:
            path = output / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".partial")
            temporary.write_bytes(item.content)
            temporary.replace(path)
        completion = dict(
            schema_version="field-run-completion-v1",
            files=[
                dict(path=item.path, sha256=sha256(item.content).hexdigest()) for item in artifacts
            ],
        )
        temporary = output / "completion.partial"
        temporary.write_text(json.dumps(completion, indent=2), encoding="utf-8")
        temporary.replace(output / "completion.json")
