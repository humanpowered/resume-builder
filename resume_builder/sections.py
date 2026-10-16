"""
The guided build after the jobs: education and licences, skills, and what
the person did outside paid work.

Everything still lives in the record the pipeline reads: education and
certifications as one line each, skills as Skill entries, and the rest in
the record's optional sections. What the page needs on top (the fields a
line was built from, which sections are done, which target titles the
skills list was built for) is kept in the store's state.
"""
import re
from datetime import date

from . import health, importer, journey, skills
from . import record as mr

SECTIONS = ("edu", "skills", "beyond", "summary")
DONE = "journey.sections"
EDU_FIELDS = "journey.edufields"
BUILT_FOR = "journey.skillsbuilt"
ALSO = "journey.skillnames"       # other names for a skill: {squashed name: [names]}

# ---------------------------------------------------------------- shared


def done(store) -> dict:
    d = store.load_state(DONE) or {}
    return {k: bool(d.get(k)) for k in SECTIONS}


def mark(store, name: str, value: bool = True) -> None:
    if name not in SECTIONS:
        raise ValueError(f"No such section: {name}")
    d = store.load_state(DONE) or {}
    d[name] = bool(value)
    store.save_state(DONE, d)


def _remember(store, section: str, text: str) -> None:
    d = journey.deleted(store)
    d.setdefault("items", []).append({"section": section, "text": text})
    store.save_state(journey.DELETED, d)


def _forget(store, section: str, text: str) -> None:
    d = journey.deleted(store)
    key = importer._norm(text)
    d["items"] = [x for x in d.get("items", [])
                  if not (x["section"] == section and importer._norm(x["text"]) == key)]
    store.save_state(journey.DELETED, d)


def leave_out_deleted(store, incoming: mr.Record) -> int:
    """Take education, licence and other lines the person deleted out of an
    upload, so a new file never quietly brings them back."""
    gone = {(x["section"], importer._norm(x["text"]))
            for x in journey.deleted(store).get("items", [])}
    if not gone:
        return 0
    n = 0
    for section in ("education", "certifications"):
        lines = getattr(incoming, section)
        keep = [l for l in lines if (section, importer._norm(l)) not in gone]
        n += len(lines) - len(keep)
        setattr(incoming, section, keep)
    for key, lines in incoming.extras.items():
        keep = [l for l in lines if (key, importer._norm(l)) not in gone]
        n += len(lines) - len(keep)
        incoming.extras[key] = keep
    return n


def _lines_of(rec: mr.Record, section: str) -> list:
    if section in ("education", "certifications"):
        return getattr(rec, section)
    if section in mr.EXTRA_KEYS:
        return rec.extras.setdefault(section, [])
    raise ValueError(f"No such section: {section}")


def _at(lines: list, i: int, expect: str) -> str:
    if not 0 <= i < len(lines):
        raise IndexError("That entry isn't on your record any more")
    if expect is not None and lines[i] != expect:
        raise journey.Stale()
    return lines[i]


def delete_item(store, rec: mr.Record, section: str, i: int, expect: str) -> dict:
    lines = _lines_of(rec, section)
    text = _at(lines, i, expect)
    lines.pop(i)
    _remember(store, section, text)
    return {"section": section, "index": i, "text": text}


def restore_item(store, rec: mr.Record, section: str, i: int, text: str) -> None:
    lines = _lines_of(rec, section)
    lines.insert(max(0, min(i, len(lines))), text)
    _forget(store, section, text)


def _join(*parts) -> str:
    return ", ".join(p.strip() for p in parts if p and p.strip())


# ---------------------------------------------------------------- education

YEAR = re.compile(r"\b(19|20)\d\d\b")
SCHOOL = re.compile(r"universit|college|institut|school|academy|polytechnic|conservatory", re.I)
QUALIFICATION = re.compile(r"degree|diploma|certificate|associate|bachelor|master|doctor|"
                           r"\bGED\b|high school", re.I)
