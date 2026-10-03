"""
Reads replies from employers out of your mailbox and updates the tracker.

The tracker only ever knew what you typed into it, so an application could sit
at "applied" for weeks after the company had already answered. Three
rejections and one scheduled interview were once all sitting unread in the
tracker while the morning brief called them "gone quiet".

What it does NOT do: it never marks anything read, never replies, never
deletes. The mailbox is opened read-only.

Two rules protect your own work:
  1. A status you entered by hand is never overwritten, except that a plain
     "applied" may advance to rejected/interview on clear evidence.
  2. Nothing ever moves backwards. An interview or offer is never downgraded.

Everything it changes, and everything it was unsure about, is written to
output/replies.json for the morning brief to report.

Usage:
  python check_replies.py            # apply changes
  python check_replies.py --dry-run  # show what it would do, change nothing
"""
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

SRC = Path(__file__).parent
OUTPUT_DIR = SRC.parent / "output"
TRACKER = OUTPUT_DIR / "application_tracker.csv"
REPLIES = OUTPUT_DIR / "replies.json"
CONFIG = SRC.parent / "config" / "boards.yaml"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ordered: the first pattern that matches wins, so a rejection is never read as
# an acknowledgement just because the mail also says "thank you for applying".
CLASSIFIERS = [
    ("rejected", [
        r"not (?:be )?(?:moving|move) forward",
        r"unable to (?:move|proceed)",
        r"will not be (?:moving|proceeding)",
        r"decided to (?:move|go) forward with other",
        r"pursuing other candidates",
        r"not (?:been )?selected",
        r"no longer under consideration",
        r"we regret to inform",
        r"position has been filled",
        r"filled the (?:position|role)",
        # One employer wrote: "the role is unfortunately no longer available
        # ... extended an offer to a finalist." A closed role is a rejection,
        # however politely phrased.
        r"no longer available",
        r"no longer open",
        r"(?:has|have) been closed",
        r"extended an offer",
        r"offer to (?:a|another) (?:finalist|candidate)",
        r"moving forward with (?:other|another)",
        r"decided not to (?:proceed|move)",
    ]),
    ("interview", [
        r"schedule (?:an?|your|the) (?:interview|call|conversation|chat)",
        r"invite you to (?:an?|the) interview",
        r"set up (?:a|an|some) (?:time|call|interview)",
        r"your availability",
        r"available for a (?:call|chat|conversation|interview)",
        r"teams meeting",
        r"zoom meeting",
        r"phone screen",
        r"next steps? in (?:our|the) (?:process|interview)",
        r"we(?:'d| would) like to (?:meet|speak|talk|chat)",
    ]),
    ("acknowledged", [
        r"thank you for applying",
        r"thanks for applying",
        r"we(?:'ve| have) received your application",
        r"application (?:has been|was) (?:successfully )?(?:received|submitted)",
        r"thank you for your (?:interest|application)",
    ]),
]
COMPILED = [(name, [re.compile(p, re.I) for p in pats]) for name, pats in CLASSIFIERS]

# Statuses that mean the conversation is over or further along than mail can
# tell us. Never touched.
TERMINAL = {"offer", "rejected", "passed", "closed", "screening", "interview"}

STOPWORDS = {"the", "and", "of", "for", "at", "a", "an", "inc", "llc", "corp",
             "company", "group", "careers", "jobs", "team", "remote", "us"}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def tokens(text: str) -> list[str]:
    return [w for w in norm(text).split() if len(w) > 2 and w not in STOPWORDS]


def classify(subject: str, body: str) -> str | None:
    blob = f"{subject}\n{body[:4000]}"
    for name, patterns in COMPILED:
        if any(p.search(blob) for p in patterns):
            return name
    return None


def parse_date(s: str):
    s = (s or "").strip()
    if not s or s.upper() == "NA":
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


JUNK_COMPANY = {"jobs", "careers", "career", "job", "apply", "talent", "team",
                "remote", "boards", "work", "hiring"}
# Hosts that identify a job board rather than an employer. Left in, they match
# any email carrying that word -- "linkedin" sits in the footer of half the
# mail in an inbox, which matched a franchise-sales email to Wolters Kluwer.
GENERIC_HOSTS = {"linkedin", "adzuna", "jooble", "experteer", "indeed",
                 "glassdoor", "ziprecruiter", "dice", "monster", "builtin",
                 "simplify", "google", "greenhouse", "lever", "ashbyhq",
                 "smartrecruiters", "workable", "icims", "taleo", "jobvite",
                 "myworkdayjobs", "successfactors", "oraclecloud", "workday",
                 "paradox", "gem", "hire", "talent", "recruiting", "mail"}
