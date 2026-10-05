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

The engine is a state machine driven one turn at a time (`Interview.step`),
so a terminal, a web page and the simulated interviewee in the evaluation all
drive exactly the same code.
"""
import json
import re
from dataclasses import dataclass, field

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


# --------------------------------------------------------------------------
# Education, certifications and the optional sections
#
# Asked once the roles are done, or on their own. Plain questions, no model:
# these are facts the person knows and a resume needs exactly as they are.
# Each list ends when the first question is left blank, so a section that
# doesn't apply costs one Enter.

BACKGROUND = "background"       # Interview(only=BACKGROUND) asks just these;
                                # "background:languages" asks one section

# (key, question, hint)
EDUCATION_QUESTIONS = [
    ("degree", "Your next qualification: a degree, diploma, apprenticeship, bootcamp or "
               "high-school diploma? Write it as it should read, e.g. 'BSN' or 'MBA'.",
     "Enter when there are no more"),
    ("field", "Field of study or major?", "Enter to skip"),
    ("school", "School, college or institution?", "Enter to skip"),
    ("year", "Year finished, or the year you expect to?", "Enter to skip"),
    ("honours", "Honours, a GPA worth showing, or a thesis title?", "Enter to skip"),
]
CERTIFICATION_QUESTIONS = [
    ("name", "Your next licence or certification? e.g. 'Registered Nurse', 'PMP', "
             "'CDL Class A', 'CPA'.", "Enter when there are no more"),
    ("issuer", "Who issued it? (a board, state or organisation)", "Enter to skip"),
    ("year", "Year earned?", "Enter to skip"),
    ("expires", "Does it expire or need renewing? When?", "Enter if it doesn't"),
]
CLEARANCE_QUESTIONS = [
    ("level", "Do you hold, or have you held, a security clearance or public trust? Its "
              "level, e.g. 'Secret', 'Top Secret/SCI', 'Public Trust'.", "Enter if none"),
    ("status", "Is it active now? If not, when did it lapse?", "Enter to skip"),
    ("agency", "Which agency or department granted it?", "Enter to skip"),
    ("polygraph", "Any polygraph? e.g. 'CI poly' or 'full scope'.", "Enter if none"),
]
# Asked only when the record shows work where a clearance is plausible. A
# nurse, teacher or shop manager should never meet the question.
CLEARANCE_SIGNS = re.compile(
    r"\b(defen[cs]e|military|army|navy|naval|air force|usaf|marines?|marine corps|usmc|"
    r"coast guard|national guard|space force|dod|department of defen[cs]e|veteran|"
    r"federal|government contract\w*|intelligence community|homeland security|dhs|"
    r"fbi|cia|nsa|dia|nro|nga|department of energy|national lab\w*|nasa|"
    r"clearance|cleared|classified|top secret|ts/sci|public trust|"
    r"gs-\d+|contracting officer|lockheed|raytheon|rtx|northrop|general dynamics|"
    r"bae systems|leidos|booz allen|saic|caci|mantech|l3harris|huntington ingalls|"
    r"anduril|palantir)\b", re.I)


def clearance_relevant(rec) -> bool:
    """Whether anything in the record suggests cleared work."""
    if rec.extras.get("clearance"):
        return True
    text = [rec.target.get(k, "") for k in ("Titles", "Industries", "Notes")]
    text += rec.certifications
    for role in rec.roles:
        text += [role.employer, role.title, *role.fields.values(), *role.recorded_bullets]
        text += [a.source_text() + " " + a.title for a in role.accomplishments]
    return bool(CLEARANCE_SIGNS.search(" ".join(t for t in text if t)))


PROJECT_QUESTIONS = [
    ("name", "A project worth showing: something you built, led or made, at work or on "
             "your own? Give it a short name.", "Enter when there are no more"),
    ("context", "Where or for whom? An employer, a client, a course, or 'personal'.",
     "Enter to skip"),
    ("year", "When?", "Enter to skip"),
    ("what", "What did you do, in a sentence?", "Enter to skip"),
    ("result", "What came of it? A number if there is one.", "Enter to skip"),
    ("link", "A link to it, if there is one (portfolio, GitHub, article)?", "Enter to skip"),
]
LANGUAGE_QUESTIONS = [
    ("language", "A language you speak, read or sign besides the one you're answering in?",
     "Enter when there are no more"),
    ("level", "How well? Native, fluent, professional, conversational or basic.",
     "Enter to skip"),
]
VOLUNTEER_QUESTIONS = [
    ("role", "Volunteer work worth listing? Your role, e.g. 'Board treasurer' or "
             "'Weekend shelter volunteer'.", "Enter when there are no more"),
    ("org", "For which organisation?", "Enter to skip"),
    ("dates", "When? e.g. 2018 - Present", "Enter to skip"),
    ("what", "What did you do or achieve there, in a sentence? A number if there is one.",
     "Enter to skip"),
]
AWARD_QUESTIONS = [
    ("name", "An award, honour or ranking you haven't already mentioned for a job? e.g. "
             "'President's Club', 'Dean's List'.", "Enter when there are no more"),
    ("from", "Who gave it?", "Enter to skip"),
    ("year", "Year?", "Enter to skip"),
    ("why", "What was it for?", "Enter to skip"),
]
PUBLICATION_QUESTIONS = [
    ("title", "Something you published, presented or patented? Its title.",
     "Enter when there are no more"),
    ("where", "Where? A journal, conference, publisher or patent number.", "Enter to skip"),
    ("year", "Year?", "Enter to skip"),
    ("link", "A link?", "Enter to skip"),
]
MEMBERSHIP_QUESTIONS = [
    ("org", "A professional association, society, union or board you belong to?",
     "Enter when there are no more"),
    ("role", "Your role there, if more than member? e.g. 'Chapter president'.",
     "Enter to skip"),
    ("years", "Since when, or which years?", "Enter to skip"),
]
TRAINING_QUESTIONS = [
    ("name", "A course, workshop or programme you finished that didn't come with a "
             "certification?", "Enter when there are no more"),
    ("provider", "Who ran it?", "Enter to skip"),
    ("year", "Year?", "Enter to skip"),
]
TESTIMONIAL_QUESTIONS = [
    ("quote", "A line someone wrote or said about your work that you'd be glad to quote? "
              "Paste it as they put it.", "Enter when there are no more"),
    ("who", "Who said it? Name and title, e.g. 'Dana Ruiz, VP Operations'.", "Enter to skip"),
    ("relation", "How did they know you? e.g. 'my manager at Acme'.", "Enter to skip"),
]
BREAK_QUESTIONS = [
    ("dates", "Any time away from work you'd like explained once, so you don't have to "
              "each time? When was it? e.g. 2019 - 2020", "Enter if none"),
    ("reason", "What were you doing? Only what you're comfortable sharing, e.g. 'caring "
               "for a parent' or 'full-time study'.", "Enter to skip"),
    ("note", "Anything you kept up or learned in that time?", "Enter to skip"),
]
SECTIONS = [("education", EDUCATION_QUESTIONS), ("certifications", CERTIFICATION_QUESTIONS),
            ("clearance", CLEARANCE_QUESTIONS),
            ("projects", PROJECT_QUESTIONS), ("languages", LANGUAGE_QUESTIONS),
            ("volunteer", VOLUNTEER_QUESTIONS), ("awards", AWARD_QUESTIONS),
            ("publications", PUBLICATION_QUESTIONS), ("memberships", MEMBERSHIP_QUESTIONS),
            ("training", TRAINING_QUESTIONS), ("testimonials", TESTIMONIAL_QUESTIONS),
            ("career_breaks", BREAK_QUESTIONS)]
SECTION_KEYS = [k for k, _ in SECTIONS]
LABELS = {"education": "Education", "certifications": "Licences and certifications",
          **{k: h for k, h, _ in mr.EXTRA_SECTIONS}}


def section_items(rec, name: str) -> list:
    """The record's list for a section: education and certifications have
    their own fields, the optional sections live in rec.extras."""
    if name in ("education", "certifications"):
        return getattr(rec, name)
    return rec.extras.setdefault(name, [])


def _join(*parts, sep=", "):
    return sep.join(p for p in parts if p)


def format_education(d: dict) -> str:
    """'BSN in Nursing, Ohio State University, 2015; magna cum laude'"""
    head = d.get("degree", "") + (f" in {d['field']}" if d.get("field") else "")
    line = ", ".join(x for x in (head, d.get("school"), d.get("year")) if x)
    return line + (f"; {d['honours']}" if d.get("honours") else "")


def format_certification(d: dict) -> str:
    """'CCRN, AACN, 2020 (expires 2026)'"""
    line = ", ".join(x for x in (d.get("name"), d.get("issuer"), d.get("year")) if x)
    return line + (f" (expires {d['expires']})" if d.get("expires") else "")


FORMAT = {
    "education": format_education,
    "certifications": format_certification,
    # 'Ward rota app (Riverside Hospital, 2022): Built a shared rota. Cut overtime 12%. example.com'
    # 'Top Secret/SCI, active, Department of Defense, CI polygraph'
    "clearance": lambda d: _join(d.get("level"), d.get("status"), d.get("agency"),
                                 d.get("polygraph")),
    "projects": lambda d: _join(
        d.get("name", "") + (f" ({_join(d.get('context'), d.get('year'))})"
                             if d.get("context") or d.get("year") else "")
        + (":" if d.get("what") or d.get("result") else ""),
        _join(d.get("what"), d.get("result"), d.get("link"), sep=". "), sep=" "),
    "languages": lambda d: d.get("language", "") + (f" ({d['level']})" if d.get("level") else ""),
    "volunteer": lambda d: _join(_join(d.get("role"), d.get("org"), d.get("dates"))
                                 + (":" if d.get("what") else ""), d.get("what"), sep=" "),
    "awards": lambda d: _join(_join(d.get("name"), d.get("from"), d.get("year"))
                              + (":" if d.get("why") else ""), d.get("why"), sep=" "),
    "publications": lambda d: _join(_join(d.get("title"), d.get("where"), d.get("year")),
                                    d.get("link"), sep=". "),
    "memberships": lambda d: _join(d.get("org"), d.get("role"), d.get("years")),
    "training": lambda d: _join(d.get("name"), d.get("provider"), d.get("year")),
    "testimonials": lambda d: _join(f'"{d.get("quote", "").strip(chr(34))}"',
                                    _join(d.get("who"), d.get("relation")), sep=" — "),
    "career_breaks": lambda d: _join(d.get("dates", "") + (":" if d.get("reason") or d.get("note") else ""),
                                     _join(d.get("reason"), d.get("note"), sep=". "), sep=" "),
}


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
8. If the result belongs to a group ("we", "the team", "our unit"), ask once
   what their own part was, and put it in "contribution" in their words: what
   they led, built, decided or did. "I was one of six on it" is a real answer.
   Leave "contribution" "" when the work was theirs alone.

{mode}

Return ONLY JSON:
{{
  "say": "the single thing to show them: a question, or a derived figure to confirm",
  "draft": {{"title": "", "problem": "", "actions": "", "contribution": "",
             "results": "", "evidence": "", "skills": []}},
  "status": "asking" | "complete" | "role_done"
}}

- Carry forward everything already in the draft and add to it. Leave a field ""
  until you have a real answer.
- "title": a few words, no figures.
- "evidence": one of metric, derived, scope, qualitative.
- "complete" only when title, problem, actions, results and evidence are all
  filled, and, for a team result, contribution too; then "say" is one short line confirming what was recorded.
"""

