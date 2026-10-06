"""
Read and write the master record -- record.md, the one file a person keeps.

One definition of the format, because several things need it: the importers
write it, the interview reads and extends it, and tailoring selects from it. Two implementations of a file format drift, and the
drift shows up as content quietly going missing.

The format is Markdown on purpose. It has to be editable in any editor by
someone who has never run the interview, and it has to diff well, because it is
the one file in this project that accumulates for years.

    ## Roles

    ### Northwind Retail — Head of Analytics

    - **Dates:** 2021 - Present
    - **Authority:** 9 analysts across two teams

    #### Paid search efficiency programme

    - **Problem:** Acquisition cost had risen 40% over two years.
    - **Actions:** Built geo-holdout tests across the ten largest markets.
    - **Results:** Cut acquisition cost 23% in two quarters.
    - **Evidence:** metric

What round-trips: roles, their fields in the order the format defines, their
notes, every accomplishment, and the recorded-bullet lists. What does not: the
exact prose of a section this module generates, and hand-written text in places
the format has no field for -- that lands in `trailing` and is written back
verbatim at the end. Everything before "## Contact" is kept verbatim too, so an
importer's provenance header survives being re-rendered.

`render(parse(text))` is idempotent: parsing what it produced and rendering
again gives the same bytes. It is deliberately not byte-identical to arbitrary
hand-edited input -- that would mean preserving every whitespace choice a person
made, and the round-trip test asserts the property worth having instead.
"""
import re
from dataclasses import dataclass, field

# The evidence ladder, strongest first. The interview walks down it and stops at
# the first rung that lands; the build step prefers the top of it when a posting
# rewards numbers. "qualitative" is a real answer, not a failure -- plenty of
# good work changes no number anyone measured.
EVIDENCE_TIERS = ("metric", "derived", "scope", "qualitative")
EVIDENCE_HELP = {
    "metric": "a number you already knew",
    "derived": "a number worked out from before-and-after",
    "scope": "the size of the thing, not its outcome",
    "qualitative": "a contribution with no number attached",
}

# Role fields, in the order they render. Only `employer` and `title` live in the
# heading; everything else is a labelled line underneath.
ROLE_FIELDS = ("Dates", "Employment type", "Other titles", "Location", "Company", "Challenge", "Authority",
               "Territory", "Budget", "Reported to", "Markets",
               "Responsibilities", "Results against targets", "Recognition", "Note on accomplishments")
# Employment types, offered as choices. The second list is work outside a paid
# job: it is kept with the jobs so it gets the same coached interview, but it
# is never a job on the resume, never a gap in employment, and never a role
# that "needs more accomplishments".
PAID_TYPES = ("Full-time", "Part-time", "Contract", "Freelance or self-employed",
              "Temporary or seasonal", "Internship or apprenticeship", "Military")
OUTSIDE_TYPES = ("Volunteer", "Board or committee", "Community or faith group",
                 "Personal or side project", "Study or coursework", "Caregiving or family")

ACC_FIELDS = ("Problem", "Actions", "Contribution", "Results", "Skills used", "Evidence",
              "Bullet")
# Fields filled only when they apply, so a blank one is never a gap to fill.
ACC_OPTIONAL = ("Evidence", "Bullet", "Contribution", "Skills used")


def acc_attr(label: str) -> str:
    """'Skills used' -> 'skills_used'."""
    return label.lower().replace(" ", "_")
# Words that mark a result as a team's. Most work is shared, and "we cut costs
# 23%" filed as one person's result is exactly the claim an interviewer probes.
TEAM_WORDS = re.compile(r"\b(we|our|us|the team|my team|our team|together|jointly|co-led)\b", re.I)

BULLETS_HEADING = "Recorded resume bullets"
BULLETS_PREAMBLE = [
    "From a finished resume, so each one is already compressed: the problem it",
    "solved and the actions taken are missing. Expanding one into its own",
    "`####` block is optional -- they are usable as they stand.",
]

