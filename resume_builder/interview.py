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
from . import skills as sk
from .record import Accomplishment, Role


class Stop(Exception):
    """The person chose to leave. Not an error."""


GIVE_UP = {"done", "stop", "next", "no more", "skip", "nothing", "that's all",
           "thats all", "no", "n", "none", "move on", "i don't know", "dont know"}


def gave_up(answer: str) -> bool:
    return not answer or answer.strip().lower().rstrip(".!") in GIVE_UP


# Leaving a conversation about one accomplishment takes an explicit word. "No"
# is an answer there: the coach asks "is that figure right?", and "no" to that
# must go back to the coach, not throw the story away. "I don't know" is the
# coach's to handle too (it moves down the evidence ladder).
LEAVE_TALK = {"done", "stop", "next", "skip", "move on", "no more", "that's all",
              "thats all", "skip this", "skip this one", "let's move on", "lets move on"}


def left_talk(answer: str) -> bool:
    return not answer or answer.strip().lower().rstrip(".!") in LEAVE_TALK


# "I don't remember that detail" answers a plain question with nothing. Short
# and without a figure, so "I don't recall exactly, about 30%" still counts.
NON_ANSWER = re.compile(r"^(i\s+)?(don'?t|do not|can'?t|cannot|couldn'?t)\s+(remember|recall|"
                        r"know|say)|^(not sure|no idea|unknown|n/?a|unsure|not applicable)\b", re.I)


# "I don't have a Student Teacher role at Fairhaven": a job imported from an
# old resume that the person says was never theirs. Plain negatives ("I didn't
# have direct reports in that role") are answers, so a denial has to name the
# job, or say outright that it was not theirs.
DISOWN = re.compile(r"\bnever worked (at|for|there)\b|\bthat wasn'?t me\b|"
                    r"\bnot (a )?(job|role) i (had|held|did)\b", re.I)
DENY = re.compile(r"\b(don'?t|do not|didn'?t|did not|never)\s+(have|had|hold|held)\s+"
                  r"(a|an|the|that|this|any)?\s*(?P<rest>.{0,60})", re.I)


def disowns(answer: str, role) -> bool:
    a = answer or ""
    if DISOWN.search(a):
        return True
    m = DENY.search(a)
    return bool(m and role is not None and role.title
                and mr._squash(role.title) in mr._squash(m.group("rest")))


def blank(answer: str) -> bool:
    """A skip, or a non-answer to a plain factual question. Not used in the
    conversation with the model, which handles "I don't remember" itself."""
    a = (answer or "").strip()
    return gave_up(a) or (bool(NON_ANSWER.search(a)) and len(a.split()) <= 8
                          and not re.search(r"\d", a))


# --------------------------------------------------------------------------
# Role context

# (field, question). Worded for any job: a nurse, a plant manager and a
# software engineer must all be able to answer each one.
CONTEXT_QUESTIONS = [
    ("Employment type", "What kind of job was {title} at {employer}?"),
    # Promotions are easy to lose: people fold them into a title or mention
    # them under recognition. Each title is its own entry with its own dates,
    # and the resume stacks them under one company heading.
    ("Other titles", "Did you hold any other title at {employer}, before or after {title}? "
                     "e.g. a promotion. Give each with its dates, e.g. 'Staff RN, 2018 - "
                     "2021'; separate several with ';'."),
    ("Company", "In a sentence, what was {employer}? What it does, roughly how big "
                "(people, a revenue range, sites, beds, students -- whatever is natural), and the industry."),
    ("Challenge", "What were you brought in to do, or what problem was waiting for you "
                  "when you started as {title}?"),
    ("Authority", "Did you lead or supervise anyone there? How many, and in what roles?"),
    ("Budget", "Were you responsible for a budget, revenue target, or expensive "
               "equipment or inventory? Roughly what size? A range is fine, e.g. "
               "'$2-5M', 'a fleet of 40 trucks'."),
    ("Reported to", "What was the title of the person you reported to?"),
    ("Territory", "What did your work cover: one site, a region, national, international, "
                  "a set of clients or accounts?"),
    # The result, not the process: "how was it measured?" gets "annual review".
    ("Results against targets", "Did you have targets, KPIs or rankings there? How did "
                                "you do against them? e.g. '112% of quota in 2023', 'ranked "
                                "3rd of 40 reps', '99.9% uptime against a 99.5% target', "
                                "'top review rating two years running'."),
    ("Recognition", "Any awards, promotions, top ratings or formal recognition there?"),
]


# Questions answered by picking one of these; a typed answer is kept as typed.
OPTIONS = {"Employment type": mr.PAID_TYPES}


def dropped(answer: str, options) -> list:
    """The options a person unticked: numbers or names, separated by commas
    or semicolons. The web page sends "drop: A; B". Enter, or "none", keeps
    every one."""
    a = re.sub(r"^\s*drop\s*:", "", answer or "", flags=re.I).strip()
    if gave_up(a):
        return []
    out = []
    for part in re.split(r"[,;\n]", a):
        part = part.strip().strip(".")
        if not part:
            continue
        if part.isdigit() and 1 <= int(part) <= len(options):
            name = options[int(part) - 1]
        else:
            name = next((o for o in options if mr._squash(o) == mr._squash(part)), None)
        if name and name not in out:
            out.append(name)
    if not out and DROP_WORDS.search(a):
        # "I haven't used Java or Kubernetes in years": names in a sentence
        low = f" {a.lower()} "
        out = [o for o in options
               if re.search(r"(?<![\w+#])" + re.escape(o.lower()) + r"(?![\w+#])", low)]
    return out


