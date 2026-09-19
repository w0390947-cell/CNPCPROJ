"""Size-bounded append log; caller serializes concurrent writes."""

from pathlib import Path


class RotatingJournal:
    def __init__(self, path: Path, maximum_bytes: int = 10_000_000, backups: int = 5):
        self.path, self.maximum_bytes, self.backups = path, maximum_bytes, backups
        self.handle = path.open("a", encoding="utf-8")

    def append(self, line: str) -> None:
        if self.handle.tell() + len(line.encode("utf-8")) > self.maximum_bytes:
            self.handle.close()
            for index in range(self.backups, 0, -1):
                source = self.path if index == 1 else self.path.with_suffix(f".jsonl.{index - 1}")
                target = self.path.with_suffix(f".jsonl.{index}")
                if source.exists():
                    source.replace(target)
            self.handle = self.path.open("a", encoding="utf-8")
        self.handle.write(line + "\n")
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()
