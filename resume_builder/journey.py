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

from . import health, importer, llm
from . import record as mr

STEP = "journey.step"
DELETED = "journey.deleted"
ANSWERS = "journey.answers"

SCREENS = ("welcome", "upload", "check", "targets", "bg", "lines", "line", "more", "end",
           "stopped", "bgdone", "record")
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


def save_step(store, screen: str, at: str = "") -> dict:
    """`at` names the question on the background screens, so a refresh
    lands on the same question."""
    if screen not in SCREENS:
        raise ValueError(f"No such step: {screen}")
    step = load_step(store)
    step["screen"] = screen
    step["at"] = at
    if screen != "welcome" and screen != "upload":
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


# --------------------------------------------------------------------------
# Jobs you're aiming for

MAX_TARGETS = 3
WORK_MODES = ("Remote", "Hybrid", "On site")
# Kept on the record beside the target fields the pipeline already reads.
WORK_ARRANGEMENT = "Work arrangement"


def _split(value: str) -> list:
    return [t.strip() for t in re.split(r"[;\n]", clean(value)) if t.strip()]


def answers(store) -> dict:
    a = store.load_state(ANSWERS) or {}
    a.setdefault("emp", {})
    a.setdefault("job", {})
    return a


def targets_view(store, rec: mr.Record) -> dict:
    titles = _split(rec.target.get("Titles", ""))
    taken = {mr._squash(t) for t in titles}
    seen, suggest = set(), []
    for role in rec.roles:
        key = mr._squash(role.title)
        if role.title and not role.outside() and key not in taken and key not in seen:
            seen.add(key)
            suggest.append(role.title)
    return {"titles": titles, "where": clean(rec.target.get("Locations", "")),
            "modes": [m for m in _split(rec.target.get(WORK_ARRANGEMENT, "")) if m in WORK_MODES],
            "status": answers(store).get("targets", ""), "suggest": suggest[:6]}


def save_targets(store, rec: mr.Record, titles: list, where: str, modes: list,
                 not_sure: bool = False) -> None:
    """One list of up to three titles for the whole tool. Saving never resets
    an answer anywhere else."""
    out, seen = [], set()
    for t in titles:
        t = (t or "").strip()
        if t and mr._squash(t) not in seen:
            seen.add(mr._squash(t))
            out.append(t)
    if len(out) > MAX_TARGETS:
        raise ValueError(f"Up to {MAX_TARGETS} titles. Remove one to add another")
    bad = [m for m in modes if m not in WORK_MODES]
    if bad:
        raise ValueError(f"Not a way of working: {bad[0]}")
    for key, value in (("Titles", "; ".join(out)), ("Locations", (where or "").strip()),
                       (WORK_ARRANGEMENT, "; ".join(m for m in WORK_MODES if m in modes))):
        if value:
            rec.target[key] = value
        else:
            rec.target.pop(key, None)
    a = answers(store)
    a["targets"] = "not_sure" if not_sure and not out else "saved"
    store.save_state(ANSWERS, a)


# --------------------------------------------------------------------------
# The background questions, one job at a time

COMPANY = "Company"
QUESTIONS = (
    ("Challenge", "What were you brought in to do?",
     "The reason the job existed, or the problem you were hired to fix.",
     ["Open a second warehouse and get it running", "Cover nights on a busy cardiac ward",
      "Win back accounts the last rep lost"]),
    ("Authority", "What were you responsible for, and how much of it?",
     "Count whatever you led, handled or supported: staff, the people you served, money, "
     "customers, systems, projects or territory. Use the largest it reached. Part of a team? "
     "Give the team\u2019s size and your share. A rough figure or a range is fine.",
     ["A team of 12 and a $4M annual budget", "6 patients a shift on a 30-bed unit",
      "The ordering system used by 300 staff, about 2 million orders a month",
      "Calendars and travel for 3 executives, about 40 meetings a week"]),
    ("Results against targets", "How was your work judged?",
     "What your manager looked at to decide whether you were doing well. Formal targets and "
     "informal ones both count.",
     ["Sales quota", "On-time delivery rate", "Patient satisfaction scores",
      "Hitting client deadlines"]),
    ("Recognition", "Were you recognised for it?",
     "Any sign that someone else thought your work was good.",
     ["Promoted after 18 months", "Employee of the quarter", "Asked to train new hires",
      "Picked to lead the system rollout"]),
)
QUESTION_FIELDS = tuple(q[0] for q in QUESTIONS)
COMPANY_HELP = ("A line a stranger would understand. Asked once for each employer.",
                ["Member-owned grocery store, two locations", "Regional hospital, 300 beds",
                 "Software company selling payroll tools to small businesses"])