DROP_WORDS = re.compile(r"\b(drop|remove|untick|delete|except|not|never|don'?t|haven'?t|"
                        r"didn'?t|no longer)\b", re.I)


def pick(answer: str, options) -> str:
    """'3', 'contract', '2. Part-time, while I studied' or 'I was part time' ->
    the option. Anything that names no option, or several, as typed."""
    a = (answer or "").strip()
    m = re.match(r"^(\d+)\b", a)
    if m and 1 <= int(m.group(1)) <= len(options):
        return options[int(m.group(1)) - 1]
    if not a:
        return a
    hits = [o for o in options if o.lower().startswith(a.lower())]
    if len(hits) == 1:
        return hits[0]
    text = " " + re.sub(r"[^a-z0-9]+", " ", a.lower()) + " "
    named = [o for o in options
             if " " + re.sub(r"[^a-z0-9]+", " ", o.lower()).strip() + " " in text
             or " " + re.sub(r"[^a-z0-9]+", " ", o.lower()).split()[0] + " " in text]
    return named[0] if len(named) == 1 else a


# --------------------------------------------------------------------------
# Work outside paid jobs
#
# Volunteering, a board seat, a project, a capstone: for a student, a returner
# or a career changer this is often the strongest evidence they have. Each one
# is kept with the jobs and gets the same coached conversation, so it has a
# problem, actions and a result rather than a one-line mention.

OUTSIDE_INTRO = (
    "Now, work outside a paid job. It counts as much as a job when you put real "
    "effort in and something changed because of you: volunteering, a board or "
    "committee seat, a community or faith group, a personal or side project, a "
    "course or capstone project, or organising something for family or neighbours. "
    "Answer it the way you answered for your jobs: what the situation was, what "
    "you did, and what came of it, with a number where there is one (money raised, "
    "people served, members, users, hours). Skip anything you'd rather not share.")
OUTSIDE_QUESTIONS = {
    "what": ("Work outside a paid job worth recording? Your role or the project's name, "
             "e.g. 'Treasurer', 'Built a budgeting app', 'Capstone project'.",
             "Enter if there's none"),
    "more": ("Another piece of work outside a paid job? Your role or the project's name.",
             "Enter if there's no more"),
    "where": ("Where, or for whom? An organisation, a school, or 'personal'.", "Enter to skip"),
    "kind": ("What kind of work was it?", "Pick one, or type your own"),
    "dates": ("When? e.g. 2021 - 2023", "Enter to skip"),
}


# Shown at the start of every session about jobs. The coach (rule 9) backs it
# up mid-conversation, which is where the temptation actually arises.
CONFIDENTIAL_NOTE = (
    "Please don't enter confidential figures: exact revenue, profit, margins, client "
    "budgets, prices or salaries. A percentage, a range or a ranking works just as "
    "well on a resume, e.g. 'grew revenue 32%' or 'ran an eight-figure P&L'.")


# Asked at the end of a job only when it holds fewer than THIN_ROLE
# accomplishments and resume bullets. A strong record never needs duties: a
# result beats a duty on every resume. A thin job (hourly, early career, long
# ago) has little else, and its duties carry the posting's keywords.
DUTIES_QUESTION = ("What did a normal week involve there? The regular duties, with volumes "
                   "where you know them: calls a day, accounts, patients, orders, reports.")
THIN_ROLE = 3
DISOWN_OPTIONS = ("Yes, remove it", "No, it was mine")


# --------------------------------------------------------------------------
# Who the person is and what they want next
#
# A record started from nothing has no name on it and no target, and the
# pipeline searches by the target titles. Each is asked once when missing; a
# skipped one is remembered and not asked again.

# (where, label, question, hint)
PROFILE_QUESTIONS = [
    ("contact", "Name", "Your name, as it should appear on a resume?", "Enter to skip"),
    ("contact", "Email", "The email address employers should use?", "Enter to skip"),
    ("contact", "Telephone", "Phone number?", "Enter to skip"),
    ("contact", "Location", "Where are you based? City and state or country is enough.",
     "Enter to skip"),
    ("contact", "Linkedin", "A LinkedIn profile, portfolio or personal website?", "Enter to skip"),
    ("target", "Titles", "What jobs are you aiming for next? Job titles, separated by ';'.",
     "Enter to skip"),
    ("target", "Industries", "Which industries or kinds of employer? Separated by ';'.",
     "Enter to skip"),
    ("target", "Locations", "Where do you want to work? Cities, regions or 'remote'.",
     "Enter to skip"),
]
# Asked after the jobs, when the person has their whole career in mind. A
# cover letter needs the thread between the jobs, which no single job holds.
STORY_QUESTIONS = [
    ("sets_apart", "Best at", "What are you best at: the thing colleagues and managers "
                              "come to you for?", "Enter to skip"),
    ("sets_apart", "Career thread", "Looking across your jobs, what connects them? The "
                                    "thread a hiring manager should see, especially if "
                                    "you've changed fields.", "Enter to skip"),
    ("target", "Next move", "What do you want from your next role, and why now?",
     "Enter to skip"),
]
ASKED_STATE = "interview_asked"     # labels already asked, kept across sessions