ROLE_HEADING = re.compile(r"^###\s+(?P<employer>.+?)\s+(?:—|--|-)\s+(?P<title>.+?)\s*$")
ACC_HEADING = re.compile(r"^####\s+(?P<title>.+?)\s*$")
LABELLED = re.compile(r"^-\s+\*\*(?P<label>[^:*]+):\*\*\s*(?P<value>.*)$")
PLAIN_BULLET = re.compile(r"^-\s+(?!\*\*)(?P<text>.+)$")
SECTION = re.compile(r"^##\s+(?P<name>.+?)\s*$")
SUBSECTION = re.compile(r"^###\s+(?P<name>.+?)\s*$")


@dataclass
class Accomplishment:
    title: str = ""
    problem: str = ""
    actions: str = ""
    # Their own part of a shared result, in their words. Empty when the work
    # was theirs alone.
    contribution: str = ""
    results: str = ""
    # The skills, tools, methods and know-how this took, "; "-separated as the
    # record shows them. The skills list's evidence is built from these, so a
    # skill always points back to the work that proves it.
    skills_used: str = ""
    evidence: str = ""
    # The resume line compiled from the three fields above, kept here rather
    # than in a hidden cache so it is visible, diffable and editable: a bullet
    # you rewrote by hand is better than one a model drafted, and this is where
    # you rewrite it.
    bullet: str = ""

    def source_text(self) -> str:
        """What a compiled bullet has to be grounded in."""
        return " ".join(x for x in (self.problem, self.actions, self.contribution,
                                    self.results) if x)

    def skill_names(self) -> list:
        return [s.strip() for s in re.split(r";", self.skills_used or "") if s.strip()]

    def add_skill(self, name: str) -> bool:
        """Add a skill if it isn't listed already, compared ignoring case."""
        name = (name or "").strip()
        if not name or _squash(name) in {_squash(n) for n in self.skill_names()}:
            return False
        self.skills_used = "; ".join([*self.skill_names(), name])
        return True

    def team_unclear(self) -> bool:
        """A result told as a team's, with nothing saying what was theirs."""
        return (not self.contribution.strip()
                and bool(TEAM_WORDS.search(f"{self.actions} {self.results}")))

    def is_empty(self) -> bool:
        return not any((self.title, self.problem, self.actions, self.results))

    def has_number(self) -> bool:
        """Whether the result carries a figure. Used for coverage, not for
        grading -- a count here is a prompt to the user, never a judgement."""
        return bool(re.search(r"\d", self.results or ""))


@dataclass
class Role:
    employer: str = ""
    title: str = ""
    fields: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    accomplishments: list = field(default_factory=list)
    recorded_bullets: list = field(default_factory=list)

    def outside(self) -> bool:
        """Work outside a paid job: volunteering, a project, study."""
        return self.fields.get("Employment type", "") in OUTSIDE_TYPES

    def label(self) -> str:
        return f"{self.employer or '[employer]'} — {self.title or '[title]'}"

    def coverage(self) -> dict:
        """
        What this role has, so the interview can show it rather than nag.

        A count the user can see does the work that "any more accomplishments?"
        asked ten times does badly.
        """
        accs = [a for a in self.accomplishments if not a.is_empty()]
        quantified = [a for a in accs
                      if a.evidence in ("metric", "derived") or a.has_number()]
        return {"accomplishments": len(accs),
                "quantified": len(quantified),
                "bullets": len(self.recorded_bullets),
                "total": len(accs) + len(self.recorded_bullets)}


@dataclass
class Skill:
    """
    A skill, whether the person has it, and what proves it.

    `have` is "yes", "no" (a skill their field asks for that they lack, kept so
    nobody suggests it again and the pipeline can see the gap) or "verify" (on
    the list but not yet answered: a suggestion, or a standard skill for their
    field). Only "yes" ever reaches a resume.

    `evidence` names accomplishments by title. A skill with no evidence is
    still a skill, but tailoring treats it as a claim rather than a fact: it
    may appear in a skills list, never as the subject of a bullet.
    """
    name: str
    category: str = ""
    level: str = ""
    evidence: list = field(default_factory=list)
    have: str = "yes"
    source: str = ""            # resume, interview, suggested, your field, you
    # How long and how recently. Postings ask for "5+ years of X", and a skill
    # last used in 2009 should not be tailored as current. A leading "~" marks
    # an estimate worked out from the dates of the roles in `evidence`; a value
    # the person typed has none.
    years: str = ""
    last_used: str = ""

    def render(self) -> str:
        line = f"- {self.name}"
        if self.level:
            line += f" ({self.level})"
        if self.source:
            line += f" — from: {self.source}"
        if self.years:
            line += f" — years: {self.years}"
        if self.last_used:
            line += f" — last used: {self.last_used}"
        if self.evidence:
            line += " — evidence: " + "; ".join(self.evidence)
        return line