DEGREE_FIELDS = ("degree", "field", "school", "year", "studying", "gpa", "extra")
LICENCE_FIELDS = ("name", "issuer", "year", "current", "expires", "multistate")
TICKS = ("studying", "multistate")         # yes/no fields, kept as True or False
GPA = re.compile(r"\bGPA:?\s*(\d(?:\.\d+)?)(?:\s*(?:/|out of)\s*(\d(?:\.\d+)?))?", re.I)
EXPIRES = re.compile(r",?\s*\bexpires?:?\s+([A-Za-z]{3,9}\.?\s+\d{4}|\d{1,2}/\d{4}|\d{4})", re.I)
MULTISTATE = re.compile(r",?\s*\bmultistate(?: \(compact\))?(?: licen[cs]e)?", re.I)
CURRENT = ("Yes", "No, it lapsed", "Not sure")


def _parts(text: str) -> list:
    out = []
    for p in re.split(r"[,|·;]| – | - ", text):
        p = p.strip(" .-–")
        p = re.sub(r"^\((.*)\)$", r"\1", p.replace("()", "")).strip()
        if p and p != "()":
            out.append(p)
    return out


def _guess_degree(line: str) -> dict:
    """Best guess at a degree line's parts, for the person to correct. The
    line itself is only rewritten when they save."""
    gpa = GPA.search(line)
    line = GPA.sub("", line) if gpa else line
    years = [m.group(0) for m in YEAR.finditer(line)]
    year = years[-1] if years else ""
    parts = [p for p in _parts(YEAR.sub("", line)) if p.lower() not in ("expected", "in")]
    out = {k: "" for k in DEGREE_FIELDS}
    out["gpa"] = gpa.group(0).split(None, 1)[-1].lstrip(": ") if gpa else ""
    out["year"] = year or ""
    out["studying"] = bool(re.search(r"expected|in progress|current", line, re.I))
    for p in parts:
        if not out["school"] and SCHOOL.search(p) and not QUALIFICATION.search(p):
            out["school"] = p
        elif not out["degree"] and (mr.DEGREE.search(p) or QUALIFICATION.search(p)):
            # "BS in Business" carries the field too
            m = re.match(r"(.+)\s+in\s+(.+)", p)
            if m and not out["field"] and (mr.DEGREE.search(m.group(1)) or QUALIFICATION.search(m.group(1))):
                out["degree"], out["field"] = m.group(1), m.group(2)
            else:
                out["degree"] = p
        elif not out["field"]:
            out["field"] = p
        else:
            out["extra"] = _join(out["extra"], p)
    if not out["degree"] and out["field"]:
        out["degree"], out["field"] = out["field"], ""
    return out


def _guess_licence(line: str) -> dict:
    exp = EXPIRES.search(line)
    multistate = bool(MULTISTATE.search(line))
    line = MULTISTATE.sub("", EXPIRES.sub("", line))
    years = [m.group(0) for m in YEAR.finditer(line)]
    parts = _parts(YEAR.sub("", line))
    lapsed = bool(re.search(r"lapsed|expired", line, re.I))
    parts = [p for p in parts if not re.fullmatch(r"(?i)lapsed|expired", p)]
    return {"name": parts[0] if parts else line, "issuer": _join(*parts[1:]),
            "year": years[0] if years else "", "current": "No, it lapsed" if lapsed else "",
            "expires": exp.group(1) if exp else "", "multistate": multistate}


def gpa_value(text: str):
    """A GPA as a share of its scale's top (3.6 of 4.0 -> 0.9), or None. A
    figure with no scale is read as out of 4."""
    m = re.fullmatch(r"\s*(\d(?:\.\d+)?)\s*(?:(?:/|out of)\s*(\d(?:\.\d+)?))?\s*", text or "")
    if not m:
        return None
    top = float(m.group(2) or 4)
    return float(m.group(1)) / top if top and float(m.group(1)) <= top else None