# What a step can be marked: answered, skipped (the employer line), doesn't
# apply (a background question), or done (the promotions question).
STATUSES = ("answered", "skipped", "na", "done")


def emp_key(role: mr.Role) -> str:
    return mr._squash(role.employer)


def job_key(role: mr.Role) -> str:
    return f"{mr._squash(role.employer)}|{mr._squash(role.title)}"


def _groups(rec: mr.Record) -> list:
    """Jobs by employer, in the order the employers first appear (newest
    first), each employer's titles in record order."""
    order, by = [], {}
    for i, role in enumerate(rec.roles):
        k = emp_key(role)
        if k not in by:
            by[k] = []
            order.append(k)
        by[k].append(i)
    return [(k, by[k]) for k in order]


def steps(store, rec: mr.Record) -> list:
    """Every background question for every job, in the order they're asked.
    Something the upload already gave us is left out, so it's never asked;
    an answer given here stays in, so Back can return to it."""
    a = answers(store)
    jobs = [i for _, idx in _groups(rec) for i in idx]
    number = {i: n + 1 for n, i in enumerate(jobs)}
    out = []

    def step(kind, i, field, status, text, **more):
        role = rec.roles[i]
        out.append({"id": "|".join(x for x in (kind, emp_key(role) if kind != "q" else job_key(role),
                                                field if kind == "q" else "") if x),
                    "kind": kind, "field": field, "job": i, "job_number": number[i],
                    "job_total": len(jobs), "employer": role.employer, "title": role.title,
                    "dates": clean(role.fields.get("Dates", "")), "label": role.label(),
                    "status": status or "todo", "answer": clean(text), **more})

    for ek, idx in _groups(rec):
        roles = [rec.roles[i] for i in idx]
        emp = a["emp"].get(ek, {})
        given = next((clean(r.fields.get(COMPANY, "")) for r in roles
                      if clean(r.fields.get(COMPANY, ""))), "")
        if emp.get(COMPANY) or not given:
            step("company", idx[0], COMPANY, emp.get(COMPANY), given)
        if not all(r.outside() for r in roles):
            step("titles", idx[0], "titles", emp.get("titles"), "",
                 titles=[{"title": r.title, "dates": clean(r.fields.get("Dates", ""))}
                         for r in roles])
        for i in idx:
            role = rec.roles[i]
            mine = a["job"].get(job_key(role), {})
            for f in QUESTION_FIELDS:
                text = role.fields.get(f, "")
                if mine.get(f) or not clean(text):
                    step("q", i, f, mine.get(f), text)
    return out


def question(field: str) -> dict:
    for f, q, help_, ex in QUESTIONS:
        if f == field:
            return {"q": q, "help": help_, "examples": ex}
    raise ValueError(f"No such question: {field}")


def answer(store, rec: mr.Record, step_id: str, status: str, text: str = "") -> dict:
    """Save one answer and mark the step. Skip and Doesn't apply leave the
    answer empty; nothing is ever filled in for the person."""
    if status not in STATUSES:
        raise ValueError(f"No such status: {status}")
    found = next((s for s in steps(store, rec) if s["id"] == step_id), None)
    if found is None:
        raise IndexError("That question isn't on your record any more")
    text = (text or "").strip()
    if status == "answered" and not text:
        status = "skipped" if found["kind"] == "company" else "na"
    if status != "answered":
        text = ""
    a = answers(store)
    role = rec.roles[found["job"]]
    out = {}
    if found["kind"] == "company":
        for r in rec.roles:
            if emp_key(r) == emp_key(role):
                if text:
                    r.fields[COMPANY] = text
                else:
                    r.fields.pop(COMPANY, None)
        a["emp"].setdefault(emp_key(role), {})[COMPANY] = status
    elif found["kind"] == "titles":
        a["emp"].setdefault(emp_key(role), {})["titles"] = "done"
    else:
        if text:
            role.fields[found["field"]] = text
        else:
            role.fields.pop(found["field"], None)
        a["job"].setdefault(job_key(role), {})[found["field"]] = status
        # A promotion mentioned with only one title on file: ask for the
        # earlier title, so each shows on the resume with its dates.
        same = [r for r in rec.roles if emp_key(r) == emp_key(role)]
        if (found["field"] == "Recognition" and re.search(r"promot", text, re.I)
                and len(same) == 1 and not role.outside()):
            out["ask_earlier_title"] = f"titles|{emp_key(role)}"
    store.save_state(ANSWERS, a)
    return out