SKILL_LINE = re.compile(r"^-\s+(?P<name>.+?)(?:\s+\((?P<level>Expert|Advanced|Working|Familiar)\))?"
                        r"(?:\s+(?:—|--)\s+from:\s*(?P<src>.+?))?"
                        r"(?:\s+(?:—|--)\s+years:\s*(?P<years>.+?))?"
                        r"(?:\s+(?:—|--)\s+last used:\s*(?P<last>.+?))?"
                        r"(?:\s+(?:—|--)\s+evidence:\s*(?P<ev>.+))?\s*$")
# Only these count as a level, so "SQL (BigQuery, Snowflake)" stays one skill
# name instead of becoming SQL at level "BigQuery, Snowflake".
LEVELS = ("Expert", "Advanced", "Working", "Familiar")
# The three skills sections, by whether the person has the skill.
SKILL_SECTIONS = {"yes": "Skills", "verify": "Skills to check", "no": "Skills I don't have yet"}
# Optional sections: (key, heading, other headings a resume or person uses).
# Each holds one line per entry. A section nobody fills is never written.
EXTRA_SECTIONS = [
    # A credential, like a licence: in defence, federal and contractor work it
    # goes on the resume and often decides the screen. Work authorization,
    # relocation and the like are deliberately absent; application forms ask.
    ("clearance", "Security clearance", ("security clearances", "clearance", "clearances",
                                         "clearance level", "security clearance level")),
    ("projects", "Projects", ("portfolio", "selected projects", "personal projects",
                              "portfolio projects", "key projects")),
    ("languages", "Languages", ("language skills", "spoken languages")),
    ("volunteer", "Volunteer work", ("volunteer", "volunteer experience", "volunteering",
                                     "community involvement", "community service")),
    ("awards", "Awards", ("awards and honors", "awards and honours", "honors", "honours",
                          "honors and awards", "honours and awards", "recognition")),
    ("publications", "Publications and speaking", ("publications", "speaking", "presentations",
                                                   "talks", "patents", "conference talks")),
    ("memberships", "Memberships", ("professional memberships", "affiliations",
                                    "professional affiliations", "associations", "boards",
                                    "board service")),
    ("training", "Training and courses", ("training", "courses", "professional development",
                                          "continuing education", "coursework")),
    ("testimonials", "Testimonials", ("recommendations", "references and testimonials",
                                      "endorsements")),
    ("career_breaks", "Career breaks", ("career break", "employment gaps", "gaps")),
]
EXTRA_KEYS = [k for k, _, _ in EXTRA_SECTIONS]
# A bachelor's degree or higher, as people write one. Abbreviations are
# matched in capitals only, so "MS Office" in a course name is not a master's.
DEGREE = re.compile(
    r"(?i:\b(bachelor|master|doctor(ate)?|ph\.?d|graduate degree)\b)|"
    r"\b(BA|BS|BSc|BSN|BBA|BEng|BFA|BEd|BArch|AB|SB|MA|MS(?!\s+(?:Office|Word|Excel|Teams|Access|Project|Dynamics|SQL|Visio))|MSc|MSN|MBA|MEng|MFA|MEd|"
    r"MPH|MPA|MSW|MPP|MAcc|LLM|PhD|EdD|DBA|JD|MD|DO|DNP|PharmD|DDS|DMD|DVM|PsyD)\b|"
    r"\b(B\.A|B\.S|M\.A|M\.S|M\.B\.A|J\.D|M\.D)\.")


def has_degree(rec) -> bool:
    """Whether the record shows a bachelor's degree or higher."""
    return any(DEGREE.search(e or "") for e in rec.education)


def training_skills(line: str) -> tuple:
    """('Lean program', ['Lean', 'Root cause analysis']) from a training line
    that ends 'Skills: Lean; Root cause analysis'. ('', []) if it names none."""
    head, sep, tail = (line or "").partition("Skills:")
    if not sep:
        return "", []
    name = re.split(r"\s+\(|\.\s*$", head.strip())[0].strip().rstrip(".")
    return name, [s.strip().rstrip(".") for s in tail.split(";") if s.strip()]


