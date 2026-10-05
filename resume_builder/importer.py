"""
Start a master record from whatever someone already has: an old resume, a
LinkedIn profile saved as PDF, or text pasted into a file.

The model's only job here is to say which line belongs where. It does not
rewrite, summarise or improve anything, and that is checked rather than
trusted: every line it returns must be found in the source, and every source
line it did not place is kept in an "Unplaced" section. A parser that silently
drops half a document looks exactly like one that works.

Imported bullets land as "recorded resume bullets": already compressed, so the
problem behind them and the steps taken are missing. The interview expands
them later; until then they are usable as they stand.
"""
import re
import zipfile
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path

from . import llm
from . import record as mr

# --------------------------------------------------------------------------
# Reading files


def read_text(path: Path) -> str:
    """Plain text from a .docx, .pdf, .txt or .md file."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _docx_text(path)
    if suffix == ".pdf":
        return _pdf_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def _docx_text(path: Path) -> str:
    """A .docx is a zip; word/document.xml holds the text, one <w:p> per
    paragraph. Tables are read cell by cell, which is how most templated
    resumes lay out dates beside titles."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        p = re.sub(r"<w:tab/>", "\t", p)
        p = re.sub(r"<w:br/>", "\n", p)
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S))
        paras.append(unescape(text))
    return "\n".join(paras)


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("Reading PDFs needs the pypdf package: pip install pypdf")
    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


# --------------------------------------------------------------------------
# Matching what the model returned against the source


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"^[\s•●▪◦*·\-–—o]+", "", text)        # bullet glyphs
    return re.sub(r"[^a-z0-9%$]+", " ", text).strip()


def source_lines(text: str) -> list:
    return [l.strip() for l in (text or "").splitlines() if _norm(l)]


def found_in(fragment: str, source_norm: str) -> bool:
    """
    Is this fragment in the source, allowing for the damage PDF extraction
    does: a line broken in two, hyphenation, odd spacing. Exact containment
    after normalising first; then a close match against a window of the same
    length, which catches a stray character without accepting a rewrite.
    """
    f = _norm(fragment)
    if not f:
        return True
    if f in source_norm:
        return True
    n = len(f)
    if n < 12:
        return False
    best = 0.0
    step = max(1, n // 4)
    for i in range(0, max(1, len(source_norm) - n + 1), step):
        window = source_norm[i:i + n + n // 10]
        best = max(best, SequenceMatcher(None, f, window).ratio())
        if best >= 0.92:
            return True
    return False


# --------------------------------------------------------------------------
# The model's part

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "contact": {"type": "object", "properties": {
            "Name": {"type": "string"}, "Location": {"type": "string"},
            "Email": {"type": "string"}, "Telephone": {"type": "string"},
            "Linkedin": {"type": "string"}, "Website": {"type": "string"}},
            "required": ["Name", "Location", "Email", "Telephone", "Linkedin", "Website"],
            "additionalProperties": False},
        "summary": {"type": "array", "items": {"type": "string"}},
        "roles": {"type": "array", "items": {"type": "object", "properties": {
            "employer": {"type": "string"}, "title": {"type": "string"},
            "dates": {"type": "string"}, "location": {"type": "string"},
            "company_description": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}}},
            "required": ["employer", "title", "dates", "location",
                         "company_description", "bullets"],
            "additionalProperties": False}},
        "skills": {"type": "array", "items": {"type": "string"}},
        "education": {"type": "array", "items": {"type": "string"}},
        "certifications": {"type": "array", "items": {"type": "string"}},
        "other": {"type": "array", "items": {"type": "object", "properties": {
            "heading": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "string"}}},
            "required": ["heading", "lines"], "additionalProperties": False}},
    },
    "required": ["contact", "summary", "roles", "skills", "education",
                 "certifications", "other"],
    "additionalProperties": False,
}

EXTRACT_PROMPT = """Below is the text of someone's resume or professional
profile. Sort its lines into the JSON structure. This is filing, not writing.

RULES
- Copy text exactly as it appears. Do not reword, correct, shorten, merge or
  improve anything. If a bullet is badly written, copy it badly written.
- A bullet that the text extraction broke across two lines may be rejoined;
  nothing else may be joined.
- One entry in "roles" per job. If the same employer appears with two
  titles, that is two roles. Newest first, as in the document.
- "dates" exactly as written, e.g. "Jan 2019 - Present" or "2016-2019".
- "company_description": only if the document describes the employer
  (size, industry); otherwise "".
- "skills": individual skills, tools, languages and techniques, each as the
  document writes it. Split a comma-separated list into its items.
- Licences and certifications go in "certifications", not "skills".
- Anything that fits none of the fields (volunteer work, publications,
  awards, languages, interests) goes in "other" under the heading the
  document used.
- Leave a field "" or [] when the document does not have it. Never infer.

DOCUMENT
{text}"""


def extract(text: str) -> dict:
    return llm.request_json(
        [{"role": "user", "content": EXTRACT_PROMPT.format(text=text[:60000])}],
        16000, "import", schema=EXTRACT_SCHEMA)


# --------------------------------------------------------------------------
# Building the record