def background_view(rec: mr.Record) -> list:
    """Each job's background answers as the record page shows them."""
    names = {COMPANY: "What the employer does"}
    names.update({f: q for f, q, _, _ in QUESTIONS})
    return [[{"field": f, "question": names[f], "answer": clean(role.fields.get(f, ""))}
             for f in (COMPANY,) + QUESTION_FIELDS if clean(role.fields.get(f, ""))]
            for role in rec.roles]


# --------------------------------------------------------------------------
# Making lines stronger, one job at a time
#
# Each line on a job carries a status: todo, skipped (postponed, never a
# block), stronger, kept. Removing a line deletes it from the record. A line
# is found by its wording, so the status follows it however the list shifts.

WORK = "journey.work"
LINE_ANSWERS = "journey.lineanswers"
LINE_STATUSES = ("todo", "skipped", "stronger", "kept")
LINE_FIELDS = ("situation", "did", "result", "number")
LINE_STEPS = (
    ("situation", "Situation", "What was going on?",
     "What was the problem, goal or deadline before you acted? This gives the result its stakes.",
     "Both warehouses ran heavy overtime every week during peak season."),
    ("did", "What you did", "What did you do about it?",
     "Your part, in your own words. \"We\" is fine; say what you did within it. This keeps "
     "the line about you, not the team.",
     "I redesigned the shift handoffs and cross-trained pickers to cover gaps."),
    ("result", "Result", "What changed because of it?",
     "For the business, the team or the customer. This is what a reader cares about most.",
     "Overtime came down and stayed down through the next peak."),
    ("number", "Number", "Can you put a number on it?",
     "A rough figure is fine: a percentage, hours, dollars, people. Numbers are what make a "
     "line stand out on a skim. Leave out anything your employer treats as confidential.",
     "About 30% less overtime; 12 people cross-trained."),
)
MORE_CHIPS = ("Trained or mentored people", "Saved money or time", "Fixed a recurring problem",
              "Won or kept a customer", "Improved safety or quality", "Led a project or rollout",
              "Was picked for something")

# The suggested starting lines follow a stated rule: first a change with no
# result or number ("Reduced overtime costs"), then a duty or a vague verb
# that hides the person's part, then lines that already carry a number.
CHANGE_WORDS = re.compile(
    r"\b(reduc|increas|improv|cut|grew|grow|rais|lower|sav|streamlin|boost|expand|"
    r"decreas|accelerat|shorten|speed|sped|doubl|tripl|launch|implement|introduc|"
    r"transform|turn(ed)? around|won|win)\w*", re.I)
VAGUE_WORDS = re.compile(
    r"^(responsible for|duties included|tasked with|helped|assisted|worked on|involved in|"
    r"participated in|in charge of|managed|handled|supported|coordinated|oversaw|"
    r"prepared|maintained|performed)\b", re.I)
NUMBER = re.compile(r"\d")
SUGGEST = 3


def line_key(text: str) -> str:
    return importer._norm(text)[:160]


def line_rank(text: str) -> int:
    if NUMBER.search(text):
        return 2
    if CHANGE_WORDS.search(text) and not VAGUE_WORDS.match(text):
        return 0
    return 1


def _work(store) -> dict:
    return store.load_state(WORK) or {}


def _job_work(w: dict, role: mr.Role) -> dict:
    j = w.setdefault(job_key(role), {})
    j.setdefault("lines", {})
    return j