def _degree_line(f: dict, show_year: bool = True, show_gpa: bool = True) -> str:
    """The degree as one line. The record keeps everything; a resume can
    leave the year or the GPA off."""
    year = (f.get("year") or "").strip() if show_year or f.get("studying") else ""
    if year and f.get("studying"):
        year = f"expected {year}"
    elif f.get("studying"):
        year = "in progress"
    gpa = (f.get("gpa") or "").strip()
    line = _join(f.get("degree", ""), f.get("field", ""), f.get("school", ""), year,
                 f"GPA {gpa}" if gpa and show_gpa else "")
    extra = (f.get("extra") or "").strip().rstrip(".")
    return f"{line}. {extra[:1].upper()}{extra[1:]}" if line and extra else line or extra


def _licence_line(f: dict) -> str:
    lapsed = f.get("current") == CURRENT[1]
    expires = (f.get("expires") or "").strip()
    line = _join(f.get("name", ""), f.get("issuer", ""), f.get("year", ""),
                 "multistate (compact) licence" if f.get("multistate") else "",
                 f"expires {expires}" if expires and not lapsed else "")
    return f"{line} (lapsed)" if line and lapsed else line


def education_view(store, rec: mr.Record) -> list:
    """Each degree, course, licence and certification, with the fields its
    edit form shows: what the person saved, or a guess from the line."""
    kept = store.load_state(EDU_FIELDS) or {}
    out = []
    for section, kind in (("education", "degree"), ("certifications", "licence")):
        for i, line in enumerate(getattr(rec, section)):
            f = kept.get(f"{section}|{importer._norm(line)}")
            f = f or (_guess_degree(line) if kind == "degree" else _guess_licence(line))
            out.append({"section": section, "kind": kind, "index": i, "line": line,
                        "fields": f})
    return out


def save_education(store, rec: mr.Record, section: str, i: int, expect: str, fields: dict) -> int:
    """Save one entry from its fields; i < 0 adds a new one. The line on the
    record is rebuilt from the fields, and the fields are kept for next time.
    A licence number is never asked for or kept."""
    if section not in ("education", "certifications"):
        raise ValueError(f"No such section: {section}")
    names = DEGREE_FIELDS if section == "education" else LICENCE_FIELDS
    f = {k: (bool(fields.get(k)) if k in TICKS else str(fields.get(k) or "").strip()) for k in names}
    if section == "certifications" and f["current"] not in ("",) + CURRENT:
        raise ValueError("Still current? Pick Yes, No or Not sure")
    line = _degree_line(f) if section == "education" else _licence_line(f)
    if not line:
        raise ValueError("Fill in at least the name")
    lines = getattr(rec, section)
    kept = store.load_state(EDU_FIELDS) or {}
    if i < 0:
        lines.append(line)
        i = len(lines) - 1
    else:
        old = _at(lines, i, expect)
        kept.pop(f"{section}|{importer._norm(old)}", None)
        lines[i] = line
    kept[f"{section}|{importer._norm(line)}"] = f
    store.save_state(EDU_FIELDS, kept)
    return i


# ---------------------------------------------------------------- skills

def _role_lines(rec: mr.Record) -> list:
    """(role, text) for every line on every job."""
    out = []
    for role in rec.roles:
        out += [(role, b) for b in role.recorded_bullets]
        out += [(role, journey.line_text(a)) for a in role.accomplishments if not a.is_empty()]
    return out


def named_text(rec: mr.Record) -> str:
    """What the person themselves wrote that can name a skill: their lines,
    their resume's skills list, education and training."""
    parts = [t for _, t in _role_lines(rec)]
    parts += [s.name for s in rec.skills if s.source in ("resume", skills.FROM_YOU)]
    parts += rec.education + rec.certifications + rec.extras.get("training", [])
    return "\n".join(parts)


def other_names(store) -> dict:
    return store.load_state(ALSO) or {}


