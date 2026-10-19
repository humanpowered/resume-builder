"""
Empties the test site's database between test rounds: every tester's record,
saved versions and half-finished interviews. Not for a live site.

    python -m web.wipe_test_data            shows what it would empty
    python -m web.wipe_test_data --yes      empties it

Uses the same setting as the app: RB_DATABASE_URL, else the SQLite file RB_DB.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resume_builder import store  # noqa: E402


def main(argv) -> int:
    target = store.database()
    shown = re.sub(r"://([^:@/]+):[^@]*@", r"://\1:***@", target)  # never print a password
    conn = store.connect(target)
    store.SqlStore.init(conn)
    users = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    if "--yes" not in argv:
        print(f"{shown} holds {users} record(s). Run again with --yes to empty it.")
        return 0
    store.SqlStore.wipe_all(conn)
    conn.close()
    print(f"Emptied {shown}: {users} record(s) removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
