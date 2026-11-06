"""Durable, bounded reads of completed simulation display events."""
from pathlib import Path
from uuid import uuid4
import json


class LiveJournal:
    def __init__(self, session: Path):
        from .job_manager import atomic_write_json
        self.stream = uuid4().hex
        self.directory = session / "live" / self.stream
        self.directory.mkdir(parents=True)
        self.sequence = 0
        atomic_write_json(session / "live.json", {"stream": self.stream})

    def publish(self, event: dict) -> None:
        from .job_manager import atomic_write_json
        sequence = self.sequence + 1
        atomic_write_json(self.directory / f"{sequence:08d}.json", {**event, "sequence": sequence})
        self.sequence = sequence


def read_live(session: Path, after: int = 0, stream: str | None = None) -> dict:
    manifest = session / "live.json"
    if not manifest.exists():
        return {"stream": None, "events": [], "cursor": 0, "more": False}
    current = json.loads(manifest.read_text(encoding="utf-8"))["stream"]
    if len(current) != 32 or any(c not in "0123456789abcdef" for c in current):
        raise ValueError("invalid live journal identity")
    cursor = after if stream == current else 0
    directory = session / "live" / current
    events = []
    for sequence in range(cursor + 1, cursor + 33):
        path = directory / f"{sequence:08d}.json"
        if not path.exists():
            break
        events.append(json.loads(path.read_text(encoding="utf-8")))
        cursor = sequence
    return {"stream": current, "events": events, "cursor": cursor,
            "more": (directory / f"{cursor + 1:08d}.json").exists()}
