"""
The coached interview that grows the master record.

The hard part of a master record is recall, not typing. People cannot list
fifteen accomplishments on demand, and a resume has trained them to name the
two or three that suited one application. A question they can answer -- "what
was wrong before you got there?" -- surfaces work that "list your
achievements" never reaches.

Three kinds of conversation, in the order a role needs them:

  context         the role's facts: what the organisation was, what they were
                  hired to fix, team, budget, who they reported to. Plain
                  questions, no model, each skippable.
  expand          an imported resume bullet, opened back up into the problem,
                  the actions and the result, so the number behind it surfaces.
  new             accomplishments that are on no resume at all.

The coaching is one idea applied per accomplishment: look for evidence in
descending order of strength and stop at the first rung that holds (metric,
derived, scope, qualitative). `qualitative` is a real answer; inventing a
number to avoid it is forbidden.

All input goes through one `ask` callable, so a person at a terminal and the
simulated interviewee in the evaluation drive exactly the same code.
"""
import json
import re
from pathlib import Path

from . import llm
from . import record as mr
from .record import Accomplishment, Role


class Stop(Exception):
    """The person chose to leave. Not an error."""


GIVE_UP = {"done", "stop", "next", "no more", "skip", "nothing", "that's all",
           "thats all", "no", "n", "none", "move on", "i don't know", "dont know"}


def gave_up(answer: str) -> bool:
    return not answer or answer.strip().lower().rstrip(".!") in GIVE_UP


# --------------------------------------------------------------------------
# Role context

# (field, question). Worded for any job: a nurse, a plant manager and a
# software engineer must all be able to answer each one.
CONTEXT_QUESTIONS = [
    ("Company", "In a sentence, what was {employer}? What it does, roughly how big "
                "(people, revenue, sites, beds, students -- whatever is natural), and the industry."),
    ("Challenge", "What were you brought in to do, or what problem was waiting for you "
                  "when you started as {title}?"),
    ("Authority", "Did you lead or supervise anyone there? How many, and in what roles?"),
    ("Budget", "Were you responsible for a budget, revenue target, or expensive "
               "equipment or inventory? Roughly how much?"),
    ("Reported to", "What was the title of the person you reported to?"),
    ("Territory", "What did your work cover: one site, a region, national, international, "
                  "a set of clients or accounts?"),
    ("Recognition", "Any awards, promotions, top ratings or formal recognition there?"),
]


def ask_context(role: Role, ask) -> int:
    """Ask only for the fields still blank. Returns how many were filled."""
    filled = 0
    for label, question in CONTEXT_QUESTIONS:
        if role.fields.get(label):
            continue
        answer = ask(question.format(employer=role.employer, title=role.title)
                     + "\n  (Enter to skip)")
        if gave_up(answer):
            continue
        role.fields[label] = answer.strip()
        filled += 1
    return filled


# --------------------------------------------------------------------------
# The model's half of the conversation

COACH = """You are interviewing someone to build a master record of their career
accomplishments. It is not a resume: it has no page limit, and a later step
picks from it for each job application. Get as much real material out of them
as you can, and find the numbers they are holding without realising.

Rules:

1. ONE question per turn. Short. No preamble, no summarising their answer back,
   no praise.
2. Walk the evidence ladder for each accomplishment; stop at the first rung that
   holds:
   - metric: a number they already know.
   - derived: a number you work out with them. If something got faster,
     cheaper, safer, larger or less manual, ask what it was before, what it was
     after, and how often it happens. State the figure you derived and ask
     whether it is right.
   - scope: the size of the work -- people, budget, patients, students,
     customers, sites, units, volume.
   - qualitative: what it enabled, ended or protected, and for whom. A real
     answer. Never invent a number to avoid it.
3. Never put a number in a field that they did not give or confirm.
4. Work for any profession. Do not assume office work, marketing or software.
5. If they cannot remember or want to move on, accept it at once and set status
   "role_done". Never push twice.
6. Do not ask about anything in already_recorded. If they start describing one
   of those, say so and ask for a different one.
7. Also note the skills, tools, methods, licences or equipment the
   accomplishment shows, in their words or the standard industry term.

{mode}

Return ONLY JSON:
{{
  "say": "the single thing to show them: a question, or a derived figure to confirm",
  "draft": {{"title": "", "problem": "", "actions": "", "results": "",
             "evidence": "", "skills": []}},
  "status": "asking" | "complete" | "role_done"
}}

- Carry forward everything already in the draft and add to it. Leave a field ""
  until you have a real answer.
- "title": a few words, no figures.
- "evidence": one of metric, derived, scope, qualitative.
- "complete" only when title, problem, actions, results and evidence are all
  filled; then "say" is one short line confirming what was recorded.
"""

