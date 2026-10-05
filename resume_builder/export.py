"""
Hand the master record to the job-application pipeline.

The pipeline reads two files: `master_profile.json` (work history as lists of
finished bullets, plus identity, education and the rest) and
`skills_inventory.csv` (the source of truth for skills). This writes both,
from the record, under four rules that each cost something to learn:

  1. Never lose a highlight. The record is the newer file, not always the
     fuller one. If the profile holds a highlight with no counterpart in the
     record, the export refuses; `adopt` copies those into the record first.
  2. Fill blanks; never overwrite. A value already in the profile was probably
     curated by hand. Differences are reported and a person settles them.
  3. Strip annotations. The record keeps an unsettled conflict visible as an
     HTML comment; that is a note for a person, never content.
  4. Back up both files before writing either.

A bullet is drafted once per accomplishment and kept beside its source in the
record. It is redrafted only when the Problem, Actions or Results it came from
change, so a bullet someone rewrote by hand survives every export.
"""
import csv
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import bullets as B
from . import record as mr

ANNOTATION = re.compile(r"\s*<!--.*?-->\s*", re.S)


# Answers that mean "nothing to say". A questionnaire's "Recognition: No" or
# "Budget: NA" is an honest answer in the record and noise on a resume.
NOTHING = {"na", "n/a", "no", "none", "nil", "-", "--", "not applicable"}


def clean(value: str) -> str:
    value = ANNOTATION.sub(" ", value or "").strip()
    return "" if value.lower().rstrip(".") in NOTHING else value


def fingerprint(acc) -> str:
    return hashlib.sha1(acc.source_text().encode("utf-8")).hexdigest()[:12]


def _words(text):
    return set(re.findall(r"[a-z]{4,}", (text or "").lower()))


def orphans(role: mr.Role | None, highlights: list) -> list:
    """Profile highlights with no counterpart in the record. Forgiving on
    purpose: a false orphan costs a duplicate someone deletes; a missed one
    costs them an accomplishment."""
    have = []
    if role is not None:
        have = ([a.title + " " + a.source_text() + " " + a.bullet for a in role.accomplishments]
                + list(role.recorded_bullets))
    out = []
    for h in highlights:
        hw = _words(h)
        if not hw:
            continue
        best = max((len(hw & _words(t)) / len(hw) for t in have), default=0.0)
        if best < 0.45:
            out.append(h)
    return out


