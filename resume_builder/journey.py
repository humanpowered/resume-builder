"""
The guided build, step by step: the record as the page, and what the page
needs to remember beside it.

The record (record.md) stays the one source of truth for what the person has
done, because the job pipeline reads it. What the page needs on top lives in
the store's state: which step the person is on, so a refresh lands in the
same place, and what they deleted, so a later upload doesn't quietly put it
back.

A "line" on a job is either a line from their resume (role.recorded_bullets)
or a story built in an earlier interview (role.accomplishments). The page
shows both the same way; `kind` says which list it lives in.
"""
import re
from dataclasses import asdict

from . import health, importer
from . import record as mr

STEP = "journey.step"
DELETED = "journey.deleted"

SCREENS = ("welcome", "upload", "check", "record")
CONTACT_FIELDS = ("Name", "Email", "Telephone", "Location", "Linkedin", "Website")
RESUME, STORY = "resume", "story"


class Stale(Exception):
    """The record changed since the page loaded; the edit would land on the
    wrong thing."""


def clean(value: str) -> str:
    """A value as the person should see it: without the notes a merge leaves
    for conflicts."""
    return re.sub(r"\s*<!--.*?-->", "", value or "").strip()


# --------------------------------------------------------------------------
# What the page shows

def line_text(acc: mr.Accomplishment) -> str:
    return acc.bullet or acc.title


def view(rec: mr.Record) -> dict:
    jobs = []
    for i, role in enumerate(rec.roles):
        lines = [{"kind": RESUME, "index": j, "text": b}
                 for j, b in enumerate(role.recorded_bullets)]
        lines += [{"kind": STORY, "index": j, "text": line_text(a)}
                  for j, a in enumerate(role.accomplishments) if not a.is_empty()]
        same = [r for r in rec.roles if mr._squash(r.employer) == mr._squash(role.employer)]
        jobs.append({
            "index": i, "label": role.label(),
            "employer": role.employer, "title": role.title,
            "dates": clean(role.fields.get("Dates", "")),
            "outside": role.outside(),
            "lines": lines,
            "first_at_employer": same[0] is role,
            "last_at_employer": same[-1] is role,
            "titles_at_employer": len(same),
        })
    contact = {k: clean(rec.contact.get(k, "")) for k in CONTACT_FIELDS}
    return {"jobs": jobs, "contact": contact,
            "line_count": sum(len(j["lines"]) for j in jobs)}


# --------------------------------------------------------------------------
# Where the person is

def load_step(store) -> dict:
    return store.load_state(STEP) or {}


def save_step(store, screen: str) -> dict:
    if screen not in SCREENS:
        raise ValueError(f"No such step: {screen}")
    step = load_step(store)
    step["screen"] = screen
    if screen in ("check", "record"):
        step["seen_check"] = True
    store.save_state(STEP, step)
    return step


def start_screen(store) -> str:
    """Where a page load lands: the step they were on, or for someone who
    built a record before this page existed, their record."""
    step = load_step(store)
    if step.get("screen") in SCREENS:
        return step["screen"]
    return "check" if store.exists() else "welcome"


# --------------------------------------------------------------------------
# What they deleted

def deleted(store) -> dict:
    d = store.load_state(DELETED) or {}
    d.setdefault("jobs", [])
    d.setdefault("lines", [])
    return d


def _remember(store, kind: str, item: dict) -> None:
    d = deleted(store)
    d[kind].append(item)
    store.save_state(DELETED, d)


def _forget_line(store, employer: str, title: str, text: str) -> None:
    d = deleted(store)
    key = importer._norm(text)
    d["lines"] = [x for x in d["lines"] if not (
        importer._norm(x["text"]) == key and mr._squash(x["employer"]) == mr._squash(employer)
        and mr._squash(x["title"]) == mr._squash(title))]
    store.save_state(DELETED, d)


def leave_out_deleted(store, incoming: mr.Record) -> int:
    """Take out of an upload what the person deleted or reworded before, so a
    new file never quietly brings it back. Returns how many things were left out.
    (Offering them back, "Add it back?", is the later-upload review.)"""
    d = deleted(store)
    gone_jobs = {(mr._squash(j["employer"]), mr._squash(j["title"])) for j in d["jobs"]}
    gone_lines = {(mr._squash(x["employer"]), mr._squash(x["title"]), importer._norm(x["text"]))
                  for x in d["lines"]}
    left_out, keep = 0, []
    for role in incoming.roles:
        if (mr._squash(role.employer), mr._squash(role.title)) in gone_jobs:
            left_out += 1
            continue
        before = len(role.recorded_bullets)
        role.recorded_bullets = [
            b for b in role.recorded_bullets
            if (mr._squash(role.employer), mr._squash(role.title), importer._norm(b)) not in gone_lines]
        left_out += before - len(role.recorded_bullets)
        keep.append(role)
    incoming.roles = keep
    return left_out


# --------------------------------------------------------------------------
# Jobs

def job(rec: mr.Record, i: int, expect: str = "") -> mr.Role:
    if not 0 <= i < len(rec.roles):
        raise IndexError("No such job")
    role = rec.roles[i]
    if expect and expect != role.label():
        raise Stale()
    return role