ATS_PATH = re.compile(
    r"(?:lever\.co|greenhouse\.io|ashbyhq\.com|myworkdayjobs\.com|"
    r"smartrecruiters\.com|icims\.com|workable\.com)/([a-z0-9\-_]{3,})", re.I)
SITE_HOST = re.compile(r"https?://(?:www\.|jobs\.|boards\.|careers\.|job-boards\.)*"
                       r"([a-z0-9\-]{3,})\.", re.I)


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


CONNECTORS = {"and", "the", "of", "for", "inc", "llc", "ltd", "corp", "co",
              "group", "company", "holdings"}


def name_variants(name: str) -> set[str]:
    """
    Both spellings of a joined company name.

    The tracker stores an ATS token like "beck-and-rowe"; the email says
    "Beck & Rowe", where the ampersand normalizes to a space and the "and"
    simply is not there. Compare "beckandrowe" against "beckrowe" and they
    never meet, so a real rejection went unmatched. Emit the compacted name and the same name with connector
    words removed.
    """
    compacted = compact(name)
    parts = [p for p in re.split(r"[^a-z0-9]+", (name or "").lower()) if p]
    stripped = "".join(p for p in parts if p not in CONNECTORS)
    return {v for v in (compacted, stripped) if v}


def company_keys(row: dict) -> set[str]:
    """
    Names that would identify this employer in an email.

    The tracker's company column is often an ATS token rather than a name
    (one company's token is literally "jobs", another's is "careers"), so the
    posting URL is a better source: jobs.lever.co/northwind and
    boards.greenhouse.io/beckrowe both carry the employer in the path.
    """
    keys = set()
    keys |= name_variants(row.get("company", ""))
    url = (row.get("url") or "")
    m = ATS_PATH.search(url)
    if m:
        keys |= name_variants(m.group(1))
    m = SITE_HOST.search(url)
    if m:
        keys |= name_variants(m.group(1))
    return {k for k in keys
            if len(k) >= 4 and k not in JUNK_COMPANY and k not in GENERIC_HOSTS}


