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


class SqlStore:
    """
    One user's record and state in a SQL database.

    Every query is scoped by user_id, and a store object is built per request
    for the signed-in user only, so one person's record can never be read
    through another's store. SQLite for development; the SQL is plain enough
    to run on Postgres unchanged.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS records (
        user_id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS record_history (
        user_id TEXT NOT NULL, body TEXT NOT NULL, saved_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS states (
        user_id TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY (user_id, name));
    """
    HISTORY = 50          # versions kept per user; a record is someone's career

    def __init__(self, conn, user_id: str):
        if not user_id:
            raise ValueError("a store needs a user")
        self.conn = conn
        self.user = user_id

    @classmethod
    def init(cls, conn) -> None:
        conn.executescript(cls.SCHEMA)
        conn.commit()

    def exists(self) -> bool:
        return self.conn.execute("SELECT 1 FROM records WHERE user_id = ?",
                                 (self.user,)).fetchone() is not None

    def load_record(self) -> mr.Record:
        row = self.conn.execute("SELECT body FROM records WHERE user_id = ?",
                                (self.user,)).fetchone()
        return mr.parse(row[0]) if row else mr.Record(header=["# Master record"])

    def save_record(self, rec: mr.Record) -> None:
        """Keeps the previous version, so a bad import or a mistaken edit can
        be undone. That is the database's version of the timestamped backups
        the command line makes."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        body = mr.render(rec)
        old = self.conn.execute("SELECT body FROM records WHERE user_id = ?",
                                (self.user,)).fetchone()
        if old and old[0] != body:
            self.conn.execute("INSERT INTO record_history VALUES (?, ?, ?)",
                              (self.user, old[0], now))
            self.conn.execute(
                "DELETE FROM record_history WHERE user_id = ? AND saved_at NOT IN "
                "(SELECT saved_at FROM record_history WHERE user_id = ? "
                " ORDER BY saved_at DESC LIMIT ?)", (self.user, self.user, self.HISTORY))
        self.conn.execute(
            "INSERT INTO records VALUES (?, ?, ?) ON CONFLICT(user_id) "
            "DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
            (self.user, body, now))
        self.conn.commit()

    def history(self) -> list:
        return [r[0] for r in self.conn.execute(
            "SELECT saved_at FROM record_history WHERE user_id = ? ORDER BY saved_at DESC",
            (self.user,))]

    def load_state(self, name: str):
        row = self.conn.execute("SELECT body FROM states WHERE user_id = ? AND name = ?",
                                (self.user, name)).fetchone()
        return json.loads(row[0]) if row else None

    def save_state(self, name: str, state) -> None:
        self.conn.execute(
            "INSERT INTO states VALUES (?, ?, ?) ON CONFLICT(user_id, name) "
            "DO UPDATE SET body = excluded.body", (self.user, name, json.dumps(state)))
        self.conn.commit()

    def clear_state(self, name: str) -> None:
        self.conn.execute("DELETE FROM states WHERE user_id = ? AND name = ?",
                          (self.user, name))
        self.conn.commit()

    def delete_everything(self) -> None:
        """A user's right to have their data erased. Removes the record, every
        saved version and every half-finished interview."""
        for table in ("records", "record_history", "states"):
            self.conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (self.user,))
        self.conn.commit()