LEVEL_WORDS = {"senior", "sr", "junior", "jr", "lead", "principal", "head", "chief", "staff",
               "manager", "director", "supervisor", "assistant", "associate", "deputy",
               "vp", "vice", "president", "executive", "ii", "iii", "iv", "trainee"}


def _overlap(a: str, b: str) -> float | None:
    """How much two date ranges overlap, as a share of the shorter. None when
    either doesn't parse."""
    from .health import months_between, parse_range
    ra, rb = parse_range(a or ""), parse_range(b or "")
    if not ra or not rb:
        return None
    start, end = max(ra[0], rb[0]), min(ra[1], rb[1])
    shorter = max(1, min(months_between(*ra), months_between(*rb)))
    return max(0, months_between(start, end)) / shorter


def same_job(rec, employer: str, title: str, dates: str = ""):
    """A job already on the record under a slightly different wording: the
    same employer (or one name extending the other) and a title sharing most
    of its words. "Registered Nurse, Medical ICU (Relief Charge Nurse since
    2021)" is the "Registered Nurse, Medical ICU" job; "Charge Nurse" after
    "Registered Nurse" is a promotion, not a duplicate."""
    def words(t):
        return set(re.findall(r"[a-z]{2,}", (t or "").lower())) - {"and", "the", "of", "since"}
    e = mr._squash(employer)
    for r in rec.roles:
        other = mr._squash(r.employer)
        if not e or not other or not (e == other or e.startswith(other) or other.startswith(e)):
            continue
        a, b = words(title), words(r.title)
        overlap = _overlap(dates, r.fields.get("Dates", ""))
        if overlap is not None and overlap < 0.5:
            continue                        # different years: another title there
        if mr._squash(title) == mr._squash(r.title):
            return r
        if (a ^ b) & LEVEL_WORDS:
            continue                        # "Senior Analyst" after "Analyst" is a promotion
        if a and b and len(a & b) / min(len(a), len(b)) >= 0.6:
            return r
    return None


TITLE_DATES = (r"^(?P<title>.+?)[\s,(]+(?P<dates>(?:[A-Za-z]{3,9}\.?\s+)?\d{4}\s*(?:-|–|—|to)\s*"
               r"(?:(?:[A-Za-z]{3,9}\.?\s+)?\d{4}|present|now|current|today))\)?")
DATES_AT_END = re.compile(TITLE_DATES + r"\.?\s*$", re.I)
# "Relief Charge Nurse, March 2021 - present. I still work as a staff RN":
# people explain. The title ends at its dates; the sentence after is not part
# of it.
DATES_THEN_MORE = re.compile(TITLE_DATES + r"[.,:]\s+\S", re.I)


def split_titles(answer: str) -> list:
    """'Staff RN, 2018 - 2021; Nurse Intern 2017 - 2018' ->
    [('Staff RN', '2018 - 2021'), ('Nurse Intern', '2017 - 2018')]."""
    out = []
    for part in re.split(r"[;\n]", answer or ""):
        part = part.strip().strip(".")
        if not part:
            continue
        m = DATES_AT_END.match(part) or DATES_THEN_MORE.match(part)
        out.append((m.group("title").strip(" ,"), m.group("dates").strip()) if m
                   else (part, ""))
    return out


EMPLOYER_FIELDS = ("Company", "Location", "Employment type")


def siblings(rec, role) -> list:
    """Other paid titles at the same employer."""
    return [r for r in rec.roles if r is not role and not r.outside()
            and mr._squash(r.employer) == mr._squash(role.employer)]


def fact(rec, where: str, label: str) -> str:
    """A profile or story answer already on the record, or ''."""
    if where == "sets_apart":
        for line in rec.sets_apart:
            head, sep, value = line.partition(":")
            if sep and head.strip().lower() == label.lower():
                return value.strip()
        return ""
    return getattr(rec, where).get(label, "")


def set_fact(rec, where: str, label: str, value: str) -> None:
    if where == "sets_apart":
        rec.sets_apart = [x for x in rec.sets_apart
                          if x.partition(":")[0].strip().lower() != label.lower()]
        rec.sets_apart.append(f"{label}: {value}")
    else:
        getattr(rec, where)[label] = value


# --------------------------------------------------------------------------
# Education, certifications and the optional sections
#
# Asked once the roles are done, or on their own. Plain questions, no model:
# these are facts the person knows and a resume needs exactly as they are.
# Each list ends when the first question is left blank, so a section that
# doesn't apply costs one Enter.

BACKGROUND = "background"       # Interview(only=BACKGROUND) asks just these;
                                # "background:languages" asks one section

def _programme(d: dict) -> bool:
    """A trade school, bootcamp or other programme: not a degree (associate or
    higher) and not a high-school diploma. Its hours and what it taught say
    more than a GPA."""
    q = d.get("degree", "")
    return bool(q) and not mr.DEGREE.search(q) and not NOT_PROGRAMME.search(q)


NOT_PROGRAMME = re.compile(r"(?i:high[- ]?school|secondary|\bassociate)|\b(GED|AAS|AA|AS|A\.A|A\.S)\b")

