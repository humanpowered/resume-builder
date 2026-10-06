"""
The skills inventory: what the person can do, in the words a job posting in
their field would use, each tied to what proves it.

It starts from their field rather than from their documents. `build` asks for
the standard skills a posting in that field draws from, then fills in as much
as the record supports: a skill their resume already names is theirs; one
their accomplishments show is filled in with a likely level for them to check;
one nothing shows waits for their answer. The person then answers, adjusts
levels, adds what the list missed, and can come back and change any of it.

Only a skill marked "yes" reaches a resume. "no" is kept too: it is a gap the
pipeline can see, and it stops the same skill being suggested again.

Why start from the field: people describe their work in their own words, and
an applicant tracking system matches the industry's words. A nurse writes
"kept the drips running"; the posting says "titration of vasoactive
infusions". A list drawn only from what someone wrote can't contain the term
they never thought to use.
"""
import json
from datetime import date

from . import health, llm
from . import record as mr

YES, NO, VERIFY = "yes", "no", "verify"
HAVE = (YES, NO, VERIFY)
# Where a skill came from, shown beside it so the person can see why it's there.
FROM_RESUME, FROM_RECORD, FROM_FIELD, FROM_YOU = "resume", "your record", "your field", "you"
# Categories that only say where a skill came from, not what it is; the next
# build moves a skill out of them into a real group.
UNSORTED = {"", "imported", "from the interview", "other"}


def is_pending(skill) -> bool:
    """On the list but not yet answered."""
    return skill.have == VERIFY


BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "category": {"type": "string"},
            "same_as": {"type": "string"},
            "named": {"type": "boolean"},
            "because": {"type": "array", "items": {"type": "string"}},
            "level": {"type": "string", "enum": ["", *mr.LEVELS]}},
            "required": ["name", "category", "same_as", "named", "because", "level"],
            "additionalProperties": False}},
    },
    "required": ["field", "skills"],
    "additionalProperties": False,
}

BUILD_PROMPT = """You are building a skills inventory for someone in this field:
{field}

STEP 1. List the skills a job posting in this field draws from, at this
person's level and the level above it: 40 to 80 of them. Include tools and
software, methods and techniques, domain and industry knowledge, regulations,
and leadership or people skills, whichever this field's postings ask for.
Include standard skills the record gives no sign of; the person will say
whether they have them. Leave out licences and certifications, which are kept
separately.

STEP 2. Fill in what the record below already shows, for each skill:
- "same_as": if the record's skills list (already_listed) has this skill under
  another wording, that exact already_listed name; otherwise "". Every
  already_listed skill should appear once, under its own name or a better one.
- "named": true only if the record itself uses this skill's name or an
  obvious form of it.
- "because": the exact accomplishment titles from the record that show the
  skill being used. Empty if none do. Never invent a title.
- "level": your best estimate from the record, or "" if it gives no grounds.
  Expert = led others in it or did it at depth for years; Advanced = used it
  independently with results; Working = used it regularly; Familiar = some
  exposure.

RULES
- Use the industry's standard term, the one an applicant tracking system
  matches. Where an acronym is common, give both: "Electronic Health Records,
  EHR".
- Group into the categories a hiring manager in this field would use (for a
  nurse: Clinical, Patient Safety, Systems, Leadership).
- "field": the field as you understood it, in a few words.

RECORD
{record}

already_listed: {listed}"""


def field_hint(rec: mr.Record) -> str:
    """What the record says about the person's field, for when they haven't."""
    parts = [rec.target.get("Industries", ""), rec.target.get("Titles", "")]
    parts += [f"{r.title} at {r.employer}" for r in rec.roles[:3]]
    return "; ".join(p for p in parts if p)


