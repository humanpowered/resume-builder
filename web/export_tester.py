"""
Saves what testers made on the test site as Word files, the same files they
download themselves: each person's career record and every sample resume.

    python -m web.export_tester --list               who has a record
    python -m web.export_tester pat@example.com      one person
    python -m web.export_tester --all                everyone

Files go to a folder per person under exports\\ (or --out FOLDER). Uses the
same setting as the app: RB_DATABASE_URL, else the SQLite file RB_DB.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resume_builder import resume_file, sample, store  # noqa: E402


def _safe(text: str) -> str:
    return re.sub(r"[^\w@. -]+", "", text).strip(" .")[:80] or "untitled"


def people(conn) -> list:
    return conn.execute("SELECT user_id, updated_at FROM records ORDER BY updated_at DESC").fetchall()


def export(conn, user: str, out: Path) -> list:
    """Writes one person's files and returns their paths."""
    s = store.SqlStore(conn, user)
    if not s.exists():
        raise SystemExit(f"No record for {user}.")
    folder = out / _safe(user)
    folder.mkdir(parents=True, exist_ok=True)
    files = {"career record.docx": resume_file.master_docx(s.load_record())}
    samples = sample.view(s)
    if samples["general"]:
        files["resume - general.docx"] = resume_file.to_docx(samples["general"])
    for n, res in enumerate(samples["tailored"], 1):
        post = res.get("posting") or {}
        label = " at ".join(x for x in (post.get("title"), post.get("company")) if x)
        files[f"resume {n} - {_safe(label) if label else 'for a posting'}.docx"] = resume_file.to_docx(res)
    written = []
    for name, data in files.items():
        path = folder / name
        path.write_bytes(data)
        written.append(path)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("email", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", default="exports")
    a = ap.parse_args(argv)
    conn = store.connect(store.database())
    store.SqlStore.init(conn)
    try:
        everyone = people(conn)
        if a.list or not (a.email or a.all):
            for user, updated in everyone:
                print(f"  {user:<36} last saved {updated[:16].replace('T', ' ')} UTC")
            if not everyone:
                print("No records yet.")
            return 0
        for user in ([u for u, _ in everyone] if a.all else [a.email.strip().lower()]):
            for path in export(conn, user, Path(a.out)):
                print(path)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