def _lines(role: mr.Role) -> list:
    out = [{"kind": RESUME, "index": n, "text": t} for n, t in enumerate(role.recorded_bullets)]
    out += [{"kind": STORY, "index": n, "text": line_text(a)}
            for n, a in enumerate(role.accomplishments) if not a.is_empty()]
    return out


def _months(ranges: list) -> int:
    """Months covered by a set of date ranges, overlaps counted once."""
    pts = sorted((s[0] * 12 + s[1], e[0] * 12 + e[1]) for s, e in ranges)
    total, cur = 0, None
    for a, b in pts:
        if cur is None or a > cur[1]:
            if cur:
                total += cur[1] - cur[0] + 1
            cur = [a, b]
        else:
            cur[1] = max(cur[1], b)
    if cur:
        total += cur[1] - cur[0] + 1
    return total


def work_view(store, rec: mr.Record) -> dict:
    """Each job's lines with their status and the suggested starting lines,
    whether the job is complete, and the record's years covered."""
    w = _work(store)
    bg = steps(store, rec)
    order = [i for _, idx in _groups(rec) for i in idx]
    number = {i: n + 1 for n, i in enumerate(order)}
    jobs, done_ranges, all_ranges = [], [], []
    for i, role in enumerate(rec.roles):
        j = w.get(job_key(role), {})
        status = j.get("lines", {})
        lines = []
        for l in _lines(role):
            st = status.get(line_key(l["text"]), "todo")
            lines.append({**l, "key": line_key(l["text"]), "status": st,
                          "rank": line_rank(l["text"]), "new": line_key(l["text"]) in j.get("new", [])})
        todo = sorted((l for l in lines if l["status"] == "todo"), key=lambda l: (l["rank"], l["index"]))
        open_bg = [s for s in bg if s["job"] == i and s["status"] == "todo"]
        complete = j.get("done") or (not open_bg and j.get("more")
                                     and all(l["status"] in ("stronger", "kept") for l in lines))
        rng = health.parse_range(role.fields.get("Dates", ""))
        if rng and not role.outside():
            all_ranges.append(rng)
            if complete:
                done_ranges.append(rng)
        count = {s: sum(1 for l in lines if l["status"] == s) for s in LINE_STATUSES}
        jobs.append({"key": job_key(role), "index": i, "label": role.label(),
                     "number": number[i], "total": len(order),
                     "dates": clean(role.fields.get("Dates", "")),
                     "title": role.title, "employer": role.employer,
                     "lines": lines, "suggested": [l["key"] for l in todo[:SUGGEST]],
                     "next_line": todo[0]["key"] if todo else "",
                     "counts": count, "open_questions": len(open_bg),
                     "more": bool(j.get("more")), "done": bool(j.get("done")),
                     "complete": bool(complete), "intro_seen": bool(w.get("intro_seen"))})
    return {"jobs": jobs, "order": order,
            "years": {"covered": round(_months(done_ranges) / 12),
                      "total": round(_months(all_ranges) / 12)},
            "complete_count": sum(1 for j in jobs if j["complete"])}


def _find_line(role: mr.Role, key: str) -> dict:
    for l in _lines(role):
        if line_key(l["text"]) == key:
            return l
    raise IndexError("That line isn't on this job any more")


def _role_by_key(rec: mr.Record, key: str) -> tuple:
    for i, role in enumerate(rec.roles):
        if job_key(role) == key:
            return i, role
    raise IndexError("That job isn't on your record any more")


def set_line_status(store, rec: mr.Record, jkey: str, lkey: str, status: str) -> None:
    if status not in LINE_STATUSES:
        raise ValueError(f"No such status: {status}")
    _, role = _role_by_key(rec, jkey)
    _find_line(role, lkey)
    w = _work(store)
    _job_work(w, role)["lines"][lkey] = status
    store.save_state(WORK, w)


