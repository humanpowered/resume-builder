"""
How complete and how sound the master record is, and where an hour of the
person's time is worth most.

No model calls. Everything here is counted or parsed, so it is free to run
after every session and it never disagrees with itself.

The checks follow what the pipeline needs downstream: enough accomplishments
per role to choose from, a figure behind each one where one exists, skills
tied to evidence, and facts clean enough to print (dates that parse, no
leftover notes or blanks).
"""
import re
from dataclasses import dataclass, field

from . import record as mr

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
PRESENT = re.compile(r"present|current|now|today", re.I)


def parse_point(text: str, end: bool = False):
    """
    One end of a date range as (year, month), or None.

    Accepts the forms people actually write: "Jan 2019", "January 2019",
    "01/2019", "1/2019", "2019-01", "2019". A bare year is read as January
    for a start and December for an end, which never invents a gap.
    """
    t = (text or "").strip().lower().rstrip(".")
    if not t:
        return None
    m = re.search(r"\b(\d{1,2})\s*/\s*(\d{4})\b", t)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.search(r"\b(\d{4})\s*-\s*(\d{1,2})\b", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b([a-z]{3})[a-z]*\.?\s+(\d{4})\b", t)
    if m and m.group(1) in MONTHS:
        return int(m.group(2)), MONTHS[m.group(1)]
    m = re.search(r"\b(\d{4})\b", t)
    if m:
        return int(m.group(1)), 12 if end else 1
    return None


def parse_range(text: str, today=(2026, 10)):
    """'Jan 2019 - Present' -> ((2019, 1), today). None if either end fails."""
    text = re.sub(r"<!--.*?-->", "", text or "")
    parts = re.split(r"\s+(?:-|–|—|to)\s+|\s*[–—]\s*|(?<=\d)\s*-\s*(?=[A-Za-z0-9])", text, maxsplit=1)
    if len(parts) != 2:
        return None
    start = parse_point(parts[0])
    end = today if PRESENT.search(parts[1]) else parse_point(parts[1], end=True)
    if not start or not end:
        return None
    return start, end


def months_between(a, b) -> int:
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


@dataclass
class RoleHealth:
    label: str
    accomplishments: int
    quantified: int
    no_result: list = field(default_factory=list)
    resume_only: int = 0
    context_missing: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    outside: bool = False           # work outside a paid job


@dataclass
class Report:
    roles: list = field(default_factory=list)
    record_issues: list = field(default_factory=list)
    skills_total: int = 0
    skills_without_evidence: list = field(default_factory=list)
    skills_unconfirmed: int = 0
    skills_without_level: int = 0
    skills_list_built: bool = False
    # Promotions inside one employer, with how long each took. A quick step
    # up is one of the few signals a manager trusts in place of a degree.
    progression: list = field(default_factory=list)
    has_degree: bool = True
    next_steps: list = field(default_factory=list)

    @property
    def total(self):
        return sum(r.accomplishments + r.resume_only for r in self.roles)

    @property
    def quantified(self):
        return sum(r.quantified for r in self.roles)


CONTEXT_FIELDS = ("Dates", "Company", "Challenge", "Authority", "Reported to")
LEFTOVER = re.compile(r"\[FILL IN|<!--", re.I)


def check(rec: mr.Record, target: int = 10, today=(2026, 10)) -> Report:
    rep = Report()

    spans = []
    for role in rec.roles:
        accs = [a for a in role.accomplishments if not a.is_empty()]
        h = RoleHealth(
            label=role.label(),
            accomplishments=len(accs),
            quantified=sum(1 for a in accs if a.has_number()
                           or a.evidence in ("metric", "derived")),
            no_result=[a.title for a in accs if not a.results.strip()],
            resume_only=len(role.recorded_bullets),
            context_missing=[] if role.outside() else
            [f for f in CONTEXT_FIELDS if not role.fields.get(f)],
            outside=role.outside(),
        )
        for label, value in role.fields.items():
            if LEFTOVER.search(value or ""):
                h.issues.append(f"{label} has an unsettled note or blank: {value[:70]}")
        for a in accs:
            for name in ("problem", "actions", "results"):
                if LEFTOVER.search(getattr(a, name) or ""):
                    h.issues.append(f"'{a.title}' {name} has an unsettled note or blank")
        if (re.search(r"promot", role.fields.get("Recognition", ""), re.I)
                and not role.outside()
                and sum(1 for r in rec.roles if mr._squash(r.employer)
                        == mr._squash(role.employer)) == 1):
            h.issues.append("mentions a promotion but has one title; add the earlier "
                            "title as its own job with its dates, so the promotion shows")
        for a in accs:
            if a.team_unclear():
                h.issues.append(f"'{a.title}' reads as a team result; what was your part?")
        if role.employer.startswith("[FILL IN") or role.title.startswith("[FILL IN"):
            h.issues.append("employer or title is blank")
        dates = role.fields.get("Dates", "")
        if dates:
            rng = parse_range(dates, today)
            if rng is None:
                h.issues.append(f"dates do not parse: {dates!r}")
            elif months_between(*rng) < 0:
                h.issues.append(f"dates run backwards: {dates!r}")
            elif not role.outside():        # a project doesn't fill a gap in employment
                spans.append((rng, role.label()))
        rep.roles.append(h)

    # Gaps and overlaps across the career, newest first. Overlap is common and
    # legitimate (a side role, a consulting client); it is listed so a person
    # can confirm it, not flagged as wrong. A gap over six months is the thing
    # a recruiter asks about, so it is worth an answer ready in the record.
    # Walk forward from the oldest start, measuring each start against the
    # latest end so far. Comparing neighbours instead reports a gap after a
    # short side job that a longer role was covering all along.
    spans.sort(key=lambda s: s[0][0])
    covered, c_label = None, ""
    for (start, end), label in spans:
        if covered is not None:
            gap = months_between(covered, start)
            if gap > 6:
                rep.record_issues.append(
                    f"{gap}-month gap between {c_label} and {label}")
        if covered is None or months_between(covered, end) > 0:
            covered, c_label = end, label

    rep.progression = progression(rec, today)
    rep.has_degree = mr.has_degree(rec)

    for key in ("Name", "Email"):
        if not rec.contact.get(key):
            rep.record_issues.append(f"contact {key.lower()} is missing")
    if not rec.education:
        rep.record_issues.append("no education recorded (add it even if it is a diploma or training)")

    from .skills import is_pending
    rep.skills_total = len(rec.skills)
    rep.skills_unconfirmed = sum(1 for s in rec.skills if is_pending(s))
    rep.skills_without_evidence = [s.name for s in rec.skills
                                   if not s.evidence and s.have == "yes"]
    rep.skills_without_level = sum(1 for s in rec.skills if s.have == "yes" and not s.level)
    rep.skills_list_built = any(s.source in ("your field", "your record") for s in rec.skills)

    rep.next_steps = next_steps(rec, rep, target)
    return rep


def progression(rec: mr.Record, today=(2026, 10)) -> list:
    """'Mercy Hospital: Staff RN to Charge Nurse in 18 months', for each step
    between titles at one employer, oldest first. Undated roles are skipped."""
    by_employer = {}
    for role in rec.roles:
        rng = parse_range(role.fields.get("Dates", ""), today=today)
        if rng and not role.outside():
            by_employer.setdefault(mr._squash(role.employer), []).append((rng[0], role))
    out = []
    for steps in by_employer.values():
        steps.sort(key=lambda s: s[0])
        for (start, lower), (next_start, higher) in zip(steps, steps[1:]):
            months = months_between(start, next_start)
            if months > 0 and mr._squash(lower.title) != mr._squash(higher.title):
                out.append(f"{higher.employer}: {lower.title} to {higher.title} "
                           f"in {months} months")
    return out


def next_steps(rec: mr.Record, rep: Report, target: int) -> list:
    """The three most valuable things to do next, in order."""
    steps = []
    issues = [i for r in rep.roles for i in r.issues] + rep.record_issues
    if issues:
        steps.append(f"Settle {len(issues)} fact issue(s) listed above; they reach resumes as written.")
    # the most recent roles matter most to a reader, so they come first
    for h in [h for h in rep.roles if not h.outside][:3]:
        if h.accomplishments + h.resume_only < min(target, 5):
            steps.append(f"Interview {h.label}: only {h.accomplishments + h.resume_only} "
                         f"accomplishment(s) recorded.")
        elif h.resume_only:
            steps.append(f"Open up {h.resume_only} resume bullet(s) at {h.label} "
                         f"to find the numbers behind them.")
        if len(h.context_missing) >= 3:
            steps.append(f"Answer the role questions for {h.label} "
                         f"({', '.join(h.context_missing)}).")
    if rec.roles and not rep.has_degree:
        steps.append("No degree on your record. Employers still lean on one when comparing "
                     "candidates, so make the substitutes visible: certifications, training "
                     "on the job with the skills it taught, and the size of what you ran.")
    if rec.roles and not rec.summary:
        steps.append("Write your professional summary, or have one drafted from your record.")
    if rec.roles and not rep.skills_list_built:
        steps.append("Build your skills list: the standard skills for your field, "
                     "filled in from your record.")
    if rep.skills_unconfirmed:
        steps.append(f"Answer {rep.skills_unconfirmed} skill(s) on your list: yes with a level, or no.")
    if rep.skills_without_level:
        steps.append(f"Set a level for {rep.skills_without_level} skill(s) you have.")
    return steps[:5]


def render(rep: Report) -> str:
    out = ["", f"{'Role':44} {'Recorded':>9} {'Full':>5} {'With #':>7}  Missing context"]
    for h in rep.roles:
        missing = ", ".join(h.context_missing) or "-"
        out.append(f"{h.label[:44]:44} {h.accomplishments + h.resume_only:>9} "
                   f"{h.accomplishments:>5} {h.quantified:>7}  {missing}")
    out.append("")
    out.append(f"{rep.total} accomplishment(s); {rep.quantified} with a figure. "
               f"{rep.skills_total} skill(s), {rep.skills_unconfirmed} not yet "
               f"answered, {len(rep.skills_without_evidence)} with no evidence linked.")
    problems = [(h.label, i) for h in rep.roles for i in h.issues]
    if problems or rep.record_issues:
        out += ["", "Fix before export:"]
        out += [f"  - {label}: {i}" for label, i in problems]
        out += [f"  - {i}" for i in rep.record_issues]
    if rep.progression:
        out += ["", "Promotions worth showing:"]
        out += [f"  - {p}" for p in rep.progression]
    no_result = [(h.label, t) for h in rep.roles for t in h.no_result]
    if no_result:
        out += ["", "Accomplishments with no result yet:"]
        out += [f"  - {label}: {t}" for label, t in no_result]
    if rep.next_steps:
        out += ["", "Best use of your next hour:"]
        out += [f"  {i}. {s}" for i, s in enumerate(rep.next_steps, 1)]
    return "\n".join(out)