TARGET_FIELDS = ("Titles", "Industries", "Locations", "Seniority", "Notes")


@dataclass
class Record:
    header: list = field(default_factory=list)
    contact: dict = field(default_factory=dict)
    competencies: list = field(default_factory=list)
    sets_apart: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    skills: list = field(default_factory=list)
    education: list = field(default_factory=list)
    certifications: list = field(default_factory=list)
    target: dict = field(default_factory=dict)
    summary: str = ""
    extras: dict = field(default_factory=dict)      # EXTRA_SECTIONS key -> lines
    trailing: list = field(default_factory=list)

    def all_accomplishments(self):
        """(role, accomplishment) for every structured accomplishment."""
        for role in self.roles:
            for acc in role.accomplishments:
                yield role, acc

    def skill(self, name: str):
        key = _squash(name)
        for s in self.skills:
            if _squash(s.name) == key:
                return s
        return None

    def role_by_employer(self, employer: str, title: str = None, strict: bool = False):
        """
        The role at this employer. Someone promoted inside one employer has a
        role per title, so a title, when given, picks between them. Without an
        exact title match, the employer's only role is still returned (a title
        edited in one file and not the other is the same job), unless `strict`
        or there is more than one role to choose from.
        """
        key = _squash(employer)
        matches = [r for r in self.roles if _squash(r.employer) == key]
        if title is None:
            return matches[0] if matches else None
        for role in matches:
            if _squash(role.title) == _squash(title):
                return role
        return matches[0] if len(matches) == 1 and not strict else None


