"""
Sample resumes from the record, kept inside this tool: a free draft after
the first jobs are done, a general sample for the target title, and a few
free samples written for a job posting the person pastes.

Nothing here writes a file or touches the job-application pipeline. A sample
lives in the store's state and on the page, and the person decides whether
to print or copy it.

The model chooses; it never writes. It is given the record's lines and
skills by id and returns ids, so every word on a sample is a word already on
the record, and a sample can't claim anything the person didn't say.
"""
import re
from datetime import datetime, timezone

from . import health, journey, llm, posting as postings, sections, skills
from . import record as mr

SAMPLES = "journey.samples"
PREFS = "journey.resumeprefs"
FREE_TAILORED = 3
MAX_LINES, MAX_PER_JOB, MAX_SKILLS = 18, 6, 12
MAX_POSTING = 15000
# Jobs that ended this many years ago or more go under "Earlier experience"
# as a title and employer, and degrees this old print without their year,
# unless the person asks for them. Recruiters weigh the last 10 to 15 years,
# and old dates invite guesses about age.
OLD_YEARS = 15
RECENT_GPA_YEARS = 3
GOOD_GPA = 0.75                   # 3.0 on a 4.0 scale
EXTRA_HEADINGS = (("awards", "Awards"), ("publications", "Talks and publications"),
                  ("memberships", "Boards and memberships"), ("training", "Training"))
# A licence that lets someone do the job goes above their experience, where
# hiring managers in licensed fields look for it first.
LICENSED = re.compile(r"licen[cs]|registered nurse|nurse practitioner|paramedic|journeyman|"
                      r"master (?:electrician|plumber)|bar admission|teaching (?:certificate|credential)", re.I)
LICENSED_CODES = re.compile(r"\b(?:RN|LPN|LVN|APRN|CNA|EMT|CDL|CPA|PE|BLS|ACLS|PALS|NCLEX)\b")


def prefs(store) -> dict:
    """What the person wants on every resume: their phone number (yes,
    unless they say not), and graduation years over 15 years old (no,
    unless they say so)."""
    return {"phone": True, "old_years": False, **(store.load_state(PREFS) or {})}


def save_prefs(store, phone=None, old_years=None) -> dict:
    p = prefs(store)
    for k, v in (("phone", phone), ("old_years", old_years)):
        if v is not None:
            p[k] = bool(v)
    store.save_state(PREFS, p)
    return p


# ---------------------------------------------------------------- what a sample is built from

def _paid(rec: mr.Record) -> list:
    """Paid jobs in the order the person sees them: employers newest first,
    each employer's titles newest first."""
    return [(i, rec.roles[i]) for i in journey.shown_order(rec) if not rec.roles[i].outside()]


def _ended_long_ago(role: mr.Role, now) -> bool:
    rng = health.parse_range(role.fields.get("Dates", ""), today=now)
    return bool(rng) and rng[1][0] <= now[0] - OLD_YEARS


def _split(rec: mr.Record, now=None) -> tuple:
    """(recent jobs, jobs that ended long ago). When every job is old, as for
    someone returning after a long break, they all stay in full."""
    now = now or journey.today()
    paid = _paid(rec)
    old = [(i, r) for i, r in paid if _ended_long_ago(r, now)]
    if len(old) == len(paid):
        return paid, []
    gone = {i for i, _ in old}
    return [(i, r) for i, r in paid if i not in gone], old


def _languages(rec: mr.Record) -> list:
    return [s for s in rec.skills if s.have == skills.YES and mr.is_language(s.name, s.category)]


def _pool(rec: mr.Record) -> tuple:
    """Every line and every skill the person has, each with an id. Jobs that
    ended long ago, and languages, have places of their own on the page."""
    lines = {}
    for i, role in _split(rec)[0]:
        for n, l in enumerate(journey._lines(role)):
            lines[f"j{i}l{n}"] = {"job": i, "text": l["text"]}
    langs = {id(s) for s in _languages(rec)}
    mine = [s for s in rec.skills if s.have == skills.YES and id(s) not in langs]
    return lines, {f"s{n}": s.name for n, s in enumerate(mine)}


def _contact(rec: mr.Record, p: dict) -> str:
    c = {k: journey.clean(rec.contact.get(k, "")) for k in journey.CONTACT_FIELDS}
    return " · ".join(x for x in (c["Location"], c["Email"], c["Telephone"] if p["phone"] else "",
                                  c["Linkedin"], c["Website"]) if x)