def _shown_by(rec: mr.Record, s: mr.Skill, lines: list, today, also: dict) -> dict:
    names = [s.name, *also.get(mr._squash(s.name), [])]
    hits = [(r, t) for r, t in lines if skills.names_in(names, t)]
    out = {"lines": [t for _, t in hits][:5], "shown": "", "use": ""}
    if hits:
        emps = []
        for r, _ in hits:
            if r.employer not in emps:
                emps.append(r.employer)
        where = emps[0] if len(emps) == 1 else ", ".join(emps[:-1]) + " and " + emps[-1]
        out["shown"] = (f"Shown by: “{hits[0][1]}” at {hits[0][0].employer}" if len(hits) == 1
                        else f"Shown by: {len(hits)} lines at {where}")
        ranges = []
        for r in {id(r): r for r, _ in hits}.values():
            rng = health.parse_range(r.fields.get("Dates", ""), today=today)
            if rng and health.months_between(*rng) >= 0:
                ranges.append(rng)
        if ranges:
            n = round(journey._months(ranges) / 12)
            last = max(e for _, e in ranges)
            out["use"] = (f"About {n} year{'s' if n != 1 else ''}" if n else "Under a year") + \
                (" · used now" if last == today else f" · last used {last[0]}")
    if s.years and not s.years.startswith("~"):
        typed = f"{s.years} year{'s' if s.years not in ('1',) else ''}"
        out["use"] = typed + (f" · last used {s.last_used}" if s.last_used and not s.last_used.startswith("~") else "")
    return out


def skills_view(store, rec: mr.Record) -> dict:
    t = date.today()
    today = (t.year, t.month)
    lines = _role_lines(rec)
    also = other_names(store)
    targets = journey.targets_view(store, rec)["titles"]
    current = next((r.title for r in rec.roles if not r.outside()), "")
    basis = targets or ([current] if current else [])
    groups, order = {}, []
    for s in rec.skills:
        cat = s.category if (s.category or "").strip().lower() not in skills.UNSORTED else ""
        cat = cat or ("Languages" if mr.is_language(s.name) else
                      "Added by you" if s.source == skills.FROM_YOU else "From your resume")
        if mr.is_language(s.name, cat):
            cat = "Languages"
        if cat not in groups:
            groups[cat] = []
            order.append(cat)
        sb = _shown_by(rec, s, lines, today, also)
        if s.have == skills.YES:
            state = "yes"
            tag = {"resume": "From your resume", skills.FROM_YOU: "Added by you"}.get(s.source, "Yours")
            shown = sb["shown"] or ("From your resume's skills list. No line shows it yet."
                                    if s.source == "resume" else "")
        elif s.have == skills.NO:
            state, tag, shown = "no", "Not yours", ""
        else:
            state, tag = "ask", "Suggested"
            shown = ("Your record suggests it" if s.evidence else
                     f"Common for {_and(basis)}" if basis else "Common in your field")
        groups[cat].append({"name": s.name, "state": state, "tag": tag, "shown": shown,
                            "use": sb["use"] if state == "yes" else "", "level": s.level,
                            "levels": list(mr.levels_for(s.name, cat)),
                            "years": s.years.lstrip("~"), "last_used": s.last_used.lstrip("~")})
    mine = [s for s in rec.skills if s.have == skills.YES]
    built = store.load_state(BUILT_FOR) or []
    return {"groups": [{"cat": c, "items": groups[c]} for c in order],
            "levels": list(mr.LEVELS), "basis": basis, "current": current,
            "built_for": built, "needs_build": bool(_to_build(store, rec)),
            "mine": len(mine), "unrated": sum(1 for s in mine if not s.level),
            "unanswered": sum(1 for s in rec.skills if s.have == skills.VERIFY)}


def _and(items: list) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _to_build(store, rec: mr.Record) -> list:
    """Target titles the list hasn't been built for yet; the current role
    when there are no targets. Changing targets only ever adds."""
    targets = journey.targets_view(store, rec)["titles"]
    current = next((r.title for r in rec.roles if not r.outside()), "")
    want = targets or ([current] if current else [])
    built = {mr._squash(t) for t in store.load_state(BUILT_FOR) or []}
    if not want and not built:
        return ["(your record)"]
    return [t for t in want if mr._squash(t) not in built]