def _squash(name: str) -> str:
    """Compare employer names ignoring punctuation and case. Two sources spell
    the same company differently -- "Beck Rowe" against "Beck & Rowe" -- and an
    exact match treats one of them as a role nobody has recorded."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


SKILL_HAVE = {_squash(h): state for state, h in SKILL_SECTIONS.items()}
_EXTRA_NAMES = {_squash(n): key for key, heading, others in EXTRA_SECTIONS
                for n in (heading, *others)}


def extra_key(heading: str) -> str:
    """The optional section a heading means ("Honors & Awards" is awards), or ""."""
    return _EXTRA_NAMES.get(_squash(heading.replace("&", " and ")), "")


def _extend_attr(obj, name):
    return lambda more: setattr(obj, name, f"{getattr(obj, name)} {more}".strip())


def _extend_key(d, key):
    return lambda more: d.__setitem__(key, f"{d.get(key, '')} {more}".strip())


def _extend_last(items):
    def extend(more):
        items[-1] = f"{items[-1]} {more}".strip()
    return extend


# An indented line that is not itself a list item continues the value above
# it. Editors wrap long lines this way, and so do people; dropping the second
# half of a sentence without a word is the worst thing a record can do.
CONTINUATION = re.compile(r"^[ \t]+(?![-*+]\s)\S")


def parse(text: str) -> Record:
    lines = (text or "").split("\n")
    rec = Record()
    where = "header"
    sub = None
    role = None
    acc = None
    in_bullets = False
    have = "yes"            # which skills section we are in
    extend = None           # appends a continuation line to the last value read

    def close_acc():
        nonlocal acc
        if acc and not acc.is_empty():
            role.accomplishments.append(acc)
        acc = None

    def close_role():
        nonlocal role
        close_acc()
        if role is not None:
            rec.roles.append(role)
        role = None

    for raw in lines:
        line = raw.rstrip()

        if extend and CONTINUATION.match(line):
            extend(line.strip())
            continue
        extend = None

        m = SECTION.match(line)
        if m and not line.startswith("###"):
            name = m.group("name").strip().lower()
            if where == "roles":
                close_role()
            in_bullets = False
            sub = None
            if name == "contact":
                where = "contact"
            elif name == "positioning":
                where = "positioning"
            elif name == "roles":
                where = "roles"
            elif name in ("summary", "professional summary", "profile", "summary of qualifications"):
                where = "summary"
            elif extra_key(name):
                where = "extra:" + extra_key(name)
                rec.extras.setdefault(extra_key(name), [])
            elif _squash(name) in SKILL_HAVE:
                where, have = "skills", SKILL_HAVE[_squash(name)]
            elif name in ("education", "certifications", "target"):
                where = name
            else:
                # any other section is kept verbatim and written back at the end
                where = "trailing"
                rec.trailing.append(line)
            continue

        if where == "skills":
            m = SUBSECTION.match(line)
            if m:
                sub = m.group("name").strip()
                continue
            m = SKILL_LINE.match(line)
            if m and not LABELLED.match(line):
                ev = [e.strip() for e in (m.group("ev") or "").split(";") if e.strip()]
                cat, state = sub or "", have
                if cat.lower().startswith("to verify"):      # the older layout
                    cat, state = cat.partition(":")[2].strip() or "Other", "verify"
                rec.skills.append(Skill(name=m.group("name").strip(), category=cat,
                                        level=(m.group("level") or "").strip(),
                                        evidence=ev, have=state,
                                        source=(m.group("src") or "").strip(),
                                        years=(m.group("years") or "").strip(),
                                        last_used=(m.group("last") or "").strip()))
            continue

        if where == "summary":
            if line.strip():
                rec.summary = f"{rec.summary} {line.strip()}".strip()
            continue

        if where.startswith("extra:"):
            m = PLAIN_BULLET.match(line)
            if m:
                items = rec.extras[where[6:]]
                items.append(m.group("text").strip())
                extend = _extend_last(items)
            continue

        if where in ("education", "certifications"):
            m = PLAIN_BULLET.match(line)
            if m:
                getattr(rec, where).append(m.group("text").strip())
                extend = _extend_last(getattr(rec, where))
            continue

        if where == "target":
            m = LABELLED.match(line)
            if m:
                rec.target[m.group("label").strip()] = _blank_to_empty(
                    m.group("value").strip())
                extend = _extend_key(rec.target, m.group("label").strip())
            continue

        if where == "trailing":
            rec.trailing.append(line)
            continue

        if where == "header":
            rec.header.append(line)
            continue

        if where == "contact":
            m = LABELLED.match(line)
            if m:
                rec.contact[m.group("label").strip()] = m.group("value").strip()
                extend = _extend_key(rec.contact, m.group("label").strip())
            continue

        if where == "positioning":
            m = SUBSECTION.match(line)
            if m:
                name = m.group("name").strip().lower()
                sub = "competencies" if "competenc" in name else "sets_apart"
                continue
            if sub == "competencies":
                m = PLAIN_BULLET.match(line)
                if m:
                    rec.competencies.append(m.group("text").strip())
                    extend = _extend_last(rec.competencies)
            elif sub == "sets_apart" and line.strip():
                rec.sets_apart.append(line.strip())
            continue

        # --- roles -------------------------------------------------------
        m = ROLE_HEADING.match(line)
        if m:
            close_role()
            in_bullets = False
            role = Role(employer=m.group("employer").strip(),
                        title=m.group("title").strip())
            continue
        if role is None:
            continue

        m = ACC_HEADING.match(line)
        if m:
            close_acc()
            heading = m.group("title").strip()
            if heading.lower() == BULLETS_HEADING.lower():
                in_bullets = True
            else:
                in_bullets = False
                acc = Accomplishment(title=heading)
            continue

        m = LABELLED.match(line)
        if m:
            label, value = m.group("label").strip(), m.group("value").strip()
            if acc is not None and label in ACC_FIELDS:
                setattr(acc, acc_attr(label), _blank_to_empty(value))
                extend = _extend_attr(acc, acc_attr(label))
            else:
                role.fields[label] = _blank_to_empty(value)
                extend = _extend_key(role.fields, label)
            continue

        if line.startswith(">"):
            role.notes.append(line.lstrip("> ").rstrip())
            continue

        m = PLAIN_BULLET.match(line)
        if m and in_bullets:
            role.recorded_bullets.append(m.group("text").strip())
            extend = _extend_last(role.recorded_bullets)
            continue

    if where == "roles":
        close_role()
    return rec


def _blank_to_empty(value: str) -> str:
    """A [FILL IN] marker means the field is unanswered. Treating it as content
    would have the interview skip exactly the gaps it exists to close."""
    return "" if re.match(r"^\[FILL IN", value, re.I) else value


def render(rec: Record) -> str:
    out = list(rec.header)
    while out and not out[-1].strip():
        out.pop()

    if rec.contact:
        out += ["", "## Contact", ""]
        out += [f"- **{k}:** {v}" for k, v in rec.contact.items()]

    if rec.summary:
        out += ["", "## Summary", "", rec.summary]

    if rec.competencies or rec.sets_apart:
        out += ["", "## Positioning"]
        if rec.competencies:
            out += ["", "### Core competencies", ""]
            out += [f"- {c}" for c in rec.competencies]
        if rec.sets_apart:
            out += ["", "### What sets me apart", ""] + list(rec.sets_apart)

    out += ["", "## Roles"]
    for role in rec.roles:
        out += ["", f"### {role.employer} — {role.title}", ""]
        for label in ROLE_FIELDS:
            if role.fields.get(label):
                out.append(f"- **{label}:** {role.fields[label]}")
        for label, value in role.fields.items():
            if label not in ROLE_FIELDS and value:
                out.append(f"- **{label}:** {value}")
        if role.notes:
            out.append("")
            out += [f"> {n}" for n in role.notes]
        for acc in role.accomplishments:
            out += ["", f"#### {acc.title or '[FILL IN: short title]'}", ""]
            for label in ACC_FIELDS:
                value = getattr(acc, acc_attr(label), "")
                # Evidence is set by the interview and Bullet by the build
                # step; neither is a gap for a person to fill in by hand, so
                # neither gets a [FILL IN] marker inviting them to.
                # Contribution only applies to shared work, and Skills used
                # is filled by the interview and the skills list.
                if label in ACC_OPTIONAL and not value:
                    continue
                out.append(f"- **{label}:** {value or '[FILL IN]'}")
        if role.recorded_bullets:
            out += ["", f"#### {BULLETS_HEADING}", ""] + BULLETS_PREAMBLE + [""]
            out += [f"- {b}" for b in role.recorded_bullets]

    for state, heading in SKILL_SECTIONS.items():
        group = [x for x in rec.skills if x.have == state]
        if not group:
            continue
        out += ["", f"## {heading}"]
        cats = []
        for x in group:
            if x.category not in cats:
                cats.append(x.category)
        for cat in cats:
            out.append("")
            if cat:
                out += [f"### {cat}", ""]
            out += [x.render() for x in group if x.category == cat]

    for name in ("education", "certifications"):
        items = getattr(rec, name)
        if items:
            out += ["", f"## {name.capitalize()}", ""] + [f"- {i}" for i in items]

    for key, heading, _ in EXTRA_SECTIONS:
        items = rec.extras.get(key)
        if items:
            out += ["", f"## {heading}", ""] + [f"- {i}" for i in items]

    if rec.target:
        out += ["", "## Target", ""]
        for label in TARGET_FIELDS:
            if rec.target.get(label):
                out.append(f"- **{label}:** {rec.target[label]}")
        for label, value in rec.target.items():
            if label not in TARGET_FIELDS and value:
                out.append(f"- **{label}:** {value}")

    if rec.trailing:
        tail = list(rec.trailing)
        while tail and not tail[0].strip():
            tail.pop(0)
        out += ["", *tail]

    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def coverage_note(role: Role, target: int = 10, prompt: bool = False) -> str:
    """
    One line on where this role stands. Shown instead of asking "any more?",
    which is the question that makes an interview tiring.

    `prompt` adds the encouragement, and it defaults off for a reason: in a list
    of six roles the same sentence six times reads as nagging, which is the
    failure this whole approach is meant to avoid. The caller turns it on for
    the one role it is about to work on, and nowhere else.

    `target` is a prompt, not a rule. Ten is a number most people can reach for
    a role of a few years once someone helps them look.
    """
    c = role.coverage()
    if not c["total"]:
        return "nothing recorded yet" + (
            " — a good role usually has plenty once you start looking" if prompt else "")
    parts = [f"{c['total']} recorded"]
    if c["bullets"]:
        parts.append(f"{c['bullets']} from a resume only")
    parts.append(f"{c['quantified']} with a number")
    note = ", ".join(parts)
    if prompt and c["total"] < target:
        note += f" — most people reach about {target} for a role once they look"
    return note
