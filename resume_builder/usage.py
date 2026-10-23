"""
Spending limits and usage events for the hosted test.

Every model call made for a signed-in person goes through a Meter: before the
call it checks their limits, after it records what the call cost. Three limits,
each a setting so it can be raised without a code change:

    RB_CAP_TESTER_USD   one person's spend over the whole test   (default 5)
    RB_CAP_MONTH_USD    everyone's spend this calendar month     (default 150)
    RB_CAP_CALLS_HOUR   one person's model calls in the last hour (default 60)
    RB_CAP_OVERRIDES    per-person tester caps, "a@x.com=20,b@y.com=10"

Usage events are one JSON line each on standard output. On Cloud Run those
lines land in Cloud Logging, and a log sink copies them to BigQuery. An event
names the person only by a random tester ID, never by email, and carries no
record text.
"""
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

from . import llm

TESTER_DONE = ("You've reached the test limit for AI help. Your record is saved, "
               "and you can still edit and download it.")
MONTH_DONE = ("The test has used its AI budget for this month. Your record is saved, "
              "and you can still edit and download it.")
TOO_FAST = ("That's a lot of AI help in one hour. Your record is saved; "
            "try again a little later.")


def _setting(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def tester_cap(user: str) -> float:
    for pair in os.environ.get("RB_CAP_OVERRIDES", "").split(","):
        email, _, amount = pair.partition("=")
        if email.strip().lower() == user.lower() and amount.strip():
            try:
                return float(amount)
            except ValueError:
                pass
    return _setting("RB_CAP_TESTER_USD", 5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def tester_id(conn, user: str) -> str:
    """A random ID that stands for this person in usage events. Only this
    table links it to their email, and erasing their data removes it."""
    row = conn.execute("SELECT tester_id FROM testers WHERE user_id = ?", (user,)).fetchone()
    if row:
        return row[0]
    new = "t_" + secrets.token_hex(6)
    conn.execute("INSERT INTO testers VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING",
                 (user, new))
    conn.commit()
    return conn.execute("SELECT tester_id FROM testers WHERE user_id = ?", (user,)).fetchone()[0]


def emit(event: str, tester: str, **fields) -> None:
    """One usage event as a JSON line Cloud Logging reads as structured data."""
    line = {"rb_event": event, "tester": tester, "at": _now().isoformat(), **fields}
    print(json.dumps(line), file=sys.stdout, flush=True)


def event(conn, user: str, name: str, **fields) -> None:
    emit(name, tester_id(conn, user), **fields)


class Meter:
    """One person's limits, for one request."""

    def __init__(self, conn, user: str):
        self.conn, self.user = conn, user

    def spent(self) -> float:
        return self.conn.execute("SELECT COALESCE(SUM(cost), 0) FROM spend WHERE user_id = ?",
                                 (self.user,)).fetchone()[0]

    def check(self) -> None:
        now = _now()
        month = now.strftime("%Y-%m-01")
        hour_ago = (now - timedelta(hours=1)).isoformat()
        q = self.conn.execute
        reason = None
        if self.spent() >= tester_cap(self.user):
            reason, message = "tester", TESTER_DONE
        elif q("SELECT COALESCE(SUM(cost), 0) FROM spend WHERE at >= ?",
               (month,)).fetchone()[0] >= _setting("RB_CAP_MONTH_USD", 150):
            reason, message = "month", MONTH_DONE
        elif q("SELECT COUNT(*) FROM spend WHERE user_id = ? AND at >= ?",
               (self.user, hour_ago)).fetchone()[0] >= _setting("RB_CAP_CALLS_HOUR", 60):
            reason, message = "hourly", TOO_FAST
        if reason:
            event(self.conn, self.user, "cap_reached", cap=reason)
            raise llm.LimitReached(message)

    def record(self, what: str, dollars: float, version: str = "") -> None:
        self.conn.execute("INSERT INTO spend VALUES (?, ?, ?, ?)",
                          (self.user, _now().isoformat(), what, dollars))
        self.conn.commit()
        event(self.conn, self.user, "model_call", what=what, cost_usd=round(dollars, 5),
              **({"prompt_version": version} if version else {}))


def report(conn) -> list:
    """Spend per person, most first, with this month's total for everyone."""
    month = _now().strftime("%Y-%m-01")
    rows = conn.execute("SELECT user_id, SUM(cost), COUNT(*) FROM spend GROUP BY user_id "
                        "ORDER BY SUM(cost) DESC").fetchall()
    total = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM spend WHERE at >= ?",
                         (month,)).fetchone()[0]
    return [(u, c, n, tester_cap(u)) for u, c, n in rows], total