MODE_NEW = ("MODE: find a new accomplishment that is not in already_recorded.")
MODE_EXPAND = ("MODE: expand this bullet from their old resume into a full record. "
               "It is compressed: the problem it solved, what they actually did and "
               "the result (with its number, if one exists) are missing. Start by "
               "asking about the situation before it.\nBULLET: {bullet}")

INTERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string"},
        "draft": {
            "type": "object",
            "properties": {
                "title": {"type": "string"}, "problem": {"type": "string"},
                "actions": {"type": "string"}, "results": {"type": "string"},
                "evidence": {"type": "string", "enum": list(mr.EVIDENCE_TIERS) + [""]},
                "skills": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "problem", "actions", "results", "evidence", "skills"],
            "additionalProperties": False,
        },
        "status": {"type": "string", "enum": ["asking", "complete", "role_done"]},
    },
    "required": ["say", "draft", "status"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# Lenses: one more angle, offered once when someone says they are out


LENSES = [
    ("people", ("hire", "hired", "train", "coach", "mentor", "team", "staff",
                "promot", "recruit", "precept", "onboard"),
     "Who did you hire, train, mentor or onboard there, and what changed because of it?"),
    ("fixing", ("broken", "fixed", "mess", "cleaned", "rescued", "failing",
                "backlog", "inherit", "turnaround"),
     "What was broken or neglected when you arrived that you ended up fixing?"),
    ("money", ("cost", "saved", "revenue", "budget", "margin", "profit",
               "spend", "price", "sales", "waste"),
     "Did anything you did make or save the organisation money, even indirectly?"),
    ("speed", ("faster", "hours", "days", "automat", "manual", "time",
               "turnaround", "delay", "wait"),
     "What used to take a long time, or a lot of hands, that stopped doing so?"),
    ("risk", ("audit", "complian", "outage", "safety", "error", "fraud",
              "risk", "incident", "quality", "infection", "injur", "defect"),
     "Did you prevent something going wrong, or catch something that had?"),
    ("legacy", ("standard", "adopted", "documented", "still", "rolled out",
                "template", "process", "handbook", "policy", "curriculum"),
     "Is anything you built, wrote or set up there still in use after you left?"),
    ("stopping", ("stopped", "retired", "simplif", "consolidat", "removed",
                  "cancelled", "declined"),
     "Did you stop, simplify or retire something that was not worth doing?"),
]


def pick_lens(role: Role) -> tuple:
    """The angle least covered by what this role already holds, so it is a
    genuinely different question, not a rephrasing of the one just declined."""
    text = " ".join([a.title + " " + a.problem + " " + a.actions + " " + a.results
                     for a in role.accomplishments] + list(role.recorded_bullets)).lower()
    scored = sorted(((sum(text.count(k) for k in keys), name, q)
                     for name, keys, q in LENSES), key=lambda s: s[0])
    return scored[0][1], scored[0][2]


def near_duplicate(role: Role, title: str, ignore: str = ""):
    """Has this already been recorded? Compared on content words, because the
    same accomplishment gets described twice in different phrasing. `ignore`
    is the resume bullet being expanded, which the new record replaces and so
    must not count as its duplicate."""
    def words(text):
        return set(re.findall(r"[a-z]{4,}", (text or "").lower()))
    new = words(title)
    if not new:
        return None
    for acc in role.accomplishments:
        old = words(acc.title)
        if old and len(new & old) / len(new | old) > 0.5:
            return acc.title
    for bullet in role.recorded_bullets:
        if bullet == ignore:
            continue
        old = words(bullet)
        if old and len(new & old) >= max(3, len(new) * 0.6):
            return bullet[:60] + "..."
    return None


def by_need(rec: mr.Record) -> list:
    """Thinnest first: that is where an hour of someone's time is worth most."""
    return sorted(rec.roles, key=lambda r: (r.coverage()["total"],
                                            r.coverage()["quantified"]))


# --------------------------------------------------------------------------
# Sessions


class Session:
    """
    One interview over one record file.

    Saves after every confirmed accomplishment, and saves the half-finished
    one to a sidecar after every turn, so stopping mid-conversation loses
    nothing and the next run offers to pick it up.
    """

    def __init__(self, path: Path, ask, say=print):
        self.path = Path(path)
        self.state_path = self.path.with_name("." + self.path.stem + ".interview.json")
        self.ask = ask
        self.say = say
        self.rec = (mr.parse(self.path.read_text(encoding="utf-8"))
                    if self.path.exists() else mr.Record(header=["# Master record"]))
        self.recorded = 0

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".md.tmp")
        tmp.write_text(mr.render(self.rec), encoding="utf-8")
        tmp.replace(self.path)

    def save_state(self, role: Role, messages: list, draft: dict, bullet: str) -> None:
        self.state_path.write_text(json.dumps({
            "employer": role.employer, "title": role.title, "bullet": bullet,
            "messages": messages, "draft": draft}, indent=1), encoding="utf-8")

    def load_state(self):
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def clear_state(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()

    # -- the parts ---------------------------------------------------------

    def add_roles(self) -> None:
        self.say("\nList your jobs, newest first. Include part-time, contract, "
                 "volunteer and military roles if they matter. Press Enter on an "
                 "empty employer when you are done.")
        while True:
            employer = self.ask("Employer (or organisation)?")
            if not employer:
                break
            title = self.ask(f"Your job title at {employer}?")
            dates = self.ask("Dates? (anything readable, e.g. 2019 - 2022)")
            role = Role(employer=employer.strip(), title=title.strip())
            if dates:
                role.fields["Dates"] = dates.strip()
            self.rec.roles.append(role)
            self.save()

    def context(self, role: Role) -> None:
        if ask_context(role, self.ask):
            self.save()

    def turn(self, messages: list) -> dict:
        return llm.request_json(messages, 4096, "interview", schema=INTERVIEW_SCHEMA)

    def converse(self, role: Role, bullet: str = "", resume=None, seed=None) -> str:
        """
        One accomplishment. Returns "complete", "role_done" or "skipped".
        """
        if resume:
            messages, got = resume["messages"], resume.get("draft") or {}
        else:
            ctx = {"employer": role.employer, "job_title": role.title,
                   "dates": role.fields.get("Dates", ""),
                   "role_context": {k: v for k, v in role.fields.items() if v},
                   "already_recorded": [a.title for a in role.accomplishments]}
            mode = MODE_EXPAND.format(bullet=bullet) if bullet else MODE_NEW
            messages = [{"role": "user", "content":
                         COACH.format(mode=mode) + "\nROLE:\n" + json.dumps(ctx, indent=1)
                         + "\n\nBegin with your first question."}]
            if seed:
                q, a = seed
                messages += [{"role": "assistant", "content": json.dumps(
                                 {"say": q, "draft": {}, "status": "asking"})},
                             {"role": "user", "content": a}]
            got = {}
        draft = Accomplishment(**{k: got.get(k, "") for k in
                                  ("title", "problem", "actions", "results", "evidence")})
        skills = list(got.get("skills") or [])

        while True:
            reply = self.turn(messages)
            status = reply.get("status", "asking")
            say = (reply.get("say") or "").strip()
            d = reply.get("draft") or {}
            for key in ("title", "problem", "actions", "results", "evidence"):
                if d.get(key):
                    setattr(draft, key, str(d[key]).strip())
            for s in d.get("skills") or []:
                if s and s not in skills:
                    skills.append(s)

            if status == "role_done":
                self.clear_state()
                if say:
                    self.say(say)
                return "role_done"

            if status == "complete":
                dupe = near_duplicate(role, draft.title, ignore=bullet)
                if dupe:
                    messages = messages + [
                        {"role": "assistant", "content": json.dumps(reply)},
                        {"role": "user", "content":
                         f"That duplicates {dupe!r}, already recorded. Ask for a different one."}]
                    continue
                if draft.evidence not in mr.EVIDENCE_TIERS:
                    draft.evidence = "qualitative"
                role.accomplishments.append(draft)
                if bullet and bullet in role.recorded_bullets:
                    role.recorded_bullets.remove(bullet)
                self.link_skills(skills, draft.title)
                self.save()
                self.clear_state()
                self.recorded += 1
                self.say(f"  recorded: {draft.title} ({draft.evidence})")
                return "complete"

            messages = messages + [{"role": "assistant", "content": json.dumps(reply)}]
            self.save_state(role, messages, {**vars(draft), "skills": skills}, bullet)
            answer = self.ask(say or "Go on?")
            if gave_up(answer):
                self.clear_state()
                return "skipped"
            messages = messages + [{"role": "user", "content": answer}]

    def link_skills(self, names: list, title: str) -> None:
        """Skills an accomplishment showed become evidence for those skills."""
        for name in names:
            name = name.strip()
            if not name:
                continue
            skill = self.rec.skill(name)
            if skill is None:
                skill = mr.Skill(name=name, category="From the interview")
                self.rec.skills.append(skill)
            if title not in skill.evidence:
                skill.evidence.append(title)

    def role(self, role: Role, target: int = 10) -> None:
        """Context, then expand imported bullets, then new accomplishments,
        then one more angle, once."""
        self.say(f"\n{'=' * 60}\n{role.label()}\n  {mr.coverage_note(role, target, prompt=True)}")
        self.context(role)

        for bullet in list(role.recorded_bullets):
            answer = self.ask(f"From your resume: \"{bullet}\"\n"
                              f"  Open this one up? (Enter = yes, 'skip' = leave it, "
                              f"'done' = move on)")
            low = (answer or "").strip().lower()
            if low in ("done", "next", "stop", "move on"):
                break
            if low in ("skip", "s", "no", "n"):
                continue
            self.converse(role, bullet=bullet)

        lens_used = False
        seed = None
        while True:
            outcome = self.converse(role, seed=seed)
            seed = None
            if outcome == "complete":
                continue
            if lens_used:
                return
            lens_used = True
            name, question = pick_lens(role)
            answer = self.ask(f"One more angle before we move on ({name}): {question}\n"
                              f"  (Enter to skip)")
            if gave_up(answer):
                return
            seed = (question, answer)

    def run(self, only: str = "", target: int = 10) -> None:
        state = self.load_state()
        if state:
            role = self.rec.role_by_employer(state["employer"])
            if role is not None:
                answer = self.ask(f"You stopped part-way through an accomplishment at "
                                  f"{role.employer}. Pick it up? (Enter = yes, 'no' = discard)")
                if gave_up(answer) and answer:
                    self.clear_state()
                else:
                    self.converse(role, bullet=state.get("bullet", ""), resume=state)
        if not self.rec.roles:
            self.add_roles()
        roles = by_need(self.rec)
        if only:
            roles = [r for r in roles if only.lower() in r.employer.lower()]
        for role in roles:
            self.role(role, target)