DRAFT_KEYS = ("title", "problem", "actions", "contribution", "results", "evidence")

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
                "actions": {"type": "string"}, "contribution": {"type": "string"},
                "results": {"type": "string"},
                "evidence": {"type": "string", "enum": list(mr.EVIDENCE_TIERS) + [""]},
                "skills": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "problem", "actions", "contribution", "results",
                         "evidence", "skills"],
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


# --------------------------------------------------------------------------
# The engine: one turn at a time
#
# A web page cannot sit in a loop waiting on input(), so the interview is a
# state machine. Each call to `step(answer)` takes the person's answer to the
# last prompt, does whatever follows from it (saving a field, asking the model,
# recording an accomplishment) and returns the next prompt. All progress lives
# in a JSON-serialisable state held by the store, so a person can close the
# page, or the server can restart, between any two turns and lose nothing.


@dataclass
class Prompt:
    text: str
    kind: str = "question"      # question | choice | done
    hint: str = ""              # e.g. "Enter to skip"
    notes: list = field(default_factory=list)   # things to show first


STATE = "interview"
# Jumping to education keeps its own place, so it never loses an open
# conversation about a job.
BACKGROUND_STATE = "interview_background"


def role_key(role: Role) -> str:
    return f"{role.employer}||{role.title}"