def _start(role: mr.Role):
    rng = health.parse_range(role.fields.get("Dates", ""))
    return rng[0] if rng else None


def add_job(rec: mr.Record, employer: str, title: str, dates: str = "") -> int:
    """Add a job where it belongs, newest first. A job without readable dates
    goes at the end, where the person can see it."""
    employer, title = employer.strip(), title.strip()
    if not employer or not title:
        raise ValueError("A job needs an employer and a title")
    if rec.role_by_employer(employer, title, strict=True):
        raise ValueError(f"{title} at {employer} is already on your record")
    role = mr.Role(employer=employer, title=title)
    if dates.strip():
        role.fields["Dates"] = dates.strip()
    start = _start(role)
    at = len(rec.roles)
    if start:
        for i, other in enumerate(rec.roles):
            theirs = _start(other)
            if theirs and theirs < start:
                at = i
                break
    rec.roles.insert(at, role)
    return at


def edit_job(rec: mr.Record, i: int, expect: str, employer=None, title=None, dates=None) -> mr.Role:
    role = job(rec, i, expect)
    for name, value in (("employer", employer), ("title", title)):
        if value is not None:
            if not value.strip():
                raise ValueError(f"The {name} can't be blank")
            setattr(role, name, value.strip())
    if dates is not None:
        if dates.strip():
            role.fields["Dates"] = dates.strip()
        else:
            role.fields.pop("Dates", None)
    return role


def delete_job(store, rec: mr.Record, i: int, expect: str) -> dict:
    role = job(rec, i, expect)
    _remember(store, "jobs", {
        "employer": role.employer, "title": role.title,
        "dates": clean(role.fields.get("Dates", "")),
        "lines": list(role.recorded_bullets) + [line_text(a) for a in role.accomplishments]})
    rec.roles.pop(i)
    return {"label": role.label()}


# --------------------------------------------------------------------------
# Lines

def _line_list(role: mr.Role, kind: str) -> list:
    if kind == RESUME:
        return role.recorded_bullets
    if kind == STORY:
        return role.accomplishments
    raise ValueError(f"No such kind of line: {kind}")


def _text_at(role: mr.Role, kind: str, li: int) -> str:
    items = _line_list(role, kind)
    if not 0 <= li < len(items):
        raise IndexError("No such line")
    return items[li] if kind == RESUME else line_text(items[li])


def _check_line(role, kind, li, expect):
    if expect and _text_at(role, kind, li) != expect:
        raise Stale()


def add_line(rec: mr.Record, i: int, expect: str, text: str) -> int:
    role = job(rec, i, expect)
    text = text.strip()
    if not text:
        raise ValueError("Write the line first")
    role.recorded_bullets.append(text)
    return len(role.recorded_bullets) - 1


def edit_line(store, rec: mr.Record, i: int, expect: str, kind: str, li: int,
              expect_text: str, text: str) -> None:
    """Reword a line. The old wording is remembered like a deleted line, so
    uploading the same resume again doesn't bring it back beside the new."""
    role = job(rec, i, expect)
    _check_line(role, kind, li, expect_text)
    text = text.strip()
    if not text:
        raise ValueError("A line can't be blank. To take it off, delete it")
    old = _text_at(role, kind, li)
    if importer._norm(old) != importer._norm(text):
        _remember(store, "lines", {"employer": role.employer, "title": role.title,
                                   "text": old, "reworded": True})
    if kind == RESUME:
        role.recorded_bullets[li] = text
    else:
        role.accomplishments[li].bullet = text


def delete_line(store, rec: mr.Record, i: int, expect: str, kind: str, li: int,
                expect_text: str) -> dict:
    """Delete at once; the page offers Undo with what this returns."""
    role = job(rec, i, expect)
    _check_line(role, kind, li, expect_text)
    items = _line_list(role, kind)
    item = items.pop(li)
    text = item if kind == RESUME else line_text(item)
    _remember(store, "lines", {"employer": role.employer, "title": role.title, "text": text})
    return {"kind": kind, "index": li, "text": text,
            "story": asdict(item) if kind == STORY else None}


def restore_line(store, rec: mr.Record, i: int, expect: str, kind: str, li: int,
                 text: str, story: dict | None = None) -> None:
    """Undo a delete: the line goes back where it was, and is no longer
    remembered as deleted."""
    role = job(rec, i, expect)
    items = _line_list(role, kind)
    li = max(0, min(li, len(items)))
    if kind == RESUME:
        items.insert(li, text)
    else:
        fields = {k: v for k, v in (story or {}).items()
                  if k in mr.Accomplishment.__dataclass_fields__}
        items.insert(li, mr.Accomplishment(**fields) if fields else mr.Accomplishment(bullet=text))
    _forget_line(store, role.employer, role.title, text)


# --------------------------------------------------------------------------
# Contact

def edit_contact(rec: mr.Record, fields: dict) -> None:
    for key, value in fields.items():
        if key not in CONTACT_FIELDS:
            raise ValueError(f"Can't edit {key!r} here")
        if value.strip():
            rec.contact[key] = value.strip()
        else:
            rec.contact.pop(key, None)