def to_record(data: dict, source_text: str, source_name: str,
              today: str) -> tuple[mr.Record, dict]:
    """
    Turn the model's filing into a Record, checking every line against the
    source. Returns the record and a report of what was checked.

    A line the model returned that is not in the source is not used: it goes
    to "Not found in the source" for a person to look at. A source line the
    model never placed goes to "Unplaced". Both sections are plain Markdown
    the person can edit or delete.
    """
    src_norm = _norm(" ".join(source_lines(source_text)))
    report = {"placed": 0, "rejected": [], "unplaced": []}
    placed_norm = []

    def keep(line: str) -> bool:
        line = (line or "").strip()
        if not line:
            return False
        if found_in(line, src_norm):
            report["placed"] += 1
            placed_norm.append(_norm(line))
            return True
        report["rejected"].append(line)
        return False

    rec = mr.Record(header=[
        "# Master record", "",
        f"Imported from `{source_name}` on {today}. Every line below the",
        "Roles heading was copied from that document, not rewritten.", "",
        "This record has no page limit. Add everything; the job-application",
        "pipeline chooses what fits each posting."])

    for key, value in (data.get("contact") or {}).items():
        if value and keep(value):
            rec.contact[key] = value.strip()

    rec.summary = " ".join(l.strip() for l in data.get("summary") or [] if keep(l))

    for r in data.get("roles") or []:
        employer, title = (r.get("employer") or "").strip(), (r.get("title") or "").strip()
        if not (employer or title):
            continue
        for part in (employer, title):
            if part:
                keep(part)
        role = mr.Role(employer=employer or "[FILL IN: employer]",
                       title=title or "[FILL IN: title]")
        if r.get("dates") and keep(r["dates"]):
            role.fields["Dates"] = r["dates"].strip()
        if r.get("location") and keep(r["location"]):
            role.fields["Location"] = r["location"].strip()
        if r.get("company_description") and keep(r["company_description"]):
            role.fields["Company"] = r["company_description"].strip()
        for b in r.get("bullets") or []:
            if keep(b):
                role.recorded_bullets.append(b.strip().lstrip("•●▪◦*·-–— ").strip())
        rec.roles.append(role)

    for s in data.get("skills") or []:
        if keep(s) and not rec.skill(s):
            rec.skills.append(mr.Skill(name=s.strip(), category="Imported", source="resume"))
    for name in ("education", "certifications"):
        for line in data.get(name) or []:
            if keep(line):
                getattr(rec, name).append(line.strip())

    trailing = []
    for block in data.get("other") or []:
        lines = [l for l in block.get("lines") or [] if keep(l)]
        if not lines:
            continue
        heading = (block.get("heading") or "Other").strip()
        key = mr.extra_key(heading)
        if key:                     # volunteer work, awards, languages and the like
            rec.extras.setdefault(key, []).extend(l.strip() for l in lines)
        else:
            trailing += ["", f"## {heading}", ""] + [f"- {l.strip()}" for l in lines]

    # What the model never placed. Short lines that are all section headings
    # ("EXPERIENCE") are not content and are not reported.
    for line in source_lines(source_text):
        n = _norm(line)
        if len(n) < 4 or line.isupper() and len(line.split()) <= 3:
            continue
        if not any(n in p or p in n for p in placed_norm if len(p) >= 4):
            report["unplaced"].append(line)

    if report["rejected"]:
        trailing += ["", "## Not found in the source", "",
                     "The importer returned these, but they are not in the document,",
                     "so they were not used. Delete this section once checked.", ""]
        trailing += [f"- {l}" for l in report["rejected"]]
    if report["unplaced"]:
        trailing += ["", "## Unplaced", "",
                     "Lines from the document the importer did not file. Move anything",
                     "worth keeping into the right place, then delete this section.", ""]
        trailing += [f"- {l}" for l in report["unplaced"]]
    rec.trailing = trailing
    return rec, report


def merge(existing: mr.Record, incoming: mr.Record) -> list:
    """
    Add what the existing record lacks. Never replaces a value: where the two
    disagree, the incoming value is left beside the existing one as an HTML
    comment for a person to settle. Returns a list of what changed.
    """
    changes = []
    for key, value in incoming.contact.items():
        if not existing.contact.get(key):
            existing.contact[key] = value
            changes.append(f"contact: added {key}")
    for role in incoming.roles:
        have = existing.role_by_employer(role.employer)
        if have is None or _norm(have.title) != _norm(role.title):
            existing.roles.append(role)
            changes.append(f"role added: {role.label()}")
            continue
        for label, value in role.fields.items():
            if not have.fields.get(label):
                have.fields[label] = value
            elif _norm(have.fields[label]) != _norm(value) and "<!--" not in have.fields[label]:
                have.fields[label] += f"  <!-- the import says: {value} -->"
                changes.append(f"conflict noted: {role.employer} {label}")
        known = {_norm(b) for b in have.recorded_bullets}
        known |= {_norm(a.bullet) for a in have.accomplishments if a.bullet}
        for b in role.recorded_bullets:
            if _norm(b) not in known:
                have.recorded_bullets.append(b)
                changes.append(f"bullet added: {role.employer}")
    for s in incoming.skills:
        if not existing.skill(s.name):
            existing.skills.append(s)
            changes.append(f"skill added: {s.name}")
    for name in ("education", "certifications"):
        mine = {_norm(x) for x in getattr(existing, name)}
        for item in getattr(incoming, name):
            if _norm(item) not in mine:
                getattr(existing, name).append(item)
                changes.append(f"{name}: added {item}")
    for key, items in incoming.extras.items():
        mine = existing.extras.setdefault(key, [])
        known = {_norm(x) for x in mine}
        for item in items:
            if _norm(item) not in known:
                mine.append(item)
                changes.append(f"{key}: added {item}")
    if incoming.summary and not existing.summary:
        existing.summary = incoming.summary
        changes.append("summary added")
    elif incoming.summary and _norm(incoming.summary) != _norm(existing.summary):
        existing.trailing += ["", "## Summary from the import", "",
                              "Kept beside yours rather than replacing it.", "",
                              incoming.summary]
        changes.append("conflict noted: summary")
    existing.trailing += incoming.trailing
    return changes
