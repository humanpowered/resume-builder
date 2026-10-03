"""
Where a person's record and their interview progress are kept.

The engine never touches files directly; it asks a store. Today that is a
Markdown file on disk, which is what a person running the command line wants.
A hosted product swaps in a database-backed store with the same four methods,
one per user, and nothing else in the engine changes.
"""
import json
from pathlib import Path

from . import record as mr


class FileStore:
    """record.md plus small JSON sidecars beside it (.record.<name>.json)."""

    def __init__(self, path):
        self.path = Path(path)

    def _side(self, name: str) -> Path:
        return self.path.with_name(f".{self.path.stem}.{name}.json")

    def exists(self) -> bool:
        return self.path.exists()

    def load_record(self) -> mr.Record:
        if self.path.exists():
            return mr.parse(self.path.read_text(encoding="utf-8"))
        return mr.Record(header=["# Master record"])

    def save_record(self, rec: mr.Record) -> None:
        """Through a temporary file, so an interruption mid-write cannot leave
        a half-written record where a complete one used to be."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".md.tmp")
        tmp.write_text(mr.render(rec), encoding="utf-8")
        tmp.replace(self.path)

    def load_state(self, name: str):
        p = self._side(name)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None         # derived data; a corrupt sidecar means start fresh

    def save_state(self, name: str, state) -> None:
        self._side(name).write_text(json.dumps(state, indent=1), encoding="utf-8")

    def clear_state(self, name: str) -> None:
        p = self._side(name)
        if p.exists():
            p.unlink()


class MemoryStore:
    """Everything in memory. For tests, and the shape a database store copies.
    Records are kept as rendered text so every load is a real parse, the same
    as from disk."""

    def __init__(self, rec: mr.Record | None = None):
        self.text = mr.render(rec) if rec else None
        self.states = {}

    def exists(self) -> bool:
        return self.text is not None

    def load_record(self) -> mr.Record:
        return mr.parse(self.text) if self.text else mr.Record(header=["# Master record"])

    def save_record(self, rec: mr.Record) -> None:
        self.text = mr.render(rec)

    def load_state(self, name: str):
        s = self.states.get(name)
        return json.loads(s) if s else None

    def save_state(self, name: str, state) -> None:
        self.states[name] = json.dumps(state)

    def clear_state(self, name: str) -> None:
        self.states.pop(name, None)