class Export:
    def __init__(self, rec: mr.Record, profile: dict, prints: dict | None = None,
                 with_details: bool = False):
        self.rec = rec
        self.profile = profile
        self.prints = dict(prints or {})
        self.with_details = with_details
        self.log = []
        self.conflicts = []
        self.drafted = self.reused = 0

    # -- checks -------------------------------------------------------------

    def pairs(self) -> list:
        """
        (role or None, profile entry) for each work_history entry.

        Matched one to one: employer and title first, then employer and dates,
        then an employer's one remaining role. Matching on employer alone puts
        both of a promoted person's titles on the first role.
        """
        entries = self.profile.get("work_history", [])
        found, used = {}, set()

        def digits(text):
            return re.sub(r"\D", "", ANNOTATION.sub("", text or ""))

        tests = [
            lambda r, e: mr._squash(r.title) == mr._squash(e.get("title", "")),
            lambda r, e: digits(r.fields.get("Dates")) and
                         digits(r.fields.get("Dates")) == digits(e.get("dates", "")),
        ]
        for same in tests:
            for i, e in enumerate(entries):
                if i in found:
                    continue
                for r in self.rec.roles:
                    if (id(r) not in used and mr._squash(r.employer) == mr._squash(e.get("company", ""))
                            and same(r, e)):
                        found[i] = r
                        used.add(id(r))
                        break
        for i, e in enumerate(entries):
            key = mr._squash(e.get("company", ""))
            roles = [r for r in self.rec.roles
                     if id(r) not in used and mr._squash(r.employer) == key]
            others = [j for j, x in enumerate(entries)
                      if j not in found and mr._squash(x.get("company", "")) == key]
            if i not in found and len(roles) == 1 and others == [i]:
                found[i] = roles[0]
                used.add(id(roles[0]))
        return [(found.get(i), e) for i, e in enumerate(entries)]

    def _name(self, entry) -> str:
        """The employer, with the title when the employer appears twice."""
        company = entry.get("company", "")
        twice = sum(1 for e in self.profile.get("work_history", [])
                    if mr._squash(e.get("company", "")) == mr._squash(company)) > 1
        return f"{company} — {entry.get('title', '')}" if twice else company

    def _losses(self) -> list:
        out = []
        for role, entry in self.pairs():
            if role is None:
                continue    # roles only in the profile are carried over untouched
            missing = orphans(role, entry.get("highlights", []))
            if missing:
                out.append((role, self._name(entry), missing))
        return out

    def losses(self) -> dict:
        """employer (with title, if the employer appears twice) -> highlights
        the export would drop."""
        return {name: missing for _, name, missing in self._losses()}

    def adopt(self) -> int:
        added = 0
        for role, _, missing in self._losses():
            role.recorded_bullets.extend(missing)
            role.notes.append(
                f"{len(missing)} highlight(s) copied from master_profile.json on "
                f"{datetime.now():%Y-%m-%d}. Some may restate an accomplishment above; "
                f"delete any duplicates.")
            added += len(missing)
        return added

    # -- building -----------------------------------------------------------

    def _take(self, entry: dict, key: str, value: str, who: str) -> None:
        value = clean(value)
        if not value:
            return
        current = (entry.get(key) or "").strip() if isinstance(entry.get(key), str) else entry.get(key)
        if not current:
            entry[key] = value
        elif current != value:
            self.conflicts.append(f"{who} {key}: kept {str(current)[:48]!r}, "
                                  f"record says {value[:48]!r}")

    def bullet_for(self, role: mr.Role, acc) -> str:
        key = f"{mr._squash(role.employer)}:{mr._squash(acc.title)}"
        fp = fingerprint(acc)
        if acc.bullet and self.prints.get(key) == fp:
            self.reused += 1
            return acc.bullet
        if acc.bullet and key not in self.prints:
            # A bullet with no fingerprint was written by a person, or by an
            # earlier tool. Adopt it as-is rather than redrafting their words.
            self.prints[key] = fp
            self.reused += 1
            return acc.bullet
        acc.bullet = B.compile_bullet(acc)
        self.prints[key] = fp
        self.drafted += 1
        return acc.bullet

    def work_history(self) -> list:
        pairs = self.pairs()
        existing = {id(role): e for role, e in pairs if role is not None}
        history = []
        for role in self.rec.roles:
            entry = dict(existing.get(id(role), {}))
            entry.setdefault("company", role.employer)
            self._take(entry, "title", role.title, role.employer)
            self._take(entry, "dates", role.fields.get("Dates"), role.employer)
            self._take(entry, "location", role.fields.get("Location"), role.employer)
            self._take(entry, "company_descriptor", role.fields.get("Company"), role.employer)
            scope = "; ".join(clean(role.fields[k]) for k in
                              ("Authority", "Budget", "Territory", "Reported to")
                              if clean(role.fields.get(k, "")))
            self._take(entry, "scope", scope, role.employer)
            if clean(role.fields.get("Challenge", "")):
                self._take(entry, "challenge", role.fields["Challenge"], role.employer)
            if clean(role.fields.get("Recognition", "")):
                self._take(entry, "recognition", role.fields["Recognition"], role.employer)

            highlights = [self.bullet_for(role, a) for a in role.accomplishments
                          if not a.is_empty()]
            highlights += [b for b in role.recorded_bullets if b not in highlights]
            # Keep the profile's own highlights that the record also holds in
            # another wording? No: losses() already proved every one has a
            # counterpart, so the record's version replaces it.
            entry["highlights"] = highlights
            if self.with_details:
                entry["accomplishments"] = [
                    {"title": a.title, "problem": clean(a.problem),
                     "actions": clean(a.actions), "results": clean(a.results),
                     "evidence": a.evidence, "bullet": a.bullet}
                    for a in role.accomplishments if not a.is_empty()]
            history.append(entry)
        history += [e for role, e in pairs if role is None]
        return history

    def identity(self) -> None:
        p = self.profile
        mapping = {"Name": "name", "Email": "email", "Telephone": "phone",
                   "Location": "location", "Linkedin": "linkedin"}
        for label, key in mapping.items():
            self._take(p, key, self.rec.contact.get(label, ""), "contact")
        if self.rec.sets_apart:
            self._take(p, "differentiator", " ".join(self.rec.sets_apart), "positioning")
        titles = self.rec.target.get("Titles", "")
        if titles and not p.get("target_titles"):
            p["target_titles"] = [t.strip() for t in re.split(r"[;,]", titles) if t.strip()]
        self._take(p, "summary", self.rec.summary, "summary")
        for name in ("education", "certifications", *mr.EXTRA_KEYS):
            have = p.get(name) or []
            known = {mr._squash(json.dumps(x) if not isinstance(x, str) else x) for x in have}
            items = getattr(self.rec, name) if name in ("education", "certifications") \
                else self.rec.extras.get(name, [])
            for item in items:
                if mr._squash(item) not in known:
                    have.append(clean(item))
            if have:
                p[name] = have

    def build(self) -> dict:
        self.profile["work_history"] = self.work_history()
        self.identity()
        return self.profile


# --------------------------------------------------------------------------
# skills_inventory.csv

CSV_COLUMNS = ["category", "skill", "have_it", "proficiency", "source", "notes"]


def skills_rows(rec: mr.Record) -> list:
    """
    One CSV row per skill, in the layout of the pipeline's skills_inventory.csv.
    have_it is the person's answer: yes, no (a gap in their field), or verify
    (not answered yet), which the pipeline treats as not-yes and keeps off
    resumes until the person says yes.
    """
    from .skills import UNSORTED
    rows = []
    for s in rec.skills:
        cat = s.category if (s.category or "").strip().lower() not in UNSORTED else "Other"
        rows.append({
            "category": cat,
            "skill": s.name,
            "have_it": s.have,
            "proficiency": s.level if s.have == "yes" else "",
            "source": s.source or "record",
            "notes": ("evidence: " + "; ".join(s.evidence)) if s.evidence else "",
        })
    return rows


def merge_csv(existing: list, new: list) -> tuple:
    """Add skills the CSV lacks. Never changes or removes a row someone may
    have edited in a spreadsheet. Returns (rows, added)."""
    known = {mr._squash(r.get("skill", "")) for r in existing}
    added = [r for r in new if mr._squash(r["skill"]) not in known]
    return existing + added, len(added)


def read_csv(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list) -> None:
    cols = list(CSV_COLUMNS)
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def backup(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    shutil.copy2(path, dest)
    return dest