def _year(text: str):
    m = sections.YEAR.search(text or "")
    return int(m.group(0)) if m else None


def _education(store, rec: mr.Record, p: dict, student: bool, now) -> tuple:
    """(degrees, licences) as they print. The record keeps every year and
    GPA; a resume leaves off a graduation year more than 15 years old, and
    shows a GPA of 3.0 or more only while the person is a student or a
    recent graduate."""
    degrees, licences = [], []
    for e in sections.education_view(store, rec):
        if e["kind"] != "degree":
            licences.append(e["line"])
            continue
        f, year = e["fields"], _year(e["fields"].get("year", ""))
        show_year = p["old_years"] or f.get("studying") or not year or year > now[0] - OLD_YEARS
        gpa = sections.gpa_value(f.get("gpa", ""))
        recent = f.get("studying") or (year and year >= now[0] - RECENT_GPA_YEARS)
        show_gpa = gpa is not None and gpa >= GOOD_GPA and (student or recent)
        # Only a line that loses something is rebuilt from its parts; the
        # rest print exactly as the record has them.
        rebuild = (year and not show_year) or ((f.get("gpa") or "").strip() and not show_gpa)
        degrees.append(sections._degree_line(f, show_year, show_gpa) if rebuild else e["line"])
    return degrees, licences


def _extras(rec: mr.Record) -> list:
    out = []
    if rec.extras.get("projects"):
        out.append({"key": "projects", "heading": "Projects", "lines": list(rec.extras["projects"])})
    volunteer = [f"{r.title}, {r.employer}" + (f" · {health.tidy_range(journey.clean(r.fields.get('Dates', '')))}"
                                              if r.fields.get("Dates") else "")
                 for r in rec.roles if r.outside()]
    if volunteer:
        out.append({"key": "outside", "heading": "Volunteering and projects", "lines": volunteer})
    out += [{"key": k, "heading": h, "lines": list(rec.extras.get(k, []))}
            for k, h in EXTRA_HEADINGS if rec.extras.get(k)]
    langs = list(rec.extras.get("languages", []))
    named = " ".join(langs).lower()
    spoken = [f"{s.name} ({s.level})" if s.level else s.name
              for s in _languages(rec) if s.name.lower() not in named]
    if spoken:
        langs.append(" · ".join(spoken))
    if langs:
        out.append({"key": "languages", "heading": "Languages", "lines": langs})
    if rec.extras.get("clearance"):
        out.append({"key": "clearance", "heading": "Security clearance", "lines": list(rec.extras["clearance"])})
    return out


def _dress(store, rec: mr.Record, res: dict) -> dict:
    """Everything on a sample except the lines chosen for each job, from the
    record as it is now and the person's choices, in the order that suits
    their career stage. Run again on every sample each time the page loads,
    so a corrected phone number or degree shows on resumes already made."""
    now, p = journey.today(), prefs(store)
    stage = journey.stage(rec)
    student = stage == journey.STUDENT
    degrees, licences = _education(store, rec, p, student, now)
    licensed = any(LICENSED.search(l) or LICENSED_CODES.search(l) for l in licences)
    langs = {s.name for s in _languages(rec)}
    # The line about each employer: what it does and how big it is, as the
    # person described it. Left off when they skipped it.
    about = {}
    for r in rec.roles:
        line = journey.clean(r.fields.get(journey.COMPANY, ""))
        if line:
            about.setdefault(mr._squash(r.employer), line)
    employers = [{**e, "about": about.get(mr._squash(e["employer"]), "")} for e in res.get("employers", [])]
    res = {**res, "employers": employers, "name": journey.clean(rec.contact.get("Name", "")), "contact": _contact(rec, p),
           "summary": rec.summary, "skills": [s for s in res.get("skills", []) if s not in langs],
           "stage": stage, "prefs": p}
    res.setdefault("earlier", [])
    extras = _extras(rec)
    res["extras"] = extras

    def block(key, heading, lines):
        return [{"key": key, "heading": heading, "lines": lines}] if lines else []
    if licensed and not student:
        edu = block("credentials", "Licences and certifications", licences) + block("education", "Education", degrees)
    else:
        both = "Education and licences" if degrees and licences else "Education" if degrees else "Licences and certifications"
        edu = block("education", both, degrees + licences)
    experience = [{"key": "experience", "heading": "Experience"}] + block("earlier", "Earlier experience", res["earlier"])
    skills_ = [{"key": "skills", "heading": "Skills"}] if res["skills"] else []
    summary = [{"key": "summary", "heading": "Summary"}] if res["summary"] else []
    if student:
        # Education first; projects and volunteering carry weight next to a
        # short work history, so they come before skills.
        early = [x for x in extras if x["key"] in ("projects", "outside")]
        rest = [x for x in extras if x["key"] not in ("projects", "outside")]
        order = summary + edu + experience + early + skills_ + rest
    elif licensed:
        order = summary + edu[:1] + experience + edu[1:] + skills_ + extras
    else:
        order = summary + experience + edu + skills_ + extras
    res["sections"] = order
    res["education"] = degrees + licences
    return res