def mark_job(store, rec: mr.Record, jkey: str, what: str) -> None:
    """`intro`: the line explanation was seen. `lines_fine`: every line not yet
    worked on is kept as it is. `more`: Anything else was asked. `done`: the
    person closed the job as it is (the tool never closes one itself)."""
    _, role = _role_by_key(rec, jkey)
    w = _work(store)
    j = _job_work(w, role)
    if what == "intro":
        w["intro_seen"] = True
    elif what == "lines_fine":
        for l in _lines(role):
            if j["lines"].get(line_key(l["text"]), "todo") == "todo":
                j["lines"][line_key(l["text"])] = "kept"
    elif what in ("more", "done"):
        j[what] = True
    else:
        raise ValueError(f"Nothing called {what}")
    store.save_state(WORK, w)


def add_more(store, rec: mr.Record, jkey: str, items: list) -> int:
    """Anything else from this job: each item becomes its own line to work
    through, in the person's words."""
    i, role = _role_by_key(rec, jkey)
    w = _work(store)
    j = _job_work(w, role)
    have = {line_key(l["text"]) for l in _lines(role)}
    added = 0
    for item in items:
        item = (item or "").strip()
        if item and line_key(item) not in have:
            role.recorded_bullets.append(item)
            have.add(line_key(item))
            j.setdefault("new", []).append(line_key(item))
            added += 1
    j["more"] = True
    store.save_state(WORK, w)
    return added


def line_answers(store, jkey: str, lkey: str) -> dict:
    return (store.load_state(LINE_ANSWERS) or {}).get(jkey, {}).get(lkey, {})


def save_line_answers(store, jkey: str, lkey: str, answers: dict) -> dict:
    a = store.load_state(LINE_ANSWERS) or {}
    mine = a.setdefault(jkey, {}).setdefault(lkey, {})
    for k, v in answers.items():
        if k not in LINE_FIELDS:
            raise ValueError(f"No such answer: {k}")
        mine[k] = (v or "").strip()
    mine.pop("draft", None)      # the answers changed, so any draft is out of date
    store.save_state(LINE_ANSWERS, a)
    return mine


STRONGER_SCHEMA = {
    "type": "object",
    "properties": {"line": {"type": "string"}},
    "required": ["line"],
    "additionalProperties": False,
}

STRONGER_PROMPT = """Rewrite one resume line using only what the person told us about it.

RULES
- One sentence, or two short ones. Under {max_words} words.
- Lead with what changed, then how. Start with a past-tense verb.
- No first person. No "responsible for", "helped", "assisted with".
- Use only facts in the original line and the answers below. If an answer is
  missing, leave that part out: never supply a result, a number, a team size
  or a tool the person didn't give.
- Every number must appear in the original line or the answers. Do not
  derive, round, combine or estimate one.
- If the answers say "we" or a team did it, say what this person did and
  credit the outcome to the team or the effort.
- Do not name the employer. Plain words. No "spearheaded", "leveraged",
  "utilized", no em dashes.

JOB: {title}
ORIGINAL LINE: {original}
WHAT WAS GOING ON: {situation}
WHAT THEY DID: {did}
WHAT CHANGED: {result}
NUMBER: {number}

Return JSON: {{"line": "..."}}"""


def write_stronger(store, rec: mr.Record, jkey: str, lkey: str) -> dict:
    """Draft a stronger version of one line from the person's answers and the
    original, held to them by the grounding check. With every question
    skipped, nothing is written. The draft is kept, so a refresh doesn't pay
    for a second one."""
    from . import bullets
    _, role = _role_by_key(rec, jkey)
    line = _find_line(role, lkey)
    a = store.load_state(LINE_ANSWERS) or {}
    mine = a.setdefault(jkey, {}).setdefault(lkey, {})
    if not any(mine.get(f) for f in LINE_FIELDS):
        return {"line": "", "empty": True}
    if mine.get("draft"):
        return {"line": mine["draft"]}
    source = " ".join([line["text"]] + [mine.get(f, "") for f in LINE_FIELDS])
    prompt = STRONGER_PROMPT.format(
        max_words=bullets.MAX_BULLET_WORDS, title=role.title, original=line["text"],
        **{f: mine.get(f) or "(not answered)" for f in LINE_FIELDS})
    messages = [{"role": "user", "content": prompt}]
    text = ""
    for _ in range(2):
        reply = llm.request_json(messages, 1024, "stronger line", schema=STRONGER_SCHEMA)
        text = (reply.get("line") or "").strip()
        invented = bullets.ungrounded(text, source)
        if text and not invented:
            mine["draft"] = text
            store.save_state(LINE_ANSWERS, a)
            return {"line": text}
        messages = messages + [
            {"role": "assistant", "content": f'{{"line": {text!r}}}'},
            {"role": "user", "content":
             f"These figures are not in the original line or the answers: "
             f"{', '.join(sorted(invented))}. Rewrite using only figures that appear there, or none."}]
    return {"line": "", "ungrounded": True}