def match_row(message: dict, rows: list[dict]) -> tuple[dict | None, int]:
    """
    Score each application against the message and take the best.

    The employer must be named. An earlier version scored on title tokens
    alone, and since half these applications are called "Director, Marketing
    Analytics", one company's rejection matched another company's row, and
    franchise-sales mail matched a real application as an interview. Title now only
    disambiguates between roles at the same employer; it can never carry a
    match by itself.
    """
    sender_domain = compact(message["sender"].split("@")[-1])
    blob = norm(f"{message['subject']} {message['body'][:3000]}")
    blob_compact = compact(blob)
    scored: list[tuple[int, dict]] = []

    for r in rows:
        submitted = parse_date(r.get("date_submitted", ""))
        if not submitted:
            continue
        if message["date"] and message["date"].date() < submitted - timedelta(days=2):
            continue                      # predates the application

        keys = company_keys(r)
        if not keys:
            continue
        in_body = any(k in blob_compact for k in keys)
        in_sender = any(k in sender_domain for k in keys)
        if not (in_body or in_sender):
            continue                      # employer never named: not this one

        score = 4 if in_body else 0
        if in_sender:
            score += 3                    # mail straight from the employer

        title_tokens = tokens(r.get("title", ""))
        if title_tokens:
            hits = sum(1 for t in title_tokens if t in blob)
            if hits >= max(2, len(title_tokens) // 2):
                score += 3
            elif hits:
                score += 1

        scored.append((score, r))

    if not scored:
        return None, 0, 0
    scored.sort(key=lambda x: -x[0])
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    return best, best_score, runner_up


def main(dry_run: bool = False):
    if not TRACKER.exists():
        print("  no application_tracker.csv yet; nothing to check")
        return

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    ecfg = cfg.get("email") or {}
    user = os.environ.get("IMAP_USER")
    password = os.environ.get("IMAP_APP_PASSWORD")
    if not (user and password):
        print("  [warn] reply check needs IMAP_USER and IMAP_APP_PASSWORD -- skipping")
        return

    from scraper import imap_messages

    with open(TRACKER, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0].keys()) if rows else []
    live = [r for r in rows if parse_date(r.get("date_submitted", ""))]

    since_days = ecfg.get("reply_since_days", 14)
    applied, unsure, acknowledged = [], [], []

    # Runs server-side on Gmail, so only reply-shaped mail is fetched rather
    # than the whole inbox. Deliberately broader than the classifiers below:
    # this narrows what to download, the regexes decide what it means.
    # Phrases, not single words. An earlier version searched subject:application
    # and subject:regarding, which matched newsletters by the hundred and blew
    # past the fetch cap, hiding the older replies it was meant to find.
    gmail_query = (
        f"newer_than:{since_days}d -in:sent "
        '("thank you for applying" OR "thanks for applying" OR "your application" '
        'OR "we received your application" OR "received your application" '
        'OR "thank you for your interest" OR "thanks for your interest" '
        'OR "not moving forward" OR "not be moving forward" '
        'OR "unable to move forward" OR "regret to inform" OR "other candidates" '
        'OR "no longer available" OR "no longer open" OR "extended an offer" '
        'OR "your availability" OR "schedule a call" OR "schedule an interview" '
        'OR "phone screen" OR subject:interview)'
    )

    for m in imap_messages(ecfg.get("host", "imap.gmail.com"), user, password,
                           ecfg.get("mailbox", "INBOX"), since_days,
                           ecfg.get("reply_max_messages", 300),
                           gmail_query=gmail_query):
        verdict = classify(m["subject"], m["body"])
        if not verdict:
            continue
        row, score, runner_up = match_row(m, live)
        # Two applications at the same employer (two roles at one company) both
        # match its rejection mail. Closing the wrong one is worse than
        # closing neither, so anything this close gets reported, not applied.
        too_close = row is not None and (score - runner_up) < 2
        if not row or score < 5 or too_close:
            if verdict in ("rejected", "interview") and row:
                unsure.append({"company": row.get("company", ""),
                               "title": row.get("title", ""),
                               "verdict": verdict, "score": score,
                               "reason": ("two roles at this employer match equally"
                                          if too_close else "weak match"),
                               "subject": m["subject"][:120],
                               "sender": m["sender"]})
            continue

        current = (row.get("status") or "").strip().lower()
        if current in TERMINAL:
            continue                       # never move backwards or overwrite
        if current not in ("", "applied"):
            continue                       # something you typed; leave it alone

        when = (m["date"].date() if m["date"] else date.today()).isoformat()
        change = {"company": row.get("company", ""), "title": row.get("title", ""),
                  "from": row.get("status", "") or "(blank)", "to": verdict,
                  "date": when, "subject": m["subject"][:120],
                  "sender": m["sender"], "score": score}

        if verdict == "acknowledged":
            # not a status change; just evidence the application landed
            note = f"[acknowledged {when}]"
            if note not in (row.get("notes") or ""):
                if not dry_run:
                    row["notes"] = (note + " " + (row.get("notes") or "")).strip()
                acknowledged.append(change)
            continue

        applied.append(change)
        if not dry_run:
            row["status"] = verdict
            note = f"[{verdict} {when} by email]"
            if note not in (row.get("notes") or ""):
                row["notes"] = (note + " " + (row.get("notes") or "")).strip()

    # Acknowledgements only add a note, but they still change the file. An
    # earlier version wrote the tracker only when a status changed, so those
    # notes were computed and then dropped on the floor.
    if (applied or acknowledged) and not dry_run:
        try:
            with open(TRACKER, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
        except PermissionError:
            print("  [TRACKER LOCKED] application_tracker.csv is open in another "
                  "program; no reply updates were saved")
            return

    payload = {"checked_at": datetime.now().isoformat(timespec="seconds"),
               "window_days": since_days, "changes": applied,
               "acknowledged": acknowledged, "unsure": unsure}
    if not dry_run:
        REPLIES.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    label = "would update" if dry_run else "updated"
    print(f"  replies: {label} {len(applied)} application(s)"
          + (f", {len(acknowledged)} acknowledgement(s) noted" if acknowledged else "")
          + (f", {len(unsure)} need a look" if unsure else ""))
    for c in applied:
        print(f"    {c['company'][:26]:28} | {c['title'][:34]:36} | "
              f"{c['from']} -> {c['to']} ({c['date']})")
    for u in unsure:
        print(f"    [unsure] {u['company'][:22]:24} | {u['verdict']:12} | {u['subject'][:56]}")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