def _layout(store, rec: mr.Record, chosen: dict, skill_names: list, kind: str, headline: str) -> dict:
    """The resume as the page shows it: titles at one employer stacked under
    one heading, newest first, each with the lines chosen for it, and jobs
    that ended long ago as one line each under Earlier experience."""
    recent, old = _split(rec)
    employers, at = [], {}
    for i, role in recent:
        key = mr._squash(role.employer)
        if key not in at:
            at[key] = {"employer": role.employer, "roles": []}
            employers.append(at[key])
        at[key]["roles"].append({"title": role.title,
                                 "dates": health.tidy_range(journey.clean(role.fields.get("Dates", ""))),
                                 "lines": chosen.get(i, [])})
    return _dress(store, rec, {
        "kind": kind, "headline": headline, "employers": employers, "skills": skill_names,
        "earlier": [f"{r.title}, {r.employer}" for _, r in old],
        "made": datetime.now(timezone.utc).isoformat()})


def _headline(store, rec: mr.Record) -> str:
    targets = journey.targets_view(store, rec)["titles"]
    return targets[0] if targets else next((r.title for _, r in _paid(rec)), "")


# ---------------------------------------------------------------- the free draft

def draft(store, rec: mr.Record) -> dict:
    """No model call: the lines as they stand, ones with figures first. Jobs
    worked through carry their stronger lines; the rest read as the resume
    had them."""
    chosen = {}
    for rank, (i, role) in enumerate(_split(rec)[0]):
        lines = sorted(journey._lines(role), key=lambda l: (journey.line_rank(l["text"]) * -1, l["index"]))
        chosen[i] = [l["text"] for l in lines][:5 if rank < 3 else 3]
    mine = list(_pool(rec)[1].values())[:MAX_SKILLS]
    return _layout(store, rec, chosen, mine, "draft", _headline(store, rec))


# ---------------------------------------------------------------- choosing with the model

SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {"type": "array", "items": {"type": "object", "properties": {
            "job": {"type": "integer"}, "lines": {"type": "array", "items": {"type": "string"}}},
            "required": ["job", "lines"], "additionalProperties": False}},
        "skills": {"type": "array", "items": {"type": "string"}},
        "posting_title": {"type": "string"},
        "posting_company": {"type": "string"},
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["jobs", "skills", "posting_title", "posting_company", "gaps"],
    "additionalProperties": False,
}

PROMPT = """You are choosing what goes on a two-page resume {purpose}.
You choose; you never write. Pick line ids and skill ids from the lists below.

RULES
- At most {max_lines} lines in total, and at most {per_job} for any one job.
  Recent jobs get more.
- Prefer lines that show a result or a number, and lines that match {match}.
- List each job's lines from strongest to weakest for this purpose.
- Skills: up to {max_skills} ids, most relevant first.
- posting_title and posting_company: from the posting, or "" if there is none.
- gaps: up to 4 things the posting asks for that no line or skill below shows,
  a few words each. [] if there is no posting or nothing is missing.

Return JSON: {{"jobs": [{{"job": 0, "lines": ["j0l2", "j0l0"]}}], "skills": ["s3"],
"posting_title": "", "posting_company": "", "gaps": []}}

{what}

LINES
{lines}

SKILLS
{skills}"""


