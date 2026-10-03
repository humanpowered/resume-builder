"""
The skills inventory: what the person can do, in the words a job posting in
their field would use, each tied to what proves it.

Skills arrive three ways. Imported from a resume, as written. Named during the
interview, already linked to the accomplishment that showed them. And
suggested here: the standard skills for the person's profession that their
record implies but never names. Suggestions are only ever suggestions. They go
into a "To verify" group, export as have_it = "verify", and stay off every
resume until the person says yes.

Why suggest at all: people describe their work in their own words, and an
applicant tracking system matches the industry's words. A nurse writes "kept
the drips running"; the posting says "titration of vasoactive infusions".
"""
import json

from . import llm
from . import record as mr

# A suggested skill's category carries its intended group, "To verify:
# Clinical", so confirming it puts it where it belongs. Imported skills were on
# the person's own resume but have never been graded, so they are asked too.
PENDING = "To verify"


def is_pending(skill) -> bool:
    c = (skill.category or "").lower()
    return c.startswith(PENDING.lower()) or c == "imported"


def confirmed_category(skill) -> str:
    c = skill.category or ""
    if c.lower().startswith(PENDING.lower()) and ":" in c:
        return c.split(":", 1)[1].strip() or "Other"
    return "Other"


SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "profession": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "category": {"type": "string"},
            "because": {"type": "array", "items": {"type": "string"}}},
            "required": ["name", "category", "because"],
            "additionalProperties": False}},
    },
    "required": ["profession", "skills"],
    "additionalProperties": False,
}

SUGGEST_PROMPT = """Below is someone's career record. List the skills it shows
that a job posting in their field would ask for, using the standard term
employers and applicant tracking systems use.

RULES
- Only skills the record gives direct evidence for. For each, list in
  "because" the exact accomplishment titles (from the record) that show it.
  A skill you cannot tie to at least one title is not listed.
- Use the industry's standard term. Where an acronym is common, give both
  forms in the name, separated by a comma: "Electronic Health Records, EHR".
- Group into categories a hiring manager in this field would recognise
  (e.g. for a nurse: Clinical, Patient Safety, Leadership, Systems).
- Do not repeat a skill already in already_listed.
- At most 40.

RECORD
{record}

already_listed: {listed}"""


def suggest(rec: mr.Record) -> dict:
    """Ask for suggestions and keep only those whose evidence checks out."""
    titles = {a.title for _, a in rec.all_accomplishments()}
    record_text = mr.render(mr.Record(roles=rec.roles))
    reply = llm.request_json(
        [{"role": "user", "content": SUGGEST_PROMPT.format(
            record=record_text[:50000],
            listed=json.dumps([s.name for s in rec.skills]))}],
        8000, "skills", schema=SUGGEST_SCHEMA)
    kept = []
    for s in reply.get("skills") or []:
        because = [t for t in s.get("because") or [] if t in titles]
        if not because or rec.skill(s.get("name", "")):
            continue
        kept.append(mr.Skill(name=s["name"].strip(),
                             category=f"{PENDING}: {s.get('category') or 'Other'}",
                             evidence=because))
    return {"profession": reply.get("profession", ""), "skills": kept}


def verify(rec: mr.Record, ask, say=print) -> int:
    """
    Walk the unconfirmed skills, one question each. Yes moves a skill into
    its proper group with a level; no removes it. Returns how many were
    settled.
    """
    settled = 0
    pending = [s for s in rec.skills if is_pending(s)]
    if not pending:
        say("  no skills waiting to be confirmed")
        return 0
    say(f"\n{len(pending)} skill(s) to confirm. Answer with a level -- "
        f"e (expert), a (advanced), w (working), f (familiar) -- or n if you "
        f"do not have it. Enter skips; 'done' stops.")
    levels = {"e": "Expert", "a": "Advanced", "w": "Working", "f": "Familiar"}
    for s in pending:
        why = f"  (shown by: {'; '.join(s.evidence)})" if s.evidence else ""
        answer = (ask(f"{s.name}?{why}") or "").strip().lower()
        if answer in ("done", "stop"):
            break
        if not answer:
            continue
        if answer in ("n", "no"):
            rec.skills.remove(s)
            settled += 1
            continue
        if answer[:1] in levels:
            s.level = levels[answer[:1]]
            s.category = confirmed_category(s)
            settled += 1
    return settled
