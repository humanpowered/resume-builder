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
import os
from datetime import datetime, timezone

from . import journey, llm, posting as postings, skills
from . import record as mr

SAMPLES = "journey.samples"
MAX_LINES, MAX_PER_JOB, MAX_SKILLS = 18, 6, 12
MAX_POSTING = 15000


def free_tailored() -> int:
    """How many samples for a posting each person gets: RB_FREE_TAILORED,
    20 unless set."""
    try:
        return max(0, int(os.environ.get("RB_FREE_TAILORED", 20)))
    except ValueError:
        return 20

EXTRA_HEADINGS = (("awards", "Awards"), ("publications", "Talks and publications"),
                  ("memberships", "Boards and memberships"), ("training", "Training"))


# ---------------------------------------------------------------- what a sample is built from

def _paid(rec: mr.Record) -> list:
    """Paid jobs in the order the person sees them: employers newest first,
    each employer's titles newest first."""
    return [(i, rec.roles[i]) for i in journey.shown_order(rec) if not rec.roles[i].outside()]


def _pool(rec: mr.Record) -> tuple:
    """Every line and every skill the person has, each with an id."""
    lines = {}
    for i, role in _paid(rec):
        for n, l in enumerate(journey._lines(role)):
            lines[f"j{i}l{n}"] = {"job": i, "text": l["text"]}
    mine = [s for s in rec.skills if s.have == skills.YES]
    return lines, {f"s{n}": s.name for n, s in enumerate(mine)}


def _contact(rec: mr.Record) -> str:
    c = {k: journey.clean(rec.contact.get(k, "")) for k in journey.CONTACT_FIELDS}
    return " · ".join(x for x in (c["Location"], c["Email"], c["Telephone"], c["Linkedin"]) if x)


def _layout(rec: mr.Record, chosen: dict, skill_names: list, kind: str, headline: str) -> dict:
    """The resume as the page shows it: titles at one employer stacked under
    one heading, newest first, each with the lines chosen for it."""
    employers, at = [], {}
    for i, role in _paid(rec):
        key = mr._squash(role.employer)
        if key not in at:
            at[key] = {"employer": role.employer, "roles": []}
            employers.append(at[key])
        at[key]["roles"].append({"title": role.title,
                                 "dates": journey.clean(role.fields.get("Dates", "")),
                                 "lines": chosen.get(i, [])})
    extras = [{"heading": h, "lines": list(rec.extras.get(k, []))}
              for k, h in EXTRA_HEADINGS if rec.extras.get(k)]
    volunteer = [f"{r.title}, {r.employer}" + (f" · {journey.clean(r.fields.get('Dates', ''))}"
                                              if r.fields.get("Dates") else "")
                 for r in rec.roles if r.outside()]
    if volunteer:
        extras.append({"heading": "Volunteering and projects", "lines": volunteer})
    return {"kind": kind, "name": journey.clean(rec.contact.get("Name", "")), "headline": headline,
            "contact": _contact(rec), "summary": rec.summary,
            "employers": employers, "education": list(rec.education) + list(rec.certifications),
            "skills": skill_names, "extras": extras,
            "made": datetime.now(timezone.utc).isoformat()}


def _headline(store, rec: mr.Record) -> str:
    targets = journey.targets_view(store, rec)["titles"]
    return targets[0] if targets else next((r.title for _, r in _paid(rec)), "")


# ---------------------------------------------------------------- the free draft

def draft(store, rec: mr.Record) -> dict:
    """No model call: the lines as they stand, ones with figures first. Jobs
    worked through carry their stronger lines; the rest read as the resume
    had them."""
    chosen = {}
    for rank, (i, role) in enumerate(_paid(rec)):
        lines = sorted(journey._lines(role), key=lambda l: (journey.line_rank(l["text"]) * -1, l["index"]))
        chosen[i] = [l["text"] for l in lines][:5 if rank < 3 else 3]
    mine = [s.name for s in rec.skills if s.have == skills.YES][:MAX_SKILLS]
    return _layout(rec, chosen, mine, "draft", _headline(store, rec))


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
  Recent jobs get more. A job more than 15 years old gets 2 lines at most,
  or none.
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
    roles = dict(_paid(rec))
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
    res = _layout(rec, chosen, picked, "general", title)
    st = _state(store)
    st["general"] = res
    store.save_state(SAMPLES, st)
    return res


def tailored(store, rec: mr.Record, posting: str) -> dict:
    """One paid call: a resume for one posting. A few are free."""
    posting = (posting or "").strip()
    st = _state(store)
    free = free_tailored()
    if len(st["tailored"]) >= free:
        raise ValueError(f"You've used your {free} free samples written for a posting")
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
    res = _layout(rec, chosen, picked, "tailored", title or _headline(store, rec))
    res["posting"] = {"title": title, "company": (out.get("posting_company") or "").strip()[:120], "url": link}
    res["gaps"] = [g.strip()[:120] for g in (out.get("gaps") or []) if g.strip()][:4]
    st["tailored"].append(res)
    store.save_state(SAMPLES, st)
    return res


def view(store) -> dict:
    st = _state(store)
    return {"general": st["general"], "tailored": st["tailored"],
            "free_left": max(0, free_tailored() - len(st["tailored"])), "free": free_tailored()}


def as_text(res: dict) -> str:
    """The sample as plain text, for copying into an application form."""
    out = [res["name"].upper() if res.get("name") else "", res.get("headline", ""), res.get("contact", "")]
    if res.get("summary"):
        out += ["", "SUMMARY", res["summary"]]
    out += ["", "EXPERIENCE"]
    for e in res["employers"]:
        out += ["", e["employer"]]
        for r in e["roles"]:
            out.append(f"{r['title']}" + (f"  {r['dates']}" if r["dates"] else ""))
            out += [f"• {l}" for l in r["lines"]]
    if res.get("education"):
        out += ["", "EDUCATION AND LICENCES"] + res["education"]
    if res.get("skills"):
        out += ["", "SKILLS", " · ".join(res["skills"])]
    for x in res.get("extras", []):
        out += ["", x["heading"].upper()] + x["lines"]
    return "\n".join(l for l in out if l is not None).strip() + "\n"
