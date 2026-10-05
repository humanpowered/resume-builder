"""
Resume bullets: drafting one from a recorded accomplishment, and the checks
every bullet has to pass wherever it came from.

The grounding rule is the one that matters most. Every figure in a bullet must
appear in the person's own words. A fabricated number that reaches a resume
passes every later check, because by then it looks like a fact, and it is the
one error that can cost someone an offer after they have been hired.
"""
import re

from . import llm

NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
WORDS = re.compile(r"[A-Za-z0-9'%$.-]+")

MAX_BULLET_WORDS = 40

# Openers that describe a duty rather than a result. A bullet starting with one
# of these tells a reader what the job was, not what the person did with it.
DUTY_OPENERS = re.compile(
    r"^(responsible for|duties included|tasked with|helped( to)?|assisted( with| in)?|"
    r"worked on|involved in|participated in|in charge of)\b", re.I)
FIRST_PERSON = re.compile(r"\b(I|me|my|we|our)\b")
# Words that read as filler to a recruiter and as AI-written to many of them.
INFLATED = re.compile(
    r"\b(spearheaded|leveraged|synerg\w*|utilized|orchestrated|"
    r"passionate|results-driven|dynamic|go-getter)\b", re.I)


def numbers(text: str) -> set:
    """Figures, normalised so 1,200 and 1200 compare equal."""
    return {n.replace(",", "").rstrip(".") for n in NUMBER.findall(text or "")}


def ungrounded(bullet: str, source: str) -> set:
    """Figures in the bullet that are nowhere in its source."""
    return numbers(bullet) - numbers(source)


def word_count(text: str) -> int:
    return len((text or "").split())


def lint(bullet: str) -> list:
    """
    Formatting problems with one bullet, as short readable strings.

    Deliberately a short list. Each rule here is one a recruiter would notice;
    style preferences that are matters of taste stay out, because a lint that
    flags everything gets ignored.
    """
    problems = []
    b = (bullet or "").strip()
    if not b:
        return ["empty bullet"]
    if word_count(b) > MAX_BULLET_WORDS:
        problems.append(f"over {MAX_BULLET_WORDS} words ({word_count(b)})")
    if DUTY_OPENERS.match(b):
        problems.append("opens with a duty, not a result")
    if FIRST_PERSON.search(b):
        problems.append("first person")
    if "—" in b:
        problems.append("em dash")
    m = INFLATED.search(b)
    if m:
        problems.append(f"inflated word: {m.group(0)}")
    first = b.split()[0]
    if not first[:1].isupper():
        problems.append("does not start with a capital")
    return problems


def has_figure(bullet: str) -> bool:
    return bool(NUMBER.search(bullet or ""))


BULLET_SCHEMA = {
    "type": "object",
    "properties": {"bullet": {"type": "string"}},
    "required": ["bullet"],
    "additionalProperties": False,
}

BULLET_PROMPT = """Turn one recorded accomplishment into a single resume bullet.

RULES
- One sentence, or two short ones. Under {max_words} words.
- Lead with what was achieved, not the task. Start with a past-tense verb.
- No first person. No "responsible for", "helped", "assisted with".
- Every number must appear in the source below. Do not derive, round, combine
  or estimate one. If the source has no number, write the bullet without one.
- Do not name the employer; the resume shows it above the bullet.
- Plain words. No "spearheaded", "leveraged", "utilized", no em dashes.
- If "My part" is filled, the result was a team's: the bullet says what this
  person did and credits the outcome to the team or the effort, never to
  them alone.
{emphasis}
SOURCE
Title:   {title}
Problem: {problem}
Actions: {actions}
My part: {contribution}
Results: {results}

Return JSON: {{"bullet": "..."}}"""


def compile_bullet(acc, emphasis: list | None = None) -> str:
    """
    Draft one bullet, then hold it to its own source.

    `emphasis` is a list of the posting's terms this accomplishment genuinely
    shows; the model is asked to use their wording where it is accurate. Two
    drafts with an invented figure and the person's own words are used instead:
    dull beats wrong.
    """
    hint = ""
    if emphasis:
        hint = ("- Where it is accurate, use these terms from the job posting: "
                + ", ".join(emphasis) + ".\n")
    prompt = BULLET_PROMPT.format(max_words=MAX_BULLET_WORDS, emphasis=hint,
                                  title=acc.title, problem=acc.problem,
                                  actions=acc.actions, results=acc.results,
                                  contribution=acc.contribution or "(theirs alone)")
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        reply = llm.request_json(messages, 1024, "bullet", schema=BULLET_SCHEMA)
        bullet = (reply.get("bullet") or "").strip()
        invented = ungrounded(bullet, acc.source_text())
        if bullet and not invented:
            return bullet
        messages = messages + [
            {"role": "assistant", "content": f'{{"bullet": {bullet!r}}}'},
            {"role": "user", "content":
             f"These figures are not in the source: {', '.join(sorted(invented))}. "
             f"Rewrite using only figures that appear there, or none."}]
    return (acc.results or acc.actions or acc.title).strip()