class Interview:
    """
    The order a role needs: its context, then each imported resume bullet
    opened back up, then accomplishments no resume held, then one more angle,
    once. Thinnest role first.
    """

    def __init__(self, store, only: str = "", target: int = 10):
        self.store = store
        self.only = only
        self.background_only = only == BACKGROUND or only.startswith(BACKGROUND + ":")
        if only.startswith(BACKGROUND + ":") and only.partition(":")[2] not in SECTION_KEYS:
            raise ValueError(f"No section called {only.partition(':')[2]!r}. "
                             f"Sections: {', '.join(SECTION_KEYS)}")
        self.target = target
        self.rec = store.load_record()
        # one section on its own keeps its own place too
        self.key = (f"{BACKGROUND_STATE}:{only.partition(':')[2]}".rstrip(":")
                    if self.background_only else STATE)
        self.state = store.load_state(self.key)
        self.notes = []
        self.recorded = 0

    # -- plumbing ------------------------------------------------------------

    def _save_record(self):
        self.store.save_record(self.rec)

    def _save_state(self):
        if self.state is None or self.state.get("phase") == "done":
            self.store.clear_state(self.key)
        else:
            self.store.save_state(self.key, self.state)

    def role(self):
        key = self.state["queue"][self.state["qi"]]
        for r in self.rec.roles:
            if role_key(r) == key:
                return r
        return None

    def _queue(self):
        roles = by_need(self.rec)
        if self.only and not self.background_only:
            roles = [r for r in roles if self.only.lower() in r.employer.lower()]
        return [role_key(r) for r in roles]

    # -- public --------------------------------------------------------------

    def step(self, answer: str | None = None) -> Prompt:
        self.notes = []
        if self.state is None:
            self.state = self._fresh()
            answer = None
        elif answer is not None:
            self._take(answer)
        prompt = self._advance()
        prompt.notes = self.notes + prompt.notes
        self._save_state()
        return prompt

    def _fresh(self) -> dict:
        s = {"phase": "begin_role", "queue": [], "qi": 0, "ctx_i": 0, "bullet_i": 0,
             "talk": None, "lens_used": False, "new_role": {}}
        if self.background_only:
            s["phase"] = BACKGROUND
            one = self.only.partition(":")[2]
            if one:
                i = SECTION_KEYS.index(one)
                s["bg"] = {"section": i, "stop": i + 1, "q": 0, "draft": {}, "intro": False}
        elif not self.rec.roles:
            s["phase"] = "roles"
            s["new_role"] = {"stage": "employer"}
            self.notes.append("List your jobs, newest first. Include part-time, contract, "
                              "volunteer and military roles if they matter. Leave the "
                              "employer blank when you are done.")
        else:
            s["queue"] = self._queue()
        return s

    # -- taking an answer ----------------------------------------------------

    def _take(self, answer: str) -> None:
        s = self.state
        phase = s["phase"]
        a = (answer or "").strip()

        if phase == "roles":
            nr = s["new_role"]
            if nr["stage"] == "employer":
                if not a:
                    s["phase"], s["queue"], s["qi"] = "begin_role", self._queue(), 0
                    return
                nr.update(employer=a, stage="title")
            elif nr["stage"] == "title":
                nr.update(title=a, stage="dates")
            else:
                role = Role(employer=nr["employer"], title=nr.get("title", ""))
                if a:
                    role.fields["Dates"] = a
                self.rec.roles.append(role)
                self._save_record()
                s["new_role"] = {"stage": "employer"}
            return

        if phase == BACKGROUND:
            self._take_background(a)
            return

        if phase == "context":
            label = CONTEXT_QUESTIONS[s["ctx_i"]][0]
            if not gave_up(a):
                self.role().fields[label] = a
                self._save_record()
            s["ctx_i"] += 1
            return

        if phase == "offer_bullet":
            low = a.lower()
            if low in ("done", "next", "stop", "move on"):
                s["phase"] = "new"
            elif low in ("skip", "s", "no", "n"):
                s["bullet_i"] += 1
            else:
                bullet = self.role().recorded_bullets[s["bullet_i"]]
                s["talk"] = self._open_talk(bullet=bullet)
                s["phase"] = "talk"
            return

        if phase == "talk":
            if gave_up(a):
                self._end_talk("skipped")
                return
            s["talk"]["messages"].append({"role": "user", "content": a})
            s["talk"]["pending"] = True
            return

        if phase == "lens":
            if gave_up(a):
                self._next_role()
            else:
                s["talk"] = self._open_talk(seed=(s["lens_question"], a))
                s["phase"] = "talk"
                s["talk"]["pending"] = True
            return

    # -- producing the next prompt -------------------------------------------

    def _advance(self) -> Prompt:
        s = self.state
        for _ in range(500):            # each pass either returns or moves forward
            phase = s["phase"]

            if phase == "roles":
                stage = s["new_role"]["stage"]
                emp = s["new_role"].get("employer", "")
                return {"employer": Prompt("Employer (or organisation)?", hint="Enter when done"),
                        "title": Prompt(f"Your job title at {emp}?"),
                        "dates": Prompt("Dates? (anything readable, e.g. 2019 - 2022)")}[stage]

            if phase == BACKGROUND:
                prompt = self._background_prompt()
                if prompt is not None:
                    return prompt
                s["background_done"] = True
                s["phase"] = "done"
                continue

            if phase != "done" and s["qi"] >= len(s["queue"]):
                # roles finished: education and certifications, once
                s["phase"] = "done" if s.get("background_done") or self.only else BACKGROUND
                continue

            if phase == "done":
                added = s.get("background_added", 0)
                extra = f" and {added} other entr{'y' if added == 1 else 'ies'}" \
                    if added else ""
                return Prompt(f"That's everything for now. {self.recorded} accomplishment(s)"
                              f"{extra} recorded this session.", kind="done")

            role = self.role()
            if role is None:                # deleted by hand between turns
                self._next_role()
                continue

            if phase == "begin_role":
                self.notes.append(f"{role.label()}: "
                                  f"{mr.coverage_note(role, self.target, prompt=True)}")
                s.update(phase="context", ctx_i=0, bullet_i=0, lens_used=False, talk=None)
                continue

            if phase == "context":
                while s["ctx_i"] < len(CONTEXT_QUESTIONS) and \
                        role.fields.get(CONTEXT_QUESTIONS[s["ctx_i"]][0]):
                    s["ctx_i"] += 1
                if s["ctx_i"] < len(CONTEXT_QUESTIONS):
                    q = CONTEXT_QUESTIONS[s["ctx_i"]][1]
                    return Prompt(q.format(employer=role.employer, title=role.title),
                                  hint="Enter to skip")
                s["phase"] = "offer_bullet"
                continue

            if phase == "offer_bullet":
                if s["bullet_i"] < len(role.recorded_bullets):
                    return Prompt(f"From your resume: \"{role.recorded_bullets[s['bullet_i']]}\"\n"
                                  f"Open this one up to find what is behind it?",
                                  kind="choice", hint="Enter = yes, 'skip', or 'done' to move on")
                s["phase"] = "new"
                continue

            if phase == "new":
                s["talk"] = self._open_talk()
                s["talk"]["pending"] = True
                s["phase"] = "talk"
                continue

            if phase == "talk":
                t = s["talk"]
                if not t.get("pending"):
                    return Prompt(t["say"] or "Go on?")
                prompt = self._model_turn()
                if prompt is not None:
                    return prompt
                continue

            if phase == "lens":
                if s["lens_used"]:
                    self._next_role()
                    continue
                s["lens_used"] = True
                name, question = pick_lens(role)
                s["lens_question"] = question
                return Prompt(f"One more angle before we move on ({name}): {question}",
                              hint="Enter to skip")
        raise RuntimeError("interview made no progress")

    # -- education and certifications ----------------------------------------

    def _background_prompt(self):
        s = self.state
        bg = s.setdefault("bg", {"section": 0, "q": 0, "draft": {}, "intro": False})
        asked_for = bg.get("stop") == bg["section"] + 1     # this one section, by name
        while (bg["section"] < bg.get("stop", len(SECTIONS)) and not asked_for
               and SECTIONS[bg["section"]][0] == "clearance"
               and not clearance_relevant(self.rec)):
            bg.update(section=bg["section"] + 1, q=0, draft={}, intro=False)
        if bg["section"] >= bg.get("stop", len(SECTIONS)):
            return None
        name, questions = SECTIONS[bg["section"]]
        if not bg["intro"]:
            bg["intro"] = True
            have = section_items(self.rec, name)
            label = LABELS[name]
            if have:
                self.notes.append(f"{label} on your record: " + "; ".join(have)
                                  + ". Add any that are missing.")
            elif name in ("education", "certifications"):
                self.notes.append(f"{label}: nothing recorded yet. Include anything an "
                                  f"employer could check" + (", even a high-school diploma "
                                  "or a training programme." if name == "education" else "."))
            else:
                self.notes.append(f"{label} (optional; Enter skips it).")
        key, question, hint = questions[bg["q"]]
        if bg["q"]:
            first = bg["draft"].get(questions[0][0], "")
            first = first if len(first) <= 40 else first[:37].rstrip() + "..."
            question = f"{first}: {question[0].lower()}{question[1:]}"
        return Prompt(question, hint=hint)

    def _take_background(self, a: str) -> None:
        bg = self.state.setdefault("bg", {"section": 0, "q": 0, "draft": {}, "intro": True})
        name, questions = SECTIONS[bg["section"]]
        key = questions[bg["q"]][0]
        if bg["q"] == 0 and gave_up(a):            # blank first answer ends this list
            bg.update(section=bg["section"] + 1, q=0, draft={}, intro=False)
            return
        if not gave_up(a):
            bg["draft"][key] = a
        bg["q"] += 1
        if bg["q"] < len(questions):
            return
        line = FORMAT[name](bg["draft"])
        items = section_items(self.rec, name)
        if mr._squash(line) not in {mr._squash(x) for x in items}:
            items.append(line)
            self._save_record()
            self.state["background_added"] = self.state.get("background_added", 0) + 1
            self.notes.append(f"recorded: {line}")
        bg.update(q=0, draft={})

    # -- conversations with the model ----------------------------------------

    def _open_talk(self, bullet: str = "", seed=None) -> dict:
        role = self.role()
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
        return {"messages": messages, "bullet": bullet, "say": "", "pending": True,
                "draft": {k: "" for k in DRAFT_KEYS},
                "skills": []}

    def _model_turn(self):
        """Ask the model for the next move. Returns a Prompt to show, or None
        when the conversation ended and the caller should move on."""
        s, t = self.state, self.state["talk"]
        role = self.role()
        reply = llm.request_json(list(t["messages"]), 4096, "interview", schema=INTERVIEW_SCHEMA)
        status = reply.get("status", "asking")
        d = reply.get("draft") or {}
        for key in DRAFT_KEYS:
            t["draft"].setdefault(key, "")    # a talk saved before a key existed
            if d.get(key):
                t["draft"][key] = str(d[key]).strip()
        for sk in d.get("skills") or []:
            if sk and sk not in t["skills"]:
                t["skills"].append(sk)
        t["messages"].append({"role": "assistant", "content": json.dumps(reply)})
        t["say"] = (reply.get("say") or "").strip()
        t["pending"] = False

        if status == "role_done":
            if t["say"]:
                self.notes.append(t["say"])
            self._end_talk("role_done")
            return None

        if status == "complete":
            draft = Accomplishment(**t["draft"])
            dupe = near_duplicate(role, draft.title, ignore=t["bullet"])
            if dupe:
                t["messages"].append({"role": "user", "content":
                                      f"That duplicates {dupe!r}, already recorded. "
                                      f"Ask for a different one."})
                t["pending"] = True
                return None
            if draft.evidence not in mr.EVIDENCE_TIERS:
                draft.evidence = "qualitative"
            role.accomplishments.append(draft)
            if t["bullet"] and t["bullet"] in role.recorded_bullets:
                role.recorded_bullets.remove(t["bullet"])
            link_skills(self.rec, t["skills"], draft.title)
            self._save_record()
            self.recorded += 1
            self.notes.append(f"recorded: {draft.title} ({draft.evidence})")
            self._end_talk("complete")
            return None

        return Prompt(t["say"] or "Go on?")

    def _end_talk(self, outcome: str) -> None:
        s = self.state
        was_bullet = bool(s["talk"] and s["talk"]["bullet"])
        s["talk"] = None
        if was_bullet:
            # a completed expansion removed its bullet, so the index already
            # points at the next one; a skipped one has to step past it
            if outcome != "complete":
                s["bullet_i"] += 1
            s["phase"] = "offer_bullet"
        elif outcome == "complete":
            s["phase"] = "new"
        else:
            s["phase"] = "lens"

    def _next_role(self) -> None:
        s = self.state
        s["qi"] += 1
        s["phase"] = "begin_role"
        s["talk"] = None


def link_skills(rec: mr.Record, names: list, title: str) -> None:
    """Skills an accomplishment showed become evidence for those skills."""
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        skill = rec.skill(name)
        if skill is None:
            skill = mr.Skill(name=name, category="From the interview", source="interview")
            rec.skills.append(skill)
        if title not in skill.evidence:
            skill.evidence.append(title)


def run(interview: Interview, ask, say=print) -> Interview:
    """Drive an interview from a terminal, or anything else that can answer a
    question with a line of text. Ctrl-C (Stop) leaves the state saved."""
    prompt = interview.step()
    while True:
        for n in prompt.notes:
            say(f"  {n}")
        if prompt.kind == "done":
            say(prompt.text)
            return interview
        text = prompt.text + (f"\n  ({prompt.hint})" if prompt.hint else "")
        prompt = interview.step(ask(text))