def build(rec: mr.Record, field: str = "") -> dict:
    """
    Fill the record's skills from the standard list for the person's field.
    Only blanks are filled: an answer, level or group the person set is never
    changed. Returns what happened, for the person to read.
    """
    titles = {a.title for _, a in rec.all_accomplishments()}
    text = mr.render(rec)
    reply = llm.request_json(
        [{"role": "user", "content": BUILD_PROMPT.format(
            field=field.strip() or f"(not given; work it out from the record: {field_hint(rec)})",
            record=text[:60000],
            listed=json.dumps([s.name for s in rec.skills]))}],
        12000, "skills", schema=BUILD_SCHEMA)
    plain = mr._squash(text)
    out = {"field": reply.get("field", "") or field, "added": [], "filled": [], "yours": []}
    for item in reply.get("skills") or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        because = [t for t in item.get("because") or [] if t in titles]
        level = item.get("level") if item.get("level") in mr.LEVELS else ""
        category = (item.get("category") or "").strip() or "Other"
        s = rec.skill(item.get("same_as") or "") or rec.skill(name)
        if s is not None:
            if _fill(s, category, level, because):
                out["filled"].append(s.name)
            continue
        if item.get("named") and _named_in(name, plain):
            s = mr.Skill(name=name, category=category, level=level, evidence=because,
                         have=YES, source=FROM_RESUME)
            out["yours"].append(name)
        else:
            s = mr.Skill(name=name, category=category, level=level if because else "",
                         evidence=because, have=VERIFY,
                         source=FROM_RECORD if because else FROM_FIELD)
            out["added"].append(name)
        rec.skills.append(s)
    link_both_ways(rec)
    out["dated"] = estimate_use(rec)
    return out


def link_both_ways(rec: mr.Record) -> int:
    """
    Keep each accomplishment's "Skills used" and each skill's evidence in step.

    A skill named on an accomplishment is the person's own word that they used
    it, so it joins the skills list as "yes" and points back to that work. A
    confirmed skill whose evidence names an accomplishment is added to that
    accomplishment's line. Unconfirmed and declined skills never are: the line
    is what the person did, not what the list suggests. Returns links added.
    """
    added = 0
    accs = {}
    for _, a in rec.all_accomplishments():
        accs.setdefault(a.title, []).append(a)
        for name in a.skill_names():
            s = rec.skill(name)
            if s is None:
                s = mr.Skill(name=name, category="From the interview", source=FROM_RECORD)
                rec.skills.append(s)
            if a.title not in s.evidence:
                s.evidence.append(a.title)
                added += 1
    for s in rec.skills:
        if s.have != YES:
            continue
        for title in s.evidence:
            for a in accs.get(title, []):
                added += a.add_skill(s.name)
    return added


def estimate_use(rec: mr.Record, today=None) -> list:
    """
    Years used and last used, for each skill the person has or may have, from
    the dates of the roles whose accomplishments prove it. Fills blanks only,
    marked "~" as an estimate, so a value the person typed is never touched.
    Overlapping roles are counted once. Returns the skills it dated.

    An estimate can undercount (a skill used in a role with no accomplishment
    recorded for it) but never overcounts, which is the safe direction.
    """
    if today is None:
        t = date.today()
        today = (t.year, t.month)
    role_of = {}
    for role, acc in rec.all_accomplishments():
        role_of.setdefault(acc.title, []).append(role)
    dated = []
    for s in rec.skills:
        if s.have == NO or not s.evidence or (s.years and s.last_used):
            continue
        months, latest = set(), None
        for title in s.evidence:
            for role in role_of.get(title, []):
                rng = health.parse_range(role.fields.get("Dates", ""), today=today)
                if not rng or health.months_between(*rng) < 0:
                    continue
                (y0, m0), (y1, m1) = rng
                months |= {y * 12 + m for y in range(y0, y1 + 1) for m in range(1, 13)
                           if (y0, m0) <= (y, m) <= (y1, m1)}
                latest = max(latest or rng[1], rng[1])
        if not months:
            continue
        if not s.years:
            n = round(len(months) / 12)
            s.years = f"~{n}" if n else "~<1"
        if not s.last_used:
            s.last_used = "~current" if latest == today else f"~{latest[0]}"
        dated.append(s.name)
    return dated


def _named_in(name: str, plain: str) -> bool:
    """The record uses the skill's name, or one of its forms: "Electronic
    Health Records, EHR" is named by either half."""
    return any(len(k) > 2 and k in plain
               for k in (mr._squash(part) for part in [name, *name.split(",")]))


