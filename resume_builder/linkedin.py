"""
A LinkedIn profile written from the record: headline, About, a description
for every job, and the skills in the order to add them.

LinkedIn has no way for another site to change a profile, so this writes
text for the person to copy and paste. Titles, employers and dates are never
written by the model; the page shows them straight from the record, so the
profile always agrees with every resume made from it.

A profile is public and a resume isn't. Every field is written twice: as the
record has it, and with money amounts turned into sizes ("eight figures").
Exact figures are the default; the page warns, and the person chooses.

The first profile is free to ask for; after that a few rewrites are allowed
(RB_LINKEDIN_REWRITES, 10 unless set), counted across every section.
"""
import hashlib
import os
from datetime import datetime, timezone

from . import journey, llm, skills
from . import record as mr
from . import summary as summ

STATE = "journey.linkedin"
HEADLINE, ABOUT, ROLE = 220, 2600, 2000     # LinkedIn's character limits
FOLD = 265                                  # About shown before "see more", roughly
MAX_SKILLS = 50
OPEN = ("active", "offers", "quiet")
SECTIONS = ("headline", "about")            # plus "job:<key>" for a job's description


def rewrites_allowed() -> int:
    """RB_LINKEDIN_REWRITES, 10 unless set."""
    try:
        return max(0, int(os.environ.get("RB_LINKEDIN_REWRITES", 10)))
    except ValueError:
        return 10


# ---------------------------------------------------------------- what it's written from

def _paid(rec: mr.Record) -> list:
    return [(i, rec.roles[i]) for i in journey.shown_order(rec) if not rec.roles[i].outside()]


def _source(store, rec: mr.Record) -> tuple:
    """The record as the model reads it, and its jobs by id."""
    body = mr.render(mr.Record(contact={}, roles=rec.roles,
                               skills=[s for s in rec.skills if s.have == skills.YES],
                               education=rec.education, certifications=rec.certifications,
                               competencies=rec.competencies, sets_apart=rec.sets_apart,
                               target=rec.target, extras=rec.extras))
    years = journey.work_view(store, rec)["years"]["total"]
    targets = journey.targets_view(store, rec)["titles"]
    jobs = {f"j{n}": role for n, (_, role) in enumerate(_paid(rec))}
    listing = []
    for jid, role in jobs.items():
        lines = [l["text"] for l in journey._lines(role)]
        listing.append(f"[{jid}] {role.title}, {role.employer}, {journey.clean(role.fields.get('Dates', ''))}\n"
                       + "\n".join(f"  - {t}" for t in lines))
    head = (f"YEARS OF PAID WORK: {years}\n" if years else "") + \
           f"TARGET ROLES: {', '.join(targets) or '(none set)'}\n"
    return head + "\n" + body[:60000], "\n".join(listing), jobs


