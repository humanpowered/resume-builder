"""
The professional summary: the three or four sentences at the top of a resume.

The record keeps one base summary. The pipeline rewrites it for each posting,
so this one's job is to be accurate and complete enough to rewrite from: who
the person is, in their field's words, and the two or three results that
best show it. A person can write it themselves, or have one drafted from
their record and edit it.

A draft may only use figures the record contains. Any number in the draft
that the record doesn't hold is reported, because a summary is the first
thing a reader checks against the rest of the page.
"""
import re

from . import llm
from . import record as mr

MAX_WORDS = 90

DRAFT_PROMPT = """Write the professional summary for the top of this person's
resume, from their career record below.

RULES
- 3 or 4 sentences, at most {max_words} words.
- Open with who they are professionally, in the words a job posting in their
  field would use, and how long they have done it if the record shows it.
- Include the two or three strongest results in the record, with their
  figures exactly as the record gives them. Never round, combine or invent
  a number.
- Name the kinds of role or problem they are best at, in their field's terms.
- No first person ("I", "my"), no third person name. Resume style: "ICU charge
  nurse with ten years...".
- None of these: results-driven, proven track record, passionate, dynamic,
  team player, detail-oriented, go-getter, synergy, leverage.
- Only what the record supports. If it is thin, write a shorter summary.
{hint}
Return JSON: {{"summary": "..."}}

RECORD
{record}"""

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
          "required": ["summary"], "additionalProperties": False}
NUMBER = re.compile(r"\d[\d,.]*%?")


def draft(rec: mr.Record, hint: str = "") -> dict:
    """A drafted summary and any figures in it the record doesn't contain."""
    body = mr.render(mr.Record(contact={}, roles=rec.roles, skills=[s for s in rec.skills
                                                                   if s.have == "yes"],
                               education=rec.education, certifications=rec.certifications,
                               competencies=rec.competencies, sets_apart=rec.sets_apart,
                               target=rec.target, extras=rec.extras))
    extra = f"- The person asked for this emphasis: {hint.strip()}\n" if hint.strip() else ""
    reply = llm.request_json(
        [{"role": "user", "content": DRAFT_PROMPT.format(
            max_words=MAX_WORDS, hint=extra, record=body[:60000])}],
        1500, "summary", schema=SCHEMA)
    text = (reply.get("summary") or "").strip().strip('"')
    return {"text": text, "unsupported": unsupported(text, body)}


def unsupported(text: str, record_text: str) -> list:
    """Figures in the text that appear nowhere in the record."""
    have = {n.rstrip(".,") for n in NUMBER.findall(record_text)}
    return [n.rstrip(".,") for n in NUMBER.findall(text) if n.rstrip(".,") not in have]


def check(text: str) -> list:
    """What a person might want to fix in a summary they wrote."""
    issues = []
    words = len(text.split())
    if words > MAX_WORDS:
        issues.append(f"{words} words; resumes keep the summary under {MAX_WORDS}")
    if re.search(r"\b(I|my|me)\b", text):
        issues.append("written in the first person; resume summaries usually leave out 'I'")
    return issues