def build_skills(store, rec: mr.Record) -> dict:
    """One paid call: the skills postings for the new target titles ask for,
    merged into the list. Never sets a level; never changes an answer."""
    todo = _to_build(store, rec)
    if not todo:
        return {"added": [], "yours": []}
    field = "" if todo == ["(your record)"] else "; ".join(todo)
    out = skills.build(rec, field, levels=False, named_text=named_text(rec))
    also = other_names(store)
    for mine, other in out.get("also", []):
        names = also.setdefault(mr._squash(mine), [])
        if mr._squash(other) not in {mr._squash(n) for n in names}:
            names.append(other)
    store.save_state(ALSO, also)
    built = store.load_state(BUILT_FOR) or []
    store.save_state(BUILT_FOR, built + todo)
    return out


def set_skill(rec: mr.Record, name: str, have=None, level=None, years=None, last_used=None):
    return skills.set_skill(rec, name, have=have, level=level, years=years, last_used=last_used)


# ---------------------------------------------------------------- beyond paid work

# Tiles: what each asks for and where it goes on the record.
TILES = {
    "award": ("awards", "Award"),
    "membership": ("memberships", "Board or membership"),
    "training": ("training", "Training or course"),
    "talk": ("publications", "Talk or publication"),
    "break": ("career_breaks", "Career break"),
}
BEYOND_SECTIONS = ("awards", "memberships", "training", "publications", "career_breaks",
                   "volunteer", "projects")
KIND_NAMES = {"awards": "Award", "memberships": "Board or membership",
              "training": "Training or course", "publications": "Talk or publication",
              "career_breaks": "Career break", "volunteer": "Volunteering",
              "projects": "Project"}
JOB_LIKE = {"volunteer": "Volunteer", "side": "Personal or side project"}


def tile_line(tile: str, f: dict) -> str:
    g = lambda k: (f.get(k) or "").strip()   # noqa: E731
    if tile == "award":
        line = _join(g("name"), g("by"), g("year"))
        return f"{line}: {g('for')}" if line and g("for") else line
    if tile == "membership":
        return _join(g("role"), g("org"), g("years"))
    if tile in ("training", "talk"):
        return _join(g("name"), g("where"), g("year"))
    if tile == "break":
        span = " to ".join(x for x in (g("from"), g("to")) if x)
        return f"Career break, {span}: {g('reason')}" if g("reason") else \
            (f"Career break, {span}" if span else "")
    raise ValueError(f"No such kind: {tile}")


def add_tile(rec: mr.Record, tile: str, fields: dict) -> str:
    if tile not in TILES:
        raise ValueError(f"No such kind: {tile}")
    line = tile_line(tile, fields)
    if not line:
        raise ValueError("Fill in at least the first box")
    rec.extras.setdefault(TILES[tile][0], []).append(line)
    return line


def edit_item(rec: mr.Record, section: str, i: int, expect: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        raise ValueError("An entry can't be blank. Delete it instead")
    lines = _lines_of(rec, section)
    _at(lines, i, expect)
    lines[i] = text


def add_job_like(rec: mr.Record, kind: str, title: str, org: str, dates: str) -> int:
    """Volunteering or a side project, kept as a job outside paid work so it
    gets the same line work as a job."""
    if kind not in JOB_LIKE:
        raise ValueError(f"No such kind: {kind}")
    if not (title or "").strip() or not (org or "").strip():
        raise ValueError("Fill in what you did and where")
    i = journey.add_job(rec, org, title, dates)
    rec.roles[i].fields["Employment type"] = JOB_LIKE[kind]
    return i


def beyond_view(rec: mr.Record) -> dict:
    items = [{"section": k, "index": i, "kind": KIND_NAMES[k], "text": line}
             for k in BEYOND_SECTIONS for i, line in enumerate(rec.extras.get(k, []))]
    jobs = [{"index": i, "kind": "Volunteering" if r.fields.get("Employment type") == "Volunteer"
             else r.fields.get("Employment type", ""), "title": r.title, "employer": r.employer,
             "dates": journey.clean(r.fields.get("Dates", "")), "key": journey.job_key(r)}
            for i, r in enumerate(rec.roles) if r.outside()]
    return {"items": items, "jobs": jobs}