def approve_line(store, rec: mr.Record, jkey: str, lkey: str, text: str) -> str:
    """Put the approved version on the record. The original is kept with it
    (as the story's title), and remembered as reworded so the same resume
    uploaded again doesn't bring it back beside the new one. A version no
    different from the original is just keeping it."""
    _, role = _role_by_key(rec, jkey)
    line = _find_line(role, lkey)
    text = (text or "").strip()
    if not text:
        raise ValueError("The line can't be blank")
    w = _work(store)
    j = _job_work(w, role)
    if importer._norm(text) == importer._norm(line["text"]):
        j["lines"][lkey] = "kept"
        store.save_state(WORK, w)
        return lkey
    mine = line_answers(store, jkey, lkey)
    result = "; ".join(x for x in (mine.get("result"), mine.get("number")) if x)
    if line["kind"] == RESUME:
        role.recorded_bullets.pop(line["index"])
        role.accomplishments.append(mr.Accomplishment(
            title=line["text"], problem=mine.get("situation", ""), actions=mine.get("did", ""),
            results=result, bullet=text))
        _remember(store, "lines", {"employer": role.employer, "title": role.title,
                                   "text": line["text"], "reworded": True})
    else:
        acc = role.accomplishments[line["index"]]
        acc.bullet = text
        for attr, f in (("problem", "situation"), ("actions", "did")):
            if mine.get(f):
                setattr(acc, attr, mine[f])
        if result:
            acc.results = result
    new = line_key(text)
    j["lines"].pop(lkey, None)
    j["lines"][new] = "stronger"
    if lkey in j.get("new", []):
        j["new"].append(new)
    store.save_state(WORK, w)
    a = store.load_state(LINE_ANSWERS) or {}
    if lkey in a.get(jkey, {}):
        a[jkey][new] = a[jkey].pop(lkey)
        store.save_state(LINE_ANSWERS, a)
    return new


def remove_line(store, rec: mr.Record, jkey: str, lkey: str) -> dict:
    i, role = _role_by_key(rec, jkey)
    line = _find_line(role, lkey)
    return delete_line(store, rec, i, role.label(), line["kind"], line["index"], line["text"])


# --------------------------------------------------------------------------
# Fixing a job's employer or title keeps everything already done on it

def edit_job_keep(store, rec: mr.Record, i: int, expect: str,
                  employer=None, title=None, dates=None) -> mr.Role:
    """edit_job, then carry the job's answers, line work and line drafts over
    to its new name, so a corrected title or employer loses nothing."""
    old = job(rec, i, expect)
    old_job, old_emp = job_key(old), emp_key(old)
    role = edit_job(rec, i, expect, employer, title, dates)
    new_job, new_emp = job_key(role), emp_key(role)
    if new_job != old_job:
        a = answers(store)
        _move(a["job"], old_job, new_job)
        if new_emp != old_emp and not any(emp_key(r) == old_emp for r in rec.roles):
            _move(a["emp"], old_emp, new_emp)
        store.save_state(ANSWERS, a)
        for name in (WORK, LINE_ANSWERS):
            state = store.load_state(name) or {}
            if old_job in state:
                _move(state, old_job, new_job)
                store.save_state(name, state)
    return role


def _move(d: dict, old: str, new: str) -> None:
    """Move d[old] to d[new]; where both exist, what's already under new wins."""
    if old not in d:
        return
    was = d.pop(old)
    if isinstance(was, dict) and isinstance(d.get(new), dict):
        for k, v in was.items():
            if isinstance(v, dict) and isinstance(d[new].get(k), dict):
                d[new][k] = {**v, **d[new][k]}
            else:
                d[new].setdefault(k, v)
    else:
        d.setdefault(new, was)
