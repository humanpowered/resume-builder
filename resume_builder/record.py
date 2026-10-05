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
ROLE_FIELDS = ("Dates", "Location", "Company", "Challenge", "Authority",
               "Territory", "Budget", "Reported to", "Markets",
               "Responsibilities", "Recognition", "Note on accomplishments")
ACC_FIELDS = ("Problem", "Actions", "Results", "Evidence", "Bullet")

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
    results: str = ""
    evidence: str = ""
    # The resume line compiled from the three fields above, kept here rather
    # than in a hidden cache so it is visible, diffable and editable: a bullet
    # you rewrote by hand is better than one a model drafted, and this is where
    # you rewrite it.
    bullet: str = ""

    def source_text(self) -> str:
        """What a compiled bullet has to be grounded in."""
        return " ".join(x for x in (self.problem, self.actions, self.results) if x)

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
    A skill and what proves it.

    `evidence` names accomplishments by title. A skill with no evidence is
    still a skill, but tailoring treats it as a claim rather than a fact: it
    may appear in a skills list, never as the subject of a bullet.
    """
    name: str
    category: str = ""
    level: str = ""
    evidence: list = field(default_factory=list)

    def render(self) -> str:
        line = f"- {self.name}"
        if self.level:
            line += f" ({self.level})"
        if self.evidence:
            line += " — evidence: " + "; ".join(self.evidence)
        return line


SKILL_LINE = re.compile(r"^-\s+(?P<name>.+?)(?:\s+\((?P<level>Expert|Advanced|Working|Familiar)\))?"
                        r"(?:\s+(?:—|--)\s+evidence:\s*(?P<ev>.+))?\s*$")
# Only these count as a level, so "SQL (BigQuery, Snowflake)" stays one skill
# name instead of becoming SQL at level "BigQuery, Snowflake".
LEVELS = ("Expert", "Advanced", "Working", "Familiar")
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

    def role_by_employer(self, employer: str):
        key = _squash(employer)
        for role in self.roles:
            if _squash(role.employer) == key:
                return role
        return None


def _squash(name: str) -> str:
    """Compare employer names ignoring punctuation and case. Two sources spell
    the same company differently -- "Beck Rowe" against "Beck & Rowe" -- and an
    exact match treats one of them as a role nobody has recorded."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


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
            elif name in ("skills", "education", "certifications", "target"):
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
                rec.skills.append(Skill(name=m.group("name").strip(),
                                        category=sub or "",
                                        level=(m.group("level") or "").strip(),
                                        evidence=ev))
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
                setattr(acc, label.lower(), _blank_to_empty(value))
                extend = _extend_attr(acc, label.lower())
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
                value = getattr(acc, label.lower(), "")
                # Evidence is set by the interview and Bullet by the build
                # step; neither is a gap for a person to fill in by hand, so
                # neither gets a [FILL IN] marker inviting them to.
                if label in ("Evidence", "Bullet") and not value:
                    continue
                out.append(f"- **{label}:** {value or '[FILL IN]'}")
        if role.recorded_bullets:
            out += ["", f"#### {BULLETS_HEADING}", ""] + BULLETS_PREAMBLE + [""]
            out += [f"- {b}" for b in role.recorded_bullets]

    if rec.skills:
        out += ["", "## Skills"]
        cats = []
        for s in rec.skills:
            if s.category not in cats:
                cats.append(s.category)
        for cat in cats:
            out.append("")
            if cat:
                out += [f"### {cat}", ""]
            out += [s.render() for s in rec.skills if s.category == cat]

    for name in ("education", "certifications"):
        items = getattr(rec, name)
        if items:
            out += ["", f"## {name.capitalize()}", ""] + [f"- {i}" for i in items]

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