def fingerprint(rec: mr.Record) -> str:
    """Changes whenever the record does, so the page can say a profile was
    written from an older version of it."""
    return hashlib.sha256(mr.render(rec).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- the model's part

TEXT = {"type": "object", "properties": {"text": {"type": "string"}, "soft": {"type": "string"}},
        "required": ["text", "soft"], "additionalProperties": False}
SCHEMA = {
    "type": "object",
    "properties": {
        "headlines": {"type": "array", "items": TEXT},
        "about": TEXT,
        "close_active": TEXT,
        "close_offers": TEXT,
        "jobs": {"type": "array", "items": {"type": "object", "properties": {
            "job": {"type": "string"}, "text": {"type": "string"}, "soft": {"type": "string"}},
            "required": ["job", "text", "soft"], "additionalProperties": False}},
    },
    "required": ["headlines", "about", "close_active", "close_offers", "jobs"],
    "additionalProperties": False,
}

RULES = """RULES FOR EVERY FIELD
- Figures exactly as the record gives them. Never round, combine or invent a
  number, date, team size or money amount. If the record has no figure for
  something, write it without one.
- "soft" is the same text with every money amount (revenue, budget, sales,
  savings, in any currency) written as a size in words instead: "eight
  figures", "a multi-million budget", "several hundred million in sales".
  Percentages, counts and team sizes stay as they are. If the text has no
  money amount, soft is "".
- Keep the names of employers, clients and products as the record has them.
- Plain business English, the way the person would say it to a peer. None of
  these: results-driven, proven track record, passionate, dynamic, synergy,
  leverage, spearheaded, seasoned, thought leader, "not just X but Y". No em
  dashes. Vary sentence length.
- Only what the record supports. A thin record gets a shorter profile."""

PROFILE_PROMPT = """Write this person's LinkedIn profile from their career record below.
LinkedIn is public: recruiters, peers and their current employer read it.

WHAT TO WRITE
- headlines: three clearly different headlines, each at most 200 characters.
  Lead with their current or target title and the skills recruiters in their
  field search for. Parts may be separated with " | ". Never say they are
  looking, seeking or open to work.
- about: the About section, in the first person, at most 2,200 characters,
  in short paragraphs with no headings or bullet lists. LinkedIn shows only
  the first {fold} characters or so before "see more", so the first two
  sentences must say what they do and for whom, and stand on their own.
  Then the problems they're brought in to solve, the two or three strongest
  results in the record, and what they're best at. Leave out any closing line
  about a job search: that is written separately.
- close_active: one sentence to end About, saying they're looking for their
  next role and naming the kind of role from their targets.
- close_offers: one sentence to end About, saying they're glad to talk about
  that kind of role, without saying they're leaving their job.
- jobs: for every job under JOBS, by its id, a description of at most 1,500
  characters: one sentence on the scope of the job, then its results as lines
  starting with "• ". Draw only on that job's lines; reword them for someone
  skimming a profile, but add nothing.

{rules}
{extra}
Return JSON matching the schema.

RECORD
{record}

JOBS
{jobs}"""

ONE = {"type": "object", "properties": {"items": {"type": "array", "items": TEXT}},
       "required": ["items"], "additionalProperties": False}

REWRITE_PROMPT = """Rewrite one part of this person's LinkedIn profile from their career record below.
LinkedIn is public: recruiters, peers and their current employer read it.

WHAT TO WRITE
{what}

It must be clearly different from the current version:
{current}

{rules}
{extra}
Return JSON: {{"items": [{{"text": "...", "soft": "..."}}]}}

RECORD
{record}

JOBS
{jobs}"""

WHAT = {
    "headline": """Three new headlines, each at most 200 characters. Lead with their current
or target title and the skills recruiters in their field search for. Parts may
be separated with " | ". Never say they are looking, seeking or open to work.""",
    "about": """One new About section, in the first person, at most 2,200 characters, in short
paragraphs with no headings or bullet lists. The first two sentences must say
what they do and for whom, and stand on their own, since LinkedIn shows only
the first {fold} characters or so before "see more". Leave out any closing line
about a job search.""",
    "job": """One new description for the job {jid}, at most 1,500 characters: one sentence
on the scope of the job, then its results as lines starting with "• ". Draw
only on that job's lines; reword them for someone skimming a profile, but add
nothing.""",
}


def _pair(item: dict) -> dict:
    text = (item.get("text") or "").strip()
    soft = (item.get("soft") or "").strip()
    return {"text": text, "soft": soft if soft and soft != text else ""}


def _figures(fields: list, record_text: str) -> list:
    """Figures in the written text the record doesn't hold."""
    out = []
    for f in fields:
        for n in summ.unsupported(f["text"], record_text):
            if n not in out:
                out.append(n)
    return out


def _ask(make, schema: dict, record_text: str, collect, tokens: int) -> dict:
    """One call, and one more if a figure the record doesn't hold slipped in.
    What still has one after that is kept and named, for the page to flag.
    `make(extra)` is the prompt, with `extra` added to its rules."""
    extra = ""
    for _ in range(2):
        out = llm.request_json([{"role": "user", "content": make(extra)}],
                               tokens, "linkedin", schema=schema)
        bad = _figures(collect(out), record_text)
        if not bad:
            return {"out": out, "unsupported": []}
        extra = (f"- The last attempt used figures the record doesn't contain: {', '.join(bad)}. "
                 f"Leave them out.\n")
    return {"out": out, "unsupported": bad}


# ---------------------------------------------------------------- keeping it

def _state(store) -> dict:
    st = store.load_state(STATE) or {}
    st.setdefault("profile", None)
    st.setdefault("rewrites", 0)
    st.setdefault("open", "offers")
    st.setdefault("soften", False)
    st.setdefault("pasted", [])
    return st


def _skills(rec: mr.Record) -> list:
    """The person's skills, the ones with the most proof first."""
    mine = [(n, s) for n, s in enumerate(rec.skills) if s.have == skills.YES]
    mine.sort(key=lambda p: (-len(p[1].evidence), p[0]))
    return [{"name": s.name, "proof": len(s.evidence)} for _, s in mine][:MAX_SKILLS]


def write(store, rec: mr.Record) -> dict:
    """The whole profile, in one paid call (two at most). The first is free;
    writing it again counts as a rewrite."""
    st = _state(store)
    if st["profile"]:
        _spend(st)
    record_text, listing, jobs = _source(store, rec)
    if not jobs:
        raise ValueError("Your record has no jobs yet, so there's nothing to write a profile from")
    def make(extra):
        return PROFILE_PROMPT.format(fold=FOLD, rules=RULES, extra=extra, record=record_text, jobs=listing)

    def collect(out):
        return ([_pair(h) for h in out.get("headlines") or []] +
                [_pair(out.get(k) or {}) for k in ("about", "close_active", "close_offers")] +
                [_pair(j) for j in out.get("jobs") or []])

    got = _ask(make, SCHEMA, record_text, collect, 12000)
    out = got["out"]
    descriptions = {}
    for j in out.get("jobs") or []:
        role = jobs.get(j.get("job"))
        if role is not None and journey.job_key(role) not in descriptions:
            descriptions[journey.job_key(role)] = _pair(j)
    st["profile"] = {
        "headlines": [_pair(h) for h in out.get("headlines") or [] if (h.get("text") or "").strip()][:3],
        "headline": 0,
        "about": _pair(out.get("about") or {}),
        "close": {"active": _pair(out.get("close_active") or {}),
                  "offers": _pair(out.get("close_offers") or {})},
        "jobs": descriptions,
        "unsupported": got["unsupported"],
        "made": datetime.now(timezone.utc).isoformat(),
        "from": fingerprint(rec),
    }
    st["pasted"] = []
    store.save_state(STATE, st)
    return view(store, rec)


def _spend(st: dict) -> None:
    allowed = rewrites_allowed()
    if st["rewrites"] >= allowed:
        raise ValueError(f"You've used all {allowed} rewrites. You can still edit any part yourself")
    st["rewrites"] += 1


def rewrite(store, rec: mr.Record, section: str) -> dict:
    """A new version of one part: the headlines, About, or one job's
    description. Each counts as one rewrite."""
    st = _state(store)
    p = st["profile"]
    if not p:
        raise ValueError("Write your profile first")
    record_text, listing, jobs = _source(store, rec)
    if section == "headline":
        what, current = WHAT["headline"], "\n".join(h["text"] for h in p["headlines"])
    elif section == "about":
        what, current = WHAT["about"].format(fold=FOLD), p["about"]["text"]
    elif section.startswith("job:"):
        key = section[4:]
        jid = next((k for k, r in jobs.items() if journey.job_key(r) == key), None)
        if jid is None:
            raise LookupError("That job isn't on your record any more")
        what = WHAT["job"].format(jid=jid)
        current = (p["jobs"].get(key) or {}).get("text") or "(none yet)"
    else:
        raise ValueError("There's no such part of the profile")
    _spend(st)
    def make(extra):
        return REWRITE_PROMPT.format(what=what, current=current, rules=RULES, extra=extra,
                                     record=record_text, jobs=listing)
    got = _ask(make, ONE, record_text, lambda out: [_pair(i) for i in out.get("items") or []], 4000)
    items = [_pair(i) for i in got["out"].get("items") or [] if (i.get("text") or "").strip()]
    if not items:
        raise ValueError("We couldn't write a new version just now. Nothing was changed")
    if section == "headline":
        p["headlines"], p["headline"] = items[:3], 0
    elif section == "about":
        p["about"] = items[0]
    else:
        p["jobs"][section[4:]] = items[0]
    p["unsupported"] = [n for n in p.get("unsupported", []) if n not in got["unsupported"]] + got["unsupported"]
    store.save_state(STATE, st)
    return view(store, rec)


def choose(store, rec: mr.Record, headline=None, open_to=None, soften=None, pasted=None) -> dict:
    """The person's choices: which headline, how openly they're looking,
    whether money is written as sizes, and which parts they've pasted."""
    st = _state(store)
    if headline is not None and st["profile"]:
        if not 0 <= headline < len(st["profile"]["headlines"]):
            raise ValueError("There's no such headline")
        st["profile"]["headline"] = headline
    if open_to is not None:
        if open_to not in OPEN:
            raise ValueError("Choose who should know you're open to a move")
        st["open"] = open_to
    if soften is not None:
        st["soften"] = bool(soften)
    if pasted is not None:
        st["pasted"] = [p for p in pasted if p in ("headline", "about", "experience", "skills")]
    store.save_state(STATE, st)
    return view(store, rec)


# ---------------------------------------------------------------- what the page shows

def _pick(pair: dict, soften: bool) -> str:
    return (pair.get("soft") if soften and pair.get("soft") else pair.get("text")) or ""


def view(store, rec: mr.Record) -> dict:
    """The profile as the page shows it, in the person's chosen version.
    Jobs, titles and dates come from the record as it is now."""
    st = _state(store)
    allowed = rewrites_allowed()
    out = {"open": st["open"], "soften": st["soften"], "pasted": st["pasted"],
           "rewrites_left": max(0, allowed - st["rewrites"]), "rewrites": allowed,
           "limits": {"headline": HEADLINE, "about": ABOUT, "job": ROLE}, "fold": FOLD,
           "skills": _skills(rec), "profile": None}
    p = st["profile"]
    if not p:
        return out
    soft = st["soften"]
    about = _pick(p["about"], soft)
    close = "" if st["open"] == "quiet" else _pick(p["close"].get(st["open"], {}), soft)
    jobs = []
    for _, role in _paid(rec):
        d = p["jobs"].get(journey.job_key(role))
        jobs.append({"key": journey.job_key(role), "title": role.title, "employer": role.employer,
                     "dates": journey.clean(role.fields.get("Dates", "")),
                     "location": journey.clean(role.fields.get("Location", "")),
                     "text": _pick(d, soft) if d else ""})
    texts = [h["text"] for h in p["headlines"]] + [p["about"]["text"]] + \
            [d["text"] for d in p["jobs"].values()] + [c.get("text", "") for c in p["close"].values()]
    out["profile"] = {
        "headlines": [_pick(h, soft) for h in p["headlines"]], "headline": p["headline"],
        "about": about + (f"\n\n{close}" if close else ""),
        "jobs": jobs,
        "has_money": any(x.get("soft") for x in p["headlines"] + [p["about"]] + list(p["jobs"].values())
                         + list(p["close"].values())),
        "unsupported": [n for n in p.get("unsupported", []) if any(n in t for t in texts)],
        "made": p["made"], "stale": p.get("from") != fingerprint(rec),
    }
    return out
