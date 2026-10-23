"""
What the model has cost on the test site: each person's spend against their
cap, and this month's total against the monthly budget.

    python -m web.spend_report

Uses the same setting as the app: RB_DATABASE_URL, else the SQLite file RB_DB.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resume_builder import store, usage  # noqa: E402


def main() -> int:
    conn = store.connect(store.database())
    store.SqlStore.init(conn)
    rows, month = usage.report(conn)
    conn.close()
    budget = float(os.environ.get("RB_CAP_MONTH_USD", 150))
    print(f"This month: ${month:.2f} of ${budget:.2f}")
    if not rows:
        print("No model calls yet.")
    for user, spent, calls, cap in rows:
        print(f"  {user:<36} ${spent:>6.2f} of ${cap:.2f}  ({calls} calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