def _choose(store, rec: mr.Record, purpose: str, match: str, what: str) -> tuple:
    lines, skill_ids = _pool(rec)
    if not lines:
        raise ValueError("Your record has no lines yet, so there's nothing to choose from")
    roles = dict(_split(rec)[0])
    listing = "\n".join(f"[{k}] ({roles[v['job']].title}, {roles[v['job']].employer}, "
                        f"{journey.clean(roles[v['job']].fields.get('Dates', ''))}) {v['text']}"
                        for k, v in lines.items())
    out = llm.request_json([{"role": "user", "content": PROMPT.format(
        purpose=purpose, match=match, what=what, max_lines=MAX_LINES, per_job=MAX_PER_JOB,
        max_skills=MAX_SKILLS, lines=listing,
        skills="\n".join(f"[{k}] {v}" for k, v in skill_ids.items()) or "(none)")}],
        3000, "sample", schema=SCHEMA)
    # Only ids that exist count, each once, within the limits, under its own job.
    chosen, used, total = {}, set(), 0
    for j in out.get("jobs") or []:
        for lid in j.get("lines") or []:
            l = lines.get(lid)
            if not l or lid in used or total >= MAX_LINES or len(chosen.get(l["job"], [])) >= MAX_PER_JOB:
                continue
            used.add(lid)
            total += 1
            chosen.setdefault(l["job"], []).append(l["text"])
    picked = []
    for sid in out.get("skills") or []:
        if sid in skill_ids and skill_ids[sid] not in picked and len(picked) < MAX_SKILLS:
            picked.append(skill_ids[sid])
    return chosen, picked, out


def _state(store) -> dict:
    st = store.load_state(SAMPLES) or {}
    st.setdefault("general", None)
    st.setdefault("tailored", [])
    return st


def general(store, rec: mr.Record) -> dict:
    """One paid call: a general resume for the first target title."""
    title = _headline(store, rec)
    chosen, picked, _ = _choose(store, rec, f"for {title or 'their next job'}",
                                f"what {title} job postings usually ask for" if title else "their field",
                                f"TARGET\n{title or '(none set)'}")
    res = _layout(store, rec, chosen, picked, "general", title)
    st = _state(store)
    st["general"] = res
    store.save_state(SAMPLES, st)
    return res


def tailored(store, rec: mr.Record, posting: str) -> dict:
    """One paid call: a resume for one posting. A few are free."""
    posting = (posting or "").strip()
    st = _state(store)
    if len(st["tailored"]) >= FREE_TAILORED:
        raise ValueError(f"You've used your {FREE_TAILORED} free samples written for a posting")
    # A link is read before anything is spent; one that can't be read raises
    # with words for the person, and the free sample isn't used.
    link = ""
    if postings.is_link(posting):
        got = postings.read(posting)
        link, posting = got["url"], postings.as_posting(got)
    if len(posting) < 80:
        raise ValueError("Paste the whole job posting, or a link to it, so there's enough to match your record against")
    chosen, picked, out = _choose(store, rec, "for the job posting below",
                                  "what the posting asks for", f"POSTING\n{posting[:MAX_POSTING]}")
    title = (out.get("posting_title") or "").strip()[:120]
    res = _layout(store, rec, chosen, picked, "tailored", title or _headline(store, rec))
    res["posting"] = {"title": title, "company": (out.get("posting_company") or "").strip()[:120], "url": link}
    res["gaps"] = [g.strip()[:120] for g in (out.get("gaps") or []) if g.strip()][:4]
    st["tailored"].append(res)
    store.save_state(SAMPLES, st)
    return res


def view(store, rec: mr.Record) -> dict:
    st = _state(store)
    dress = lambda r: _dress(store, rec, r) if r else r      # noqa: E731
    return {"general": dress(st["general"]), "tailored": [dress(t) for t in st["tailored"]],
            "free_left": max(0, FREE_TAILORED - len(st["tailored"])), "free": FREE_TAILORED,
            "prefs": prefs(store)}


def as_text(res: dict) -> str:
    """The sample as plain text, for copying into an application form."""
    out = [res["name"].upper() if res.get("name") else "", res.get("headline", ""), res.get("contact", "")]
    for sec in res["sections"]:
        out += ["", sec["heading"].upper()]
        if sec["key"] == "summary":
            out.append(res["summary"])
        elif sec["key"] == "experience":
            for e in res["employers"]:
                out += ["", e["employer"]] + ([e["about"]] if e.get("about") else [])
                for r in e["roles"]:
                    out.append(f"{r['title']}" + (f"  {r['dates']}" if r["dates"] else ""))
                    out += [f"• {l}" for l in r["lines"]]
        elif sec["key"] == "skills":
            out.append(" · ".join(res["skills"]))
        else:
            out += sec["lines"]
    return "\n".join(l for l in out if l is not None).strip() + "\n"