# (key, question, hint[, ask_if(draft)])
EDUCATION_QUESTIONS = [
    ("degree", "Your next qualification: a degree, diploma, apprenticeship, bootcamp or "
               "high-school diploma? Write it as it should read, e.g. 'BSN' or 'MBA'.",
     "Enter when there are no more"),
    ("field", "Field of study or major?", "Enter to skip"),
    ("school", "School, college or institution?", "Enter to skip"),
    ("year", "Year finished, or the year you expect to?", "Enter to skip"),
    ("honours", "Honours, a GPA worth showing, or a thesis title?", "Enter to skip"),
    ("hours", "How long was the programme? Hours or months, e.g. '1,500 hours'.",
     "Enter to skip", _programme),
    ("taught", "What did it teach you to do? Name the skills, tools or methods, "
               "separated by ';'.", "Enter to skip", _programme),
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
# Asked only when no degree is recorded. Employers that drop a degree
# requirement still lean on the degree when comparing candidates, unless
# something else shows the skill; formal training on the job is one of the few
# substitutes they trust. Filed with "Training and courses".
JOB_TRAINING_QUESTIONS = [
    ("name", "Did any of your jobs give you formal training: a programme, an "
             "apprenticeship, or a certification you earned there? Its name.",
     "Enter when there are no more"),
    ("where", "At which job, or who ran it?", "Enter to skip"),
    ("year", "Year finished?", "Enter to skip"),
    ("length", "How long was it? Hours, weeks or months.", "Enter to skip"),
    ("skills", "What did it teach you to do? Name the skills, tools or methods, "
               "separated by ';'.", "Enter to skip"),
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
SECTIONS = [("education", EDUCATION_QUESTIONS), ("job_training", JOB_TRAINING_QUESTIONS),
            ("certifications", CERTIFICATION_QUESTIONS),
            ("clearance", CLEARANCE_QUESTIONS),
            ("projects", PROJECT_QUESTIONS), ("languages", LANGUAGE_QUESTIONS),
            ("volunteer", VOLUNTEER_QUESTIONS), ("awards", AWARD_QUESTIONS),
            ("publications", PUBLICATION_QUESTIONS), ("memberships", MEMBERSHIP_QUESTIONS),
            ("training", TRAINING_QUESTIONS), ("testimonials", TESTIMONIAL_QUESTIONS),
            ("career_breaks", BREAK_QUESTIONS)]
SECTION_KEYS = [k for k, _ in SECTIONS]
# Sections asked only when they apply; asking for one by name always asks it.
ASK_IF = {"clearance": lambda rec: clearance_relevant(rec),
          "job_training": lambda rec: not mr.has_degree(rec),
          # volunteering is asked in full with work outside paid jobs; this
          # one-line list is kept for imported entries and asked only by name
          "volunteer": lambda rec: False}
LABELS = {"education": "Education", "certifications": "Licences and certifications",
          "job_training": "Training on the job",
          **{k: h for k, h, _ in mr.EXTRA_SECTIONS}}


def section_items(rec, name: str) -> list:
    """The record's list for a section: education and certifications have
    their own fields, the optional sections live in rec.extras."""
    if name in ("education", "certifications"):
        return getattr(rec, name)
    if name == "job_training":
        return rec.extras.setdefault("training", [])
    return rec.extras.setdefault(name, [])


def _join(*parts, sep=", "):
    return sep.join(p for p in parts if p)


def _wanted(question: tuple, draft: dict) -> bool:
    """A follow-up with a condition is asked only when the answers so far meet it."""
    return len(question) < 4 or question[3](draft)


def format_education(d: dict) -> str:
    """'BSN in Nursing, Ohio State University, 2015; magna cum laude'"""
    head = d.get("degree", "") + (f" in {d['field']}" if d.get("field") else "")
    line = ", ".join(x for x in (head, d.get("school"), d.get("year")) if x)
    line += f"; {d['honours']}" if d.get("honours") else ""
    line += f" ({d['hours']})" if d.get("hours") else ""
    return line + (f". Skills: {d['taught']}" if d.get("taught") else "")


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
    # 'Green Belt programme (Acme Foods, 2019, 40 hours). Skills: Lean; Root cause analysis'
    "job_training": lambda d: _join(
        d.get("name", "") + (f" ({_join(d.get('where'), d.get('year'), d.get('length'))})"
                             if d.get("where") or d.get("year") or d.get("length") else ""),
        "Skills: " + "; ".join(s.strip() for s in d["skills"].split(";") if s.strip())
        if d.get("skills") else "", sep=". "),
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
5. If they cannot remember a detail, accept it at once, never ask for it again,
   and move down the ladder: a qualitative result is a real answer, so finish
   the draft and set "complete". Set "role_done" only when they want to move on
   or cannot recall the accomplishment itself. Never push twice.
6. Do not ask about anything in already_recorded, or again about anything in
   not_remembered: they have already said they can't recall it. If they start
   describing one of those, say so and ask for a different one.
7. Put the skills, tools, methods, equipment, procedures or know-how the
   accomplishment took in "skills": the ones they name, in their words or the
   standard industry term. Before "complete", unless they have already named
   them, ask once: "What tools, methods or know-how did that take?" A nurse
   names equipment and protocols, a plant manager a method or a machine, an
   analyst software. If they name none, leave "skills" empty; never add one
   they did not mention. When the ROLE has "explain_skills": true, the first
   time you ask it, add one short sentence saying what counts: "By skills I
   mean the tools, software, equipment, methods, procedures, regulations or
   specialist knowledge you used: the things a job posting in your field
   would list." Never explain it again after that.
8. If the result belongs to a group ("we", "the team", "our unit"), ask once
   what their own part was, and put it in "contribution" in their words: what
   they led, built, decided or did. "I was one of six on it" is a real answer.
   Leave "contribution" "" when the work was theirs alone.
9. Do not ask for figures an employer or client would treat as confidential:
   exact revenue, profit, margins, budgets, prices, salaries. Ask for a form
   that is safe to publish instead (a percentage change, a range or order of
   magnitude such as "an eight-figure P&L", a ranking, a count of people or
   sites). If they hesitate, offer one of those and do not press. A figure
   they give you is theirs to share: they were told not to enter confidential
   ones, so record it as given and do not question it.

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
    kind: str = "question"      # question | choice | multi | done
    hint: str = ""              # e.g. "Enter to skip"
    options: list = field(default_factory=list)  # choice: pick one; multi: untick any
    notes: list = field(default_factory=list)   # things to show first


# Skills an old resume listed are believed: they are the person's own claim.
# They are shown once, all ticked, with the one risk worth naming, and only
# what the person unticks is dropped.
SKILLS_CHECK = ("Your old resume lists these skills, and they stay on your record. "
                "Interviewers can ask about anything on your resume, so untick any "
                "you couldn't discuss today.")
SKILLS_HINT = "Enter keeps them all; or type the numbers or names to drop, e.g. '2, 5'"
LAST_USED = "When did you last use {name}?"
LAST_USED_HINT = "e.g. 'current' or '2021'. Enter to skip; 'done' skips the rest"
STOP_ASKING = {"done", "stop", "move on", "skip all", "skip the rest", "no more"}


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
        elif self.only:
            s["queue"] = self._queue()
        else:
            s["phase"] = "profile"
        if not self.background_only:
            self.notes.append(CONFIDENTIAL_NOTE)
        return s

    def _start_roles(self) -> None:
        """The job list: all of it on a new record, then any job that's new
        or missing on every later visit, so the record keeps up with a career."""
        s = self.state
        s["phase"] = "roles"
        s["new_role"] = {"stage": "employer", "adding": bool(self.rec.roles)}
        if self.rec.roles:
            self.notes.append("Jobs on your record: " + "; ".join(
                r.label() + (f" ({r.fields['Dates']})" if r.fields.get("Dates") else "")
                for r in self.rec.roles) + ".")
        else:
            self.notes.append("List your jobs, newest first. Include part-time, contract "
                              "and military roles. Promoted, or changed title, at one "
                              "employer? Enter the latest title first; you'll be asked "
                              "for the earlier ones, each with its own dates, so the "
                              "promotion shows. Leave the employer blank when you are done.")

    def _add_title(self, employer: str, title: str, dates: str, queue: bool = False) -> None:
        """Another title at an employer already listed: its own entry, so the
        promotion and its dates survive to the resume."""
        if not title or same_job(self.rec, employer, title, dates):
            return
        role = Role(employer=employer, title=title)
        if dates:
            role.fields["Dates"] = dates
        first = next((r for r in self.rec.roles if mr._squash(r.employer) == mr._squash(employer)),
                     None)
        if first is not None:
            for f in EMPLOYER_FIELDS:
                if first.fields.get(f):
                    role.fields[f] = first.fields[f]
        # beside the employer's other titles, so the record reads newest first
        at = max((i for i, r in enumerate(self.rec.roles)
                  if mr._squash(r.employer) == mr._squash(employer)), default=len(self.rec.roles) - 1)
        self.rec.roles.insert(at + 1, role)
        self._save_record()
        self.notes.append(f"Added {role.label()}" + (f" ({dates})" if dates else "") + ".")
        if queue:
            self.state["queue"].append(role_key(role))

    def _close_titles(self, nr: dict) -> None:
        """Every title at this employer knows the others were asked about."""
        names = [r.title for r in self.rec.roles
                 if mr._squash(r.employer) == mr._squash(nr["employer"])]
        for r in self.rec.roles:
            if mr._squash(r.employer) == mr._squash(nr["employer"]) and not r.fields.get("Other titles"):
                r.fields["Other titles"] = "; ".join(t for t in names if t != r.title) or "None"
        self._save_record()

    def _asked(self) -> set:
        return set((self.store.load_state(ASKED_STATE) or {}).get("asked", []))

    def _remember_asked(self, key: str, label: str) -> None:
        """Note a question as asked. Read-modify-write: the same state keeps
        the profile labels, each job's labels and the skills already shown."""
        st = self.store.load_state(ASKED_STATE) or {}
        if key == "asked":
            st["asked"] = sorted(set(st.get("asked", [])) | {label})
        else:
            jobs = st.setdefault("jobs", {})
            jobs[key] = sorted(set(jobs.get(key, [])) | {label})
        self.store.save_state(ASKED_STATE, st)

    def _job_asked(self, role) -> set:
        """Job questions this person skipped or answered "no" to: never asked
        again, as the profile questions aren't."""
        return set(((self.store.load_state(ASKED_STATE) or {}).get("jobs") or {})
                   .get(role_key(role), []))

    def _fact_prompt(self, questions) -> Prompt | None:
        """The next missing, never-asked profile or story question."""
        asked = self._asked()
        for where, label, question, hint in questions:
            if not fact(self.rec, where, label) and label not in asked:
                self.state["fact"] = [where, label]
                return Prompt(question, hint=hint)
        return None

    def _take_fact(self, a: str) -> None:
        where, label = self.state.pop("fact", None) or (None, None)
        if not label:
            return
        if not blank(a):
            set_fact(self.rec, where, label, a)
            self._save_record()
        self._remember_asked("asked", label)

    # -- taking an answer ----------------------------------------------------

    def _take(self, answer: str) -> None:
        s = self.state
        phase = s["phase"]
        a = (answer or "").strip()

        if phase in ("profile", "story"):
            self._take_fact(a)
            return

        if phase == "roles":
            nr = s["new_role"]
            if nr["stage"] == "employer":
                if gave_up(a):                     # "no", "done" or Enter ends the list
                    s["phase"], s["queue"], s["qi"] = "begin_role", self._queue(), 0
                    return
                nr.update(employer=a, stage="title")
            elif nr["stage"] == "title":
                nr.update(title=a, stage="dates")
            elif nr["stage"] == "dates":
                have = same_job(self.rec, nr["employer"], nr.get("title", ""), a)
                if have:
                    self.notes.append(f"{have.label()} is already on your record.")
                    s["new_role"] = {"stage": "employer", "adding": nr.get("adding", False)}
                    return
                role = Role(employer=nr["employer"], title=nr.get("title", ""))
                if a:
                    role.fields["Dates"] = a
                self.rec.roles.append(role)
                self._save_record()
                nr.update(stage="other", others=[])
            elif nr["stage"] == "other":
                if blank(a):
                    self._close_titles(nr)
                    s["new_role"] = {"stage": "employer", "adding": nr.get("adding", False)}
                else:
                    nr.update(other=a, stage="other_dates")
            else:                                  # other_dates
                self._add_title(nr["employer"], nr["other"], "" if blank(a) else a)
                nr["others"].append(nr["other"])
                nr["stage"] = "other"
            return

        if phase == BACKGROUND:
            self._take_background(a)
            return

        if phase == "outside":
            self._take_outside(a)
            return

        if phase == "skills":
            self._take_skills(a)
            return

        if phase == "disown":
            role = self.role()
            if pick(a, DISOWN_OPTIONS) == DISOWN_OPTIONS[0] or a.strip().lower() in ("y", "yes"):
                self.rec.roles.remove(role)
                self._save_record()
                self.notes.append(f"removed: {role.label()}")
                self._next_role()
            else:
                s.setdefault("kept", {})[role_key(role)] = True
                s["phase"] = "context"
                s["ctx_i"] += 1
            return

        if phase == "context":
            label = CONTEXT_QUESTIONS[s["ctx_i"]][0]
            if disowns(a, self.role()) and not s.get("kept", {}).get(role_key(self.role())):
                s["phase"] = "disown"
                return
            if label == "Other titles":
                role = self.role()
                titles = [] if blank(a) else split_titles(a)
                for title, dates in titles:
                    self._add_title(role.employer, title, dates, queue=True)
                role.fields[label] = "; ".join(t for t, _ in titles) or "None"
                self._save_record()
                s["ctx_i"] += 1
                return
            if label in OPTIONS:
                # "I don't remember, so I can't pick one" names no option and
                # is no answer, however long; it must not be kept as the type.
                a = pick(a, OPTIONS[label])
                if a not in OPTIONS[label] and NON_ANSWER.search(a.strip()):
                    a = ""
            if not blank(a):
                self.role().fields[label] = a
                self._save_record()
            else:
                self._remember_asked(role_key(self.role()), label)
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
            if left_talk(a):
                self._end_talk("skipped")
                return
            s["talk"]["messages"].append({"role": "user", "content": a})
            s["talk"]["pending"] = True
            return

        if phase == "duties":
            if not blank(a):
                self.role().fields["Responsibilities"] = a
                self._save_record()
            else:
                self._remember_asked(role_key(self.role()), "Responsibilities")
            self._next_role()
            return

        if phase == "lens":
            if gave_up(a):
                self._finish_role()
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

            if phase == "profile":
                prompt = self._fact_prompt(PROFILE_QUESTIONS)
                if prompt is not None:
                    return prompt
                self._start_roles()
                continue

            if phase == "story":
                prompt = self._fact_prompt(STORY_QUESTIONS)
                if prompt is not None:
                    return prompt
                s["phase"] = "done"
                continue

            if phase == "roles":
                stage = s["new_role"]["stage"]
                emp = s["new_role"].get("employer", "")
                if stage == "employer" and s["new_role"].get("adding"):
                    return Prompt("A new job, or one missing from that list? Its employer.",
                                  hint="Enter to carry on")
                return {"employer": Prompt("Employer (or organisation)?", hint="Enter when done"),
                        "title": Prompt(f"Your job title at {emp}? One title; if you had "
                                        f"more than one there, the others come next."),
                        "other": Prompt(f"Any other title at {emp}, before or after "
                                        f"{s['new_role'].get('title', '')}? e.g. before a "
                                        f"promotion. Its title.", hint="Enter if none"),
                        "other_dates": Prompt(f"Dates as {s['new_role'].get('other', '')}?"),
                        "dates": Prompt("Dates? (anything readable, e.g. 2019 - 2022)")}[stage]

            if phase == BACKGROUND:
                prompt = self._background_prompt()
                if prompt is not None:
                    return prompt
                s["background_done"] = True
                s["phase"] = "done" if self.background_only else "story"
                continue

            if phase == "outside":
                return self._outside_prompt()

            if phase == "skills":
                prompt = self._skills_prompt()
                if prompt is not None:
                    return prompt
                continue

            if phase != "done" and s["qi"] >= len(s["queue"]):
                # jobs finished: work outside them, then education and the rest, once
                if self.only or s.get("background_done"):
                    s["phase"] = "done"
                elif not s.get("skills_done") and self._unchecked_skills():
                    s["phase"] = "skills"
                elif not s.get("outside_done"):
                    s["phase"] = "outside"
                else:
                    s["phase"] = BACKGROUND
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
                for other in siblings(self.rec, role):
                    for f in EMPLOYER_FIELDS:       # the same employer, not asked twice
                        if other.fields.get(f) and not role.fields.get(f):
                            role.fields[f] = other.fields[f]
                self._save_record()
                self.notes.append(f"{role.label()}: "
                                  f"{mr.coverage_note(role, self.target, prompt=True)}")
                s.update(phase="context", ctx_i=0, bullet_i=0, lens_used=False, talk=None)
                continue

            if phase == "disown":
                return Prompt(f"It sounds like {role.title} at {role.employer} was not your "
                              f"job. Remove it from your record?", kind="choice",
                              options=list(DISOWN_OPTIONS))

            if phase == "context":
                skipped = self._job_asked(role)
                while s["ctx_i"] < len(CONTEXT_QUESTIONS) and (
                        role.outside() or role.fields.get(CONTEXT_QUESTIONS[s["ctx_i"]][0])
                        or CONTEXT_QUESTIONS[s["ctx_i"]][0] in skipped
                        or (CONTEXT_QUESTIONS[s["ctx_i"]][0] == "Other titles"
                            and siblings(self.rec, role))):
                    s["ctx_i"] += 1
                if s["ctx_i"] < len(CONTEXT_QUESTIONS):
                    label, q = CONTEXT_QUESTIONS[s["ctx_i"]]
                    return Prompt(q.format(employer=role.employer, title=role.title),
                                  hint="Pick one, or Enter to skip" if label in OPTIONS
                                  else "Enter to skip",
                                  options=list(OPTIONS.get(label, ())))
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

            if phase == "duties":
                return Prompt(DUTIES_QUESTION, hint="Enter to skip")

            if phase == "lens":
                if s["lens_used"]:
                    self._finish_role()
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
               and not ASK_IF.get(SECTIONS[bg["section"]][0], lambda rec: True)(self.rec)):
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
        key, question, hint = questions[bg["q"]][:3]
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
        if not blank(a):
            bg["draft"][key] = a
        bg["q"] += 1
        while bg["q"] < len(questions) and not _wanted(questions[bg["q"]], bg["draft"]):
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
            if name in ("job_training", "education") and "Skills:" in line:
                from .skills import link_both_ways
                link_both_ways(self.rec)
                self._save_record()
        bg.update(q=0, draft={})

    # -- conversations with the model ----------------------------------------

    def _open_talk(self, bullet: str = "", seed=None) -> dict:
        role = self.role()
        ctx = {"employer": role.employer, "job_title": role.title,
               "dates": role.fields.get("Dates", ""),
               "role_context": {k: v for k, v in role.fields.items() if v},
               "already_recorded": [a.title for a in role.accomplishments],
               "not_remembered": self.state.get("not_remembered", {}).get(role_key(role), []),
               "explain_skills": self._explain_skills()}
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

    def _explain_skills(self) -> bool:
        """Explain what "skills" means the first time only: until one
        accomplishment has been recorded with this question asked."""
        return not (self.state.get("skills_explained")
                    or any(a.skills_used for _, a in self.rec.all_accomplishments()))

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
            for name in t["skills"]:
                draft.add_skill(name)
            s["skills_explained"] = True
            role.accomplishments.append(draft)
            if t["bullet"] and t["bullet"] in role.recorded_bullets:
                role.recorded_bullets.remove(t["bullet"])
            link_skills(self.rec, draft.skill_names(), draft.title)
            self._save_record()
            self.recorded += 1
            self.notes.append(f"recorded: {draft.title} ({draft.evidence})")
            self._end_talk("complete")
            return None

        return Prompt(t["say"] or "Go on?")

    def _end_talk(self, outcome: str) -> None:
        s = self.state
        title = ((s["talk"] or {}).get("draft") or {}).get("title", "")
        if outcome != "complete" and self._keep_partial():
            outcome = "complete"            # told, though the details ran out
        if outcome != "complete" and title and self.role() is not None:
            # not asked again in this job: once they can't recall it, asking
            # a second time only costs them a question
            gone = s.setdefault("not_remembered", {}).setdefault(role_key(self.role()), [])
            if title not in gone:
                gone.append(title)
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

    def _keep_partial(self) -> bool:
        """A conversation that ends before "complete" still told something:
        what they did, or what came of it. Keep that, as a qualitative record,
        rather than lose a story they took the time to tell."""
        t, role = self.state.get("talk") or {}, self.role()
        d = dict(t.get("draft") or {})
        if role is None or not d.get("title") or not (d.get("actions") or d.get("results")):
            return False
        if near_duplicate(role, d["title"], ignore=t.get("bullet", "")):
            return False
        if d.get("evidence") not in mr.EVIDENCE_TIERS:
            d["evidence"] = "qualitative"
        draft = Accomplishment(**{k: d.get(k, "") for k in DRAFT_KEYS})
        for name in t.get("skills", []):
            draft.add_skill(name)
        role.accomplishments.append(draft)
        if t.get("bullet") and t["bullet"] in role.recorded_bullets:
            role.recorded_bullets.remove(t["bullet"])
        link_skills(self.rec, draft.skill_names(), draft.title)
        self._save_record()
        self.recorded += 1
        self.notes.append(f"recorded: {draft.title} ({draft.evidence})")
        return True

    # -- skills from an old resume ---------------------------------------------

    def _unchecked_skills(self) -> list:
        """Skills an imported resume listed that no accomplishment has shown
        and the person hasn't been shown yet. Ones the interview confirmed
        through their work need no question."""
        shown = set((self.store.load_state(ASKED_STATE) or {}).get("skills_checked", []))
        return [k.name for k in self.rec.skills
                if k.have == sk.YES and k.source == sk.FROM_RESUME and not k.evidence
                and mr._squash(k.name) not in shown]

    def _mark_checked(self, names) -> None:
        st = self.store.load_state(ASKED_STATE) or {}
        st["skills_checked"] = sorted(set(st.get("skills_checked", []))
                                      | {mr._squash(n) for n in names})
        self.store.save_state(ASKED_STATE, st)

    def _skills_prompt(self) -> Prompt | None:
        s = self.state
        k = s.get("skills_check")
        if k is None:
            names = self._unchecked_skills()
            if not names:
                self._skills_finished()
                return None
            k = s["skills_check"] = {"names": names, "later": [], "i": 0, "listed": False}
        if not k["listed"]:
            return Prompt(SKILLS_CHECK, kind="multi", hint=SKILLS_HINT, options=list(k["names"]))
        if k["i"] < len(k["later"]):
            return Prompt(LAST_USED.format(name=k["later"][k["i"]]), hint=LAST_USED_HINT)
        self._skills_finished()
        return None

    def _take_skills(self, a: str) -> None:
        k = self.state["skills_check"]
        if not k["listed"]:
            drop = dropped(a, k["names"])
            for name in drop:
                sk.set_skill(self.rec, name, have=sk.NO)
            self._save_record()
            self._mark_checked(k["names"])
            if drop:
                self.notes.append("dropped: " + "; ".join(drop))
            kept = [n for n in k["names"] if n not in drop]
            k["later"] = [n for n in kept if not (self.rec.skill(n) and self.rec.skill(n).last_used)]
            k["listed"] = True
            return
        low = a.strip().lower().rstrip(".!")
        if low in STOP_ASKING:
            k["i"] = len(k["later"])
            return
        if a.strip() and not blank(a):
            sk.set_skill(self.rec, k["later"][k["i"]], last_used=a.strip())
            self._save_record()
        k["i"] += 1

    def _skills_finished(self) -> None:
        s = self.state
        s.pop("skills_check", None)
        s["skills_done"] = True
        s["phase"] = "begin_role"           # the end of the job queue routes on from here

    def _outside_prompt(self) -> Prompt:
        s = self.state
        o = s.setdefault("outside", {"stage": "what", "draft": {}})
        if not s.get("outside_intro"):
            s["outside_intro"] = True
            self.notes.append(OUTSIDE_INTRO)
        stage = o["stage"]
        if stage == "what" and s.get("outside_added"):
            stage = "more"
        question, hint = OUTSIDE_QUESTIONS[stage]
        return Prompt(question, hint=hint,
                      options=list(mr.OUTSIDE_TYPES) if stage == "kind" else [])

    def _take_outside(self, a: str) -> None:
        s = self.state
        o = s.setdefault("outside", {"stage": "what", "draft": {}})
        stage = o["stage"]
        if stage == "what":
            if gave_up(a):
                s["outside_done"] = True
                s.pop("outside", None)
                s["phase"] = BACKGROUND
                return
            o["draft"]["what"] = a
            o["stage"] = "where"
            return
        if stage == "where":
            o["draft"]["where"] = "" if blank(a) else a
            o["stage"] = "kind"
            return
        if stage == "kind":
            kind = pick(a, mr.OUTSIDE_TYPES) if not blank(a) else ""
            # it has to read as outside a job, or it would join the job history
            o["draft"]["kind"] = kind if kind in mr.OUTSIDE_TYPES else "Personal or side project"
            o["stage"] = "dates"
            return
        d = o["draft"]
        role = Role(employer=d.get("where") or "Personal", title=d["what"])
        role.fields["Employment type"] = d["kind"]
        if not blank(a):
            role.fields["Dates"] = a
        s["outside"] = {"stage": "what", "draft": {}}
        have = same_job(self.rec, role.employer, role.title)
        if have:
            self.notes.append(f"{have.label()} is already on your record.")
            return
        self.rec.roles.append(role)
        self._save_record()
        s["outside_added"] = s.get("outside_added", 0) + 1
        # straight into its accomplishments, then back here for the next one
        s["queue"].append(role_key(role))
        s["qi"] = len(s["queue"]) - 1
        s["phase"] = "begin_role"

    def _finish_role(self) -> None:
        """Duties for a thin job, then the next job."""
        role = self.role()
        if (role is not None and not role.outside() and not role.fields.get("Responsibilities")
                and "Responsibilities" not in self._job_asked(role)
                and role.coverage()["total"] < THIN_ROLE):
            self.state["phase"] = "duties"
        else:
            self._next_role()

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
        text = prompt.text + "".join(f"\n    {i}. {o}" for i, o in enumerate(prompt.options, 1))
        text += f"\n  ({prompt.hint})" if prompt.hint else ""
        prompt = interview.step(ask(text))