def _fill(s, category, level, because) -> bool:
    """Fill what the person left blank on a skill already listed."""
    changed = False
    if (s.category or "").strip().lower() in UNSORTED and category.lower() not in UNSORTED:
        s.category, changed = category, True
    if not s.level and level and s.have != NO:
        s.level, changed = level, True
    for t in because:
        if t not in s.evidence:
            s.evidence.append(t)
            changed = True
    return changed


def accept_shown(rec: mr.Record) -> list:
    """Say yes to every unanswered skill the record shows, at its estimated
    level: the one-click answer when the estimates look right."""
    done = []
    for s in rec.skills:
        if s.have == VERIFY and s.evidence:
            s.have = YES
            done.append(s.name)
    return done


def set_skill(rec: mr.Record, name: str, have: str | None = None, level: str | None = None,
              category: str | None = None, rename: str | None = None,
              years: str | None = None, last_used: str | None = None):
    """Change one skill. LookupError if it isn't there; ValueError, with a
    message a person can read, if the change makes no sense."""
    s = rec.skill(name)
    if s is None:
        raise LookupError(f"No skill called {name!r}")
    if have is not None and have not in HAVE:
        raise ValueError(f"have must be one of {', '.join(HAVE)}")
    if level and level not in mr.LEVELS:
        raise ValueError(f"Level must be one of {', '.join(mr.LEVELS)}")
    if rename is not None and rename.strip() and rename.strip() != s.name:
        other = rec.skill(rename)
        if other is not None and other is not s:
            raise ValueError(f"{rename.strip()!r} is already on your list")
        s.name = rename.strip()
    if have is not None:
        s.have = have
    if level is not None:
        s.level = level
        if level and have is None:          # giving a level is saying yes
            s.have = YES
    if s.have == NO:
        s.level = ""
    if category is not None:
        s.category = category.strip() or "Other"
    if years is not None:
        s.years = years.strip()
    if last_used is not None:
        s.last_used = last_used.strip()
    return s


def add_skill(rec: mr.Record, name: str, category: str = "", level: str = ""):
    """A skill the list missed. Adding it is saying you have it."""
    name = (name or "").strip()
    if not name:
        raise ValueError("A skill needs a name")
    if level and level not in mr.LEVELS:
        raise ValueError(f"Level must be one of {', '.join(mr.LEVELS)}")
    if rec.skill(name) is not None:
        return set_skill(rec, name, have=YES, level=level or None, category=category or None)
    s = mr.Skill(name=name, category=category.strip() or "Other", level=level,
                 have=YES, source=FROM_YOU)
    rec.skills.append(s)
    return s


def remove_skill(rec: mr.Record, name: str) -> None:
    s = rec.skill(name)
    if s is None:
        raise LookupError(f"No skill called {name!r}")
    rec.skills.remove(s)


def verify(rec: mr.Record, ask, say=print) -> int:
    """
    Walk the unanswered skills, one question each. A level says yes at that
    level; y accepts the estimate shown; n records that you don't have it.
    Returns how many were settled.
    """
    settled = 0
    pending = [s for s in rec.skills if is_pending(s)]
    if not pending:
        say("  no skills waiting for an answer")
        return 0
    say(f"\n{len(pending)} skill(s) to answer. Reply with a level -- e (expert), "
        f"a (advanced), w (working), f (familiar) -- or y to accept the estimate "
        f"shown, or n if you don't have it. Enter skips; 'done' stops.")
    levels = {"e": "Expert", "a": "Advanced", "w": "Working", "f": "Familiar"}
    for s in pending:
        guess = f" [estimate: {s.level}]" if s.level else ""
        if s.years:
            guess += f" [{s.years.lstrip('~')} yrs, last used {s.last_used.lstrip('~')}]"
        why = f"  (shown by: {'; '.join(s.evidence)})" if s.evidence else ""
        answer = (ask(f"{s.category}: {s.name}?{guess}{why}") or "").strip().lower()
        if answer in ("done", "stop"):
            break
        if not answer:
            continue
        if answer in ("n", "no"):
            set_skill(rec, s.name, have=NO)
        elif answer in ("y", "yes"):
            set_skill(rec, s.name, have=YES)
        elif answer[:1] in levels:
            set_skill(rec, s.name, have=YES, level=levels[answer[:1]])
        else:
            continue
        settled += 1
    return settled
