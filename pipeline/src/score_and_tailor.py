"""
Scores each posting for fit against the master profile, and drafts a
tailored resume for postings above a score threshold.

Requires: pip install anthropic
Set ANTHROPIC_API_KEY in your environment before running.
"""
import csv
import html
import json
import os
import re
from datetime import date
from pathlib import Path
import anthropic

PROFILE_DIR = Path(__file__).parent.parent / "profile"
PROFILE_PATH = PROFILE_DIR / "master_profile.json"
SKILLS_CSV_PATH = PROFILE_DIR / "skills_inventory.csv"
OUTPUT_DIR = Path(__file__).parent.parent / "output"

# Tuning lives in config/tuning.yaml; these are the defaults when that file is
# absent. settings.py stops the run if the file exists but is malformed.
from settings import load_letter, load_tuning          # noqa: E402
import user_config                                      # noqa: E402

_TUNING = load_tuning()
MODEL = _TUNING["model"]
SCORE_THRESHOLD = _TUNING["score_threshold"]


# Inside user_config.using(...), one person's settings apply; outside it, the
# values from config/ above. Functions read through these rather than the
# module constants, which stay for the command-line scripts that import them.
def _tune(key: str):
    person = user_config.current()
    return person.tuning[key] if person else _TUNING[key]


def score_threshold() -> int:
    return _tune("score_threshold")

client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env


def load_skills_csv(path: Path | None = None) -> tuple[dict, list] | None:
    """
    skills_inventory.csv is the source of truth for skills. Returns
    (skills_by_category, certifications), or None if the file is absent so
    the caller can fall back to whatever is in master_profile.json.

    Only rows with have_it=yes are used. Rows marked no are development
    targets and must never reach a resume. Proficiency, when present, is
    appended so the tailoring step can lead with genuine strengths.
    """
    path = path or SKILLS_CSV_PATH
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8-sig") as f:
        return skills_from_rows(csv.DictReader(f))


def skills_from_rows(rows) -> tuple[dict, list]:
    """The same reading as load_skills_csv, from rows already in memory: the
    hosted product builds them from a person's record, not from a file."""
    by_category: dict[str, list[str]] = {}
    certifications: list[str] = []
    skipped = 0

    for row in rows:
        skill = (row.get("skill") or "").strip()
        if not skill:
            continue
        if (row.get("have_it") or "").strip().lower() not in ("yes", "y", "true", "1"):
            skipped += 1
            continue

        category = (row.get("category") or "Other").strip() or "Other"
        proficiency = (row.get("proficiency") or "").strip()
        notes = (row.get("notes") or "").strip()

        if category.lower().startswith("certification"):
            certifications.append(skill)
            continue

        entry = skill
        if proficiency:
            entry += f" ({proficiency})"
        if notes:
            entry += f" [{notes}]"
        by_category.setdefault(category, []).append(entry)

    total = sum(len(v) for v in by_category.values()) + len(certifications)
    print(f"  loaded {total} skills from skills_inventory.csv "
          f"({skipped} marked have_it=no, excluded)")
    return by_category, certifications


def load_profile() -> dict:
    with open(PROFILE_PATH, encoding="utf-8") as f:
        profile = json.load(f)
    return with_skills(profile, load_skills_csv())


def with_skills(profile: dict, loaded: tuple[dict, list] | None) -> dict:
    """Put the skills inventory into the profile in place of the profile's own
    skill lists. `loaded` is what load_skills_csv or skills_from_rows returned."""
    if loaded is None:
        return profile

    by_category, certifications = loaded
    if not by_category and not certifications:
        print("  [warn] skills_inventory.csv had no usable rows -- "
              "falling back to skills in master_profile.json")
        return profile

    # CSV wins: drop the JSON copies so the model never sees two
    # contradictory skill lists.
    profile.pop("skills", None)
    profile.pop("technical_stack", None)
    profile["skills_by_category"] = by_category
    if certifications:
        existing = profile.get("certifications", [])
        merged = list(dict.fromkeys(certifications + existing))
        profile["certifications"] = merged
    return profile


def strip_html(html: str) -> str:
    return re.sub("<[^<]+?>", " ", html or "")


def extract_text(resp) -> str:
    parts = [b.text for b in resp.content if b.type == "text"]
    if not parts:
        raise ValueError(
            f"No text block in response (stop_reason={resp.stop_reason}, "
            f"blocks={[b.type for b in resp.content]})"
        )
    return "\n".join(parts)


def parse_json_response(text: str) -> dict:
    """
    Models occasionally wrap JSON in prose or code fences, or slip in a
    // comment. Pull out the outermost {...} and strip comments before
    parsing so one stray character doesn't kill a whole pipeline run.
    """
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # retry without // line comments and trailing commas
        stripped = re.sub(r"(?<!:)//[^\n]*", "", cleaned)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        return json.loads(stripped)


def profile_preamble(lead: str, profile: dict) -> str:
    """
    The opening every prompt shares: one line of instruction and the whole
    profile. Identical for every posting in a run, which is what makes it worth
    caching -- see cached_messages below.
    """
    return f"{lead}\n\nCANDIDATE PROFILE:\n{json.dumps(profile, indent=2)}\n"


def cached_messages(stable: str, rest: str) -> list[dict]:
    """
    One user message in two blocks, with the stable half marked for caching.

    Caching is a prefix match, so the only thing that can be cached is text
    that comes before anything posting-specific. That rules out the long rules
    blocks, which deliberately sit after the posting; what it leaves is the
    profile, and the profile is most of the prompt -- scoring sends about 9,100
    input tokens against 260 out.

    The two blocks concatenate to exactly the string this used to send as one,
    so the model sees the same prompt it did before. Cache reads cost a tenth
    of base input and a write costs 1.25x, so this pays for itself on the second
    posting of a run and every one after it.

    A read also refreshes the 5-minute window, so a run that scores postings
    back to back keeps the entry warm from start to finish.
    """
    return [{"role": "user", "content": [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": rest},
    ]}]


# Token tally for the run, so the caching above is observable rather than
# claimed. A cache that quietly stops being read -- one changed byte in the
# profile preamble would do it -- costs ten times more per call and reports
# nothing at all, which is the failure shape this project keeps finding.
_SPEND = {"fresh": 0, "written": 0, "read": 0, "out": 0, "calls": 0}
# $ per million tokens for the model in tuning.yaml, used only for the summary
# line. Wrong rates here cost nothing but a misleading number.
_RATES = {"claude-sonnet-5": (2.00, 10.00), "claude-sonnet-5-5": (2.00, 10.00),
          "claude-opus-5": (5.00, 25.00), "claude-haiku-4-5": (1.00, 5.00)}


def _tally(usage) -> None:
    _SPEND["calls"] += 1
    _SPEND["fresh"] += getattr(usage, "input_tokens", 0) or 0
    _SPEND["written"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
    _SPEND["read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
    _SPEND["out"] += getattr(usage, "output_tokens", 0) or 0


def spend_summary() -> str:
    """One line on what the run cost and whether the cache was doing its job."""
    if not _SPEND["calls"]:
        return ""
    in_rate, out_rate = _RATES.get(MODEL, (2.00, 10.00))
    cost = (_SPEND["fresh"] / 1e6 * in_rate
            + _SPEND["written"] / 1e6 * in_rate * 1.25
            + _SPEND["read"] / 1e6 * in_rate * 0.10
            + _SPEND["out"] / 1e6 * out_rate)
    # what those cached reads would have cost at full price
    saved = _SPEND["read"] / 1e6 * in_rate * 0.90
    line = (f"  {_SPEND['calls']} model call(s), "
            f"{_SPEND['fresh'] + _SPEND['written'] + _SPEND['read']:,} in / "
            f"{_SPEND['out']:,} out, about ${cost:.2f}")
    if _SPEND["read"]:
        line += f" -- prompt cache saved about ${saved:.2f}"
    elif _SPEND["written"]:
        line += ("  [warn] nothing was read from the prompt cache; the cached "
                 "prefix is varying between calls")
    return line


def request_json(messages: list[dict], max_tokens: int, what: str,
                 attempts: int = 2, schema: dict | None = None) -> tuple[dict, str]:
    """
    Call the model, parse its JSON, and give it one corrective pass when the
    response does not parse. Returns (data, the raw text that parsed).

    parse_json_response already repairs the usual damage -- code fences, stray
    prose, // comments, trailing commas. What it cannot fix is JSON that is
    genuinely malformed, and that happens: one resume died on "Expecting ','
    delimiter: line 43 column 6", the cover letter for the same posting drafted
    fine, and a plain retry with no prompt change produced valid JSON. The
    failure is transient, so treating it as fatal cost a document and a day.

    Same pattern as the style lint below: name the specific violation and hand
    it back, rather than hoping the next draft happens to be clean.

    `messages` is appended to in place, so a caller that continues the
    conversation keeps the failed exchange as context.

    Truncation is not retried. Re-asking with the same budget truncates again,
    so it raises with the budget to change and the function to change it in.

    `schema` turns the contract into a constraint the API enforces, rather than
    an instruction the prompt asks for. It is worth passing wherever the prompt
    also has a conversational job: the interview asks a person a question and
    returns a record of the answer, and told to do both it did the human half
    and dropped the JSON, on almost every turn. The retry above covered for it
    and doubled the cost of every question. Where a prompt only ever produces
    JSON -- scoring, drafting -- it is not needed.
    """
    last = None
    for attempt in range(attempts):
        extra = ({"output_config": {"format": {"type": "json_schema",
                                               "schema": schema}}}
                 if schema else {})
        resp = client.messages.create(model=MODEL, max_tokens=max_tokens,
                                      messages=messages, **extra)
        _tally(resp.usage)
        text = extract_text(resp).strip()
        if resp.stop_reason == "max_tokens":
            # Surfaces as a confusing "Unterminated string" from the parser
            # otherwise, which sent me hunting the wrong bug.
            raise ValueError(
                f"{what} response truncated at max_tokens "
                f"({resp.usage.output_tokens} out); raise max_tokens")
        try:
            return parse_json_response(text), text
        except json.JSONDecodeError as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            print(f"    [json] {what} response did not parse ({exc}); asking again")
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                    f"That response is not valid JSON: {exc}. Send the same "
                    "content again as strictly valid JSON, with no code fences, "
                    "no comments, no trailing commas, and every newline inside "
                    "a string escaped as \\n. Respond ONLY with the JSON."},
            ]
    raise last


SALARY_FLOOR = _TUNING["salary_floor"]   # at or above, seniority concerns don't apply

# "$150,000 - $185,000" / "$150K-$185K" / "$150,000 to $250,000" and the
# en/em-dash variants pay-transparency boilerplate tends to use.
#
# The unit group matters: the LinkedIn actor hands over "Salary: $160,300.00/yr
# - $253,600.00/yr", and with no way to skip a "/yr" before the dash the whole
# range failed to match, so the one authoritative figure in the posting was
# invisible. It also says outright whether the pair is annual or a rate.
_SALARY_RANGE = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?([kK])?"
    r"(?:\s*/\s*(yr|year|hr|hour|mo|month))?"
    r"\s*(?:-|–|—|to)\s*\$?\s?"
    r"(\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?([kK])?")


# no \b before "/hr" -- a slash is not a word character, so the boundary never
# matches and hourly rates slip through
_RATE_TRAILING = re.compile(r"per\s+hour|hourly|an\s+hour|/\s?h(?:r|our)|\bp/?h\b", re.I)
_ANNUAL_TRAILING = re.compile(r"/\s?yr|per year|per annum|annually|a year|usd", re.I)
_PAY_CONTEXT = re.compile(r"salar|compensat|\bpay\b|annual|total target|earn", re.I)


def extract_salary_range(text: str) -> tuple[int, int] | None:
    """
    Pull the annual salary range a posting states for the role.

    Returns (low, high) in dollars, or None.

    This took the largest plausible pair, which is wrong when a posting states
    more than one range. One listed $160,300-$253,600 for the role and
    $192,300-$304,200 "in the select locations listed above", and the tracker
    reported the second -- a premium that applies to a handful of metros and
    not to the person reading it. Another stated a USD range followed by two
    CAD ranges, and the largest pair was Canadian dollars recorded as dollars. The range a posting states first is its
    general one; higher location-adjusted bands follow it.

    So: the first range in a pay context wins, falling back to the first
    plausible range if nothing is in context.
    """
    text = text or ""
    candidates: list[tuple[bool, int, int]] = []

    for m in _SALARY_RANGE.finditer(text):
        trailing = text[m.end():m.end() + 30]
        leading = text[max(0, m.start() - 60):m.start()]
        unit = (m.group(3) or "").lower()
        if unit.startswith(("hr", "hour", "mo", "month")):
            continue                    # a rate or a monthly figure, not a salary
        if _RATE_TRAILING.search(trailing):
            continue                    # "$45.00 - $65.00 per hour"

        raw_low = float(m.group(1).replace(",", ""))
        raw_high = float(m.group(4).replace(",", ""))
        # Cents used to disqualify a range outright, on the theory that they
        # mean an hourly rate. They do on a small number; on a large one they
        # are just how pay-transparency boilerplate is written, and discarding
        # "$160,300.00/yr" threw away the one range that was correct.
        has_cents = "." in m.group(1) or "." in m.group(4)
        if has_cents and (raw_low < 1000 or raw_high < 1000):
            continue

        def val(raw, k):
            # "150K", or a bare "$150" that can only mean 150K
            return int(raw * 1000) if (k or raw < 1000) else int(raw)

        low, high = val(raw_low, m.group(2)), val(raw_high, m.group(5))
        if low > high or high < 30_000 or high > 2_000_000:
            continue                    # percentage or noise

        in_context = bool(unit.startswith(("yr", "year"))
                          or _PAY_CONTEXT.search(leading)
                          or _ANNUAL_TRAILING.search(trailing))
        candidates.append((in_context, low, high))

    for in_context, low, high in candidates:
        if in_context:
            return low, high
    return (candidates[0][1], candidates[0][2]) if candidates else None


def require_credentials() -> None:
    """
    Fail the run before any work starts if there is no usable credential.

    Without this the nightly run kept reporting success: each posting hit the
    auth error, got caught by the per-posting handler meant for one bad
    posting, and was skipped. Scoring was dead for days and the log still said
    "[OK] pipeline completed".

    An unset ANTHROPIC_API_KEY does not mean there are no credentials. The SDK
    resolves, in order: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, an `ant auth
    login` profile on disk, then Workload Identity Federation env vars. Check
    for all of them, or this rejects a machine that is perfectly able to run.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return

    # Workload Identity Federation: the SDK activates it only when all four are set
    wif = ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
           "ANTHROPIC_SERVICE_ACCOUNT_ID")
    if all(os.environ.get(v) for v in wif) and (
            os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE")
            or os.environ.get("ANTHROPIC_IDENTITY_TOKEN")):
        return

    # OAuth profile written by `ant auth login`
    config_dir = os.environ.get("ANTHROPIC_CONFIG_DIR")
    if config_dir:
        candidates = [Path(config_dir)]
    elif os.name == "nt":
        candidates = [Path(os.environ.get("APPDATA", "")) / "Anthropic"]
    else:
        candidates = [Path.home() / ".config" / "anthropic"]
    if any((c / "credentials").is_dir() and any((c / "credentials").glob("*.json"))
           for c in candidates if str(c)):
        return

    raise SystemExit(
        "[FATAL] no Anthropic credentials found, so nothing can be scored or drafted.\n"
        "        Either set a key (then open a new shell):\n"
        '          setx ANTHROPIC_API_KEY "sk-ant-..."\n'
        "        or sign in without a static key:\n"
        "          ant auth login\n"
        "        Keys: https://platform.claude.com/settings/keys"
    )


# kept so older callers/scripts don't break
require_api_key = require_credentials


def is_auth_error(exc: BaseException) -> bool:
    """An auth failure is fatal for the whole run, never a per-posting problem."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return ("authentication" in name
            or "permissiondenied" in name
            or "could not resolve authentication method" in msg
            or "invalid x-api-key" in msg
            or "401" in msg)


def _experience(profile: dict) -> str:
    """'18 years of experience, most recently as Head of Analytics', or as
    much of that as the profile actually says."""
    years = profile.get("years_experience")
    if isinstance(years, (int, float)) and not isinstance(years, bool):
        text = f"{years:g} years of experience"
    elif isinstance(years, str) and years.strip():
        y = years.strip()
        text = f"{y} experience" if "year" in y.lower() else f"{y} years of experience"
    else:
        text = "the experience shown in the profile"
    jobs = profile.get("work_history") or []
    title = (jobs[0].get("title") or "").strip() if jobs else ""
    return f"{text}, most recently as {title}" if title else text


def score_posting(posting: dict, profile: dict) -> dict:
    description = strip_html(posting["description_html"])
    salary = extract_salary_range(description)
    floor = _tune("salary_floor")
    background = _experience(profile)
    # Both directions: a new graduate is underqualified for a director role as
    # surely as a director is overqualified for an analyst one. The rule used
    # to assume a senior candidate, which only ever fitted one person.
    judge_both_ways = (
        f"The candidate has {background}. Weigh seniority honestly in both "
        "directions: set overqualification_risk to true if the role is clearly "
        "junior to that level, and lower the score if the role needs materially "
        "more seniority or scope than the profile shows.")

    if salary and floor and salary[1] >= floor:
        seniority_rule = (
            f"- Seniority match. This posting states a pay range of ${salary[0]:,} to "
            f"${salary[1]:,}. The top of that range is at or above ${floor:,}, "
            "which means the role is compensated at a level appropriate to the "
            "candidate's experience. DO NOT reduce the score for overqualification, "
            "and set overqualification_risk to false, even if the title reads as IC or "
            "manager-level. Companies paying this much expect deep experience. Judge "
            "seniority on the scope of the work described, not the title."
        )
    elif salary:
        below = f", topping out below ${floor:,}" if floor else ""
        seniority_rule = (
            f"- Seniority match. This posting states a pay range of ${salary[0]:,} to "
            f"${salary[1]:,}{below}. {judge_both_ways}"
        )
    else:
        seniority_rule = f"- Seniority match. No pay range is stated. {judge_both_ways}"

    stable = profile_preamble(
        "You are helping a job seeker evaluate whether a posting is a good fit.",
        profile)
    prompt = f"""
JOB POSTING:
Title: {posting['title']}
Company: {posting['company']}
Location: {posting['location']}
Description: {description[:4000]}

Score fit from 0-10 considering:
- Skills/experience match
{seniority_rule}
- Location/remote compatibility

Do NOT treat a personal characteristic as a disqualifier unless the profile
states it. Religious affiliation, citizenship, security clearance, veteran
status, language fluency, and willingness to relocate are unknown unless
written down. If a posting requires one and the profile is silent, say the
requirement exists and that it needs confirming; do not assume it is unmet
and do not lower the score for it. Judge on skills, seniority, and the
stated location only.

Respond ONLY with JSON, no other text, in this exact shape:
{{"score": <int 0-10>, "reasoning": "<2-3 sentences>", "overqualification_risk": <true/false>}}
"""
    # 4096, not 500: the seniority rule made this prompt longer and the model
    # reasons before answering, so a small budget truncates the JSON mid-string
    # and surfaces as a confusing parse error. Raised from 2048 after the same
    # thing happened to a resume on a long posting -- the answer here is three
    # sentences, so the budget is for the reasoning, and headroom is free.
    result, _ = request_json(cached_messages(stable, prompt), 4096, "scoring")
    if salary:
        result["salary_low"], result["salary_high"] = salary
        if floor and salary[1] >= floor:
            # belt and braces: the rule above is explicit, but this is a hard
            # constraint the user asked for, so enforce it rather than trust it
            result["overqualification_risk"] = False
    return result


RESUME_SCHEMA_EXAMPLE = {
    "name": "Jordan Example",
    "contact": "City, ST | email@example.com | 555-555-5555 | linkedin.com/in/example",
    "summary": "2-3 sentence professional summary tailored to this posting",
    "experience": [
        {
            "title": "Job Title",
            "company": "Company Name",
            "dates": "2020 - Present",
            "bullets": ["Achievement bullet 1", "Achievement bullet 2"]
        }
    ],
    "skills": [
        "Category Name: Term, Term, Term, Term, Term, Term",
        "Second Category: Term, Term, Term, Term",
    ],
    "education": ["Degree, School, Year"]
}


# Fitting two pages. Measured against real output: a 977-word resume with 33
# bullets fits; 1,071 words with 37 bullets spilled onto a third page.
MAX_RESUME_WORDS = _TUNING["max_resume_words"]
MAX_SUMMARY_WORDS = _TUNING["max_summary_words"]
# bullets allowed per role by position, newest first. Recent roles carry the
# argument; the oldest are there for continuity, not detail.
BULLETS_BY_POSITION = _TUNING["bullets_by_position"]
BULLETS_TAIL = _TUNING["bullets_tail"]        # anything beyond the list above


def _company_key(company) -> str:
    """Company name reduced to letters and digits, so 'Acme, Inc.' and
    'Acme Inc' count as one employer."""
    return re.sub(r"[^a-z0-9]", "", (company or "").lower())


def fit_to_two_pages(resume: dict) -> dict:
    """
    Trim a tailored resume so it renders on two pages.

    The model is told to keep it short and does not reliably comply, the
    same as with cover letter length. Trimming happens from the end of each
    role's bullet list, which is safe because bullets are ordered by
    relevance to the posting within each role.
    """
    def total_words(r):
        parts = [r.get("summary", "")]
        parts += [b for e in r.get("experience", []) for b in e.get("bullets", [])]
        parts += r.get("skills", [])
        parts += r.get("education", [])
        return len(" ".join(parts).split())

    before = total_words(resume)

    max_summary = _tune("max_summary_words")
    by_position, tail = _tune("bullets_by_position"), _tune("bullets_tail")

    summary = resume.get("summary", "").split()
    if len(summary) > max_summary:
        # cut at a sentence boundary rather than mid-clause
        text = " ".join(summary)
        sentences = re.split(r"(?<=[.!?])\s+", text)
        kept, n = [], 0
        for s in sentences:
            if n + len(s.split()) > max_summary and kept:
                break
            kept.append(s)
            n += len(s.split())
        resume["summary"] = " ".join(kept)

    # positions count employers, not entries: titles held at one company
    # (a promotion) stack under one heading and share its place. The latest
    # title takes the employer's cap; earlier titles there are context and
    # get the tail cap at most.
    employer, prev = -1, None
    for job in resume.get("experience", []):
        key = _company_key(job.get("company"))
        stacked = bool(key) and key == prev
        if not stacked:
            employer += 1
        prev = key
        cap = by_position[employer] if employer < len(by_position) else tail
        if stacked:
            cap = min(cap, tail)
        job["bullets"] = (job.get("bullets") or [])[:cap]

    # still over? drop the weakest remaining bullet from the oldest role that
    # has more than one, and repeat
    guard = 0
    while total_words(resume) > _tune("max_resume_words") and guard < 40:
        guard += 1
        for job in reversed(resume.get("experience", [])):
            if len(job.get("bullets") or []) > 1:
                job["bullets"].pop()
                break
        else:
            break

    after = total_words(resume)
    if after != before:
        print(f"    trimmed to fit two pages: {before} -> {after} words")
    return resume


MAX_SKILL_CATEGORIES = _TUNING["max_skill_categories"]
MAX_TERMS_PER_CATEGORY = _TUNING["max_terms_per_category"]


def normalize_skills(resume: dict) -> dict:
    """
    Force the skills block into "Category: term, term" lines of atomic terms.

    Two failure modes this fixes, both measured on real output:
      - descriptive phrases instead of terms ("Incrementality experiment
        design: geo-holdout, synthetic control, causal inference"), which
        dilute keyword density and split badly on commas
      - parentheses, which some ATS parsers drop, taking "(MMM)" and
        "(Looker, Tableau)" with them
    """
    skills = resume.get("skills") or []
    if not skills:
        return resume

    # parentheses go regardless of shape; promote the contents to siblings
    skills = [re.sub(r"\s*\(([^)]*)\)", r", \1", s).strip() for s in skills]

    cat_line = re.compile(r"^([^:]{3,40}):\s*(.+)$")
    # Only treat this as categorized when EVERY entry is a category line.
    # A flat list where a few entries happen to contain a colon
    # ("Unit economics: CAC, ...") is a list of skills, not categories, and
    # inferring structure from it produced three bogus headings.
    if not (len(skills) >= 3 and all(cat_line.match(s) for s in skills)):
        seen, flat = set(), []
        for s in skills:
            k = s.lower()
            if k not in seen and s:
                seen.add(k)
                flat.append(s)
        resume["skills"] = flat
        return resume

    categories = []
    for entry in skills:
        m = cat_line.match(entry)
        label = m.group(1).strip()
        terms = [t.strip(" .;") for t in m.group(2).split(",")]
        categories.append((label, [t for t in terms if t]))

    cleaned, seen = [], set()
    max_terms = _tune("max_terms_per_category")
    for label, terms in categories[:_tune("max_skill_categories")]:
        keep = []
        for t in terms:
            k = t.lower()
            if k in seen or not t:
                continue
            seen.add(k)
            keep.append(t)
            if len(keep) >= max_terms:
                break
        if keep:
            cleaned.append(f"{label}: " + ", ".join(keep))

    resume["skills"] = cleaned
    return resume


def order_experience(resume: dict, profile: dict) -> dict:
    """
    Force reverse-chronological work history.

    Left to itself the model promotes whichever role best matches the
    posting, which put a 2010-2015 employer above a 2020-2022 one on agency
    applications. Sorting against master_profile's order is more
    reliable than parsing free-text date strings, and the profile is already
    newest-first.
    """
    entries = resume.get("experience")
    if not entries:
        return resume

    canonical = [j["company"] for j in profile.get("work_history", [])]

    def match(entry):
        # generated company fields carry descriptors, e.g.
        # "Northwind Retail (Home goods/CPG, ~$1B revenue)"
        name = (entry.get("company") or "").lower()
        for i, c in enumerate(canonical):
            if c.lower() in name or name.split("(")[0].strip() in c.lower():
                return i
        return None

    def rank(entry):
        i = match(entry)
        return len(canonical) if i is None else i   # unrecognized falls to the bottom

    # The descriptor is context for writing bullets, not resume content. Left
    # alone it reaches the page as "Northwind Retail (Home goods, furniture &
    # outdoor e-commerce/CPG; ~$1B revenue, ~$300M ad spend, 12 brands)",
    # which reads as internal notes and differs resume to resume. Pin the
    # company to the profile's own spelling.
    for entry in entries:
        i = match(entry)
        if i is not None:
            entry["company"] = canonical[i]

    resume["experience"] = sorted(entries, key=rank)
    return resume


def tailor_resume(posting: dict, profile: dict) -> dict:
    stable = profile_preamble(
        "Draft a tailored resume for this candidate applying to this specific posting.",
        profile)
    prompt = f"""
JOB POSTING:
Title: {posting['title']}
Company: {posting['company']}
Description: {strip_html(posting['description_html'])[:4000]}

Guidelines:
- Mirror the language and priorities of the posting where truthful and accurate.
- {profile.get('positioning_notes', '')}
- Do not fabricate experience, employers, titles, or metrics not present in the profile.
- If the profile has PLACEHOLDER fields, leave a clear "[FILL IN: ...]" marker in that
  field rather than inventing content.
- List work history in reverse-chronological order, most recent role first.
  Never move a role up the page because it is more relevant to this posting;
  relevance is expressed through which bullets you keep and how you word
  them, not through position. The order is enforced after generation, so
  reordering here only creates a mismatch.
- Order/emphasize experience bullets WITHIN each role to match what this
  posting cares about most.
- Promotions and title changes at one employer: give each title its own
  experience entry with its own dates, newest first, one after another, and
  the company name spelled identically in each so they print stacked under
  one heading. Put most bullets under the latest title; earlier titles at
  that employer get at most {_tune("bullets_tail")} bullets. Where a role has
  "promoted_from" in the profile, the promotion is itself evidence: let the
  first bullet of the later title or the summary say so when it helps.

SKILLS SECTION. This is read by both an ATS keyword parser and a human
skimming for anchors. Format for both:
- Group into {_tune("max_skill_categories")} categories at most, each a single line
  "Category: term, term, term". Fewer, fuller categories beat many thin ones.
- Name categories after what THIS posting asks for. An ICU posting gets
  "Critical Care"; a sales leadership posting gets "Pipeline & Forecasting".
  Do not reuse a fixed set across different postings.
- Every item must be an atomic term a parser can match: "Telemetry",
  "Salesforce", "Lean Manufacturing", "SQL", "Payroll". NOT descriptive
  phrases like "Patient care: telemetry, wound care, discharge planning" --
  that is one entry pretending to be three, and it splits badly on commas.
- No parentheses anywhere in this section. Write "Electronic Health
  Records, EHR" as two terms rather than "Electronic Health Records (EHR)";
  some parsers drop the parenthetical and lose the acronym.
- No connective words. "and", "including", "with", "across" have no place
  in a term list.
- At most {_tune("max_terms_per_category")} terms per category, strongest first.
- Draw terms from the profile's skills_by_category, preferring the ones
  this posting names. Do not list a skill the candidate does not have.

Respond ONLY with JSON, no other text, matching exactly this shape:
{json.dumps(RESUME_SCHEMA_EXAMPLE, indent=2)}
"""
    # 16384, matching the cover letter: a resume is only ~800 words, but the
    # model reasons about which highlights to keep before writing any of them,
    # and a 10k-character posting pushed that past 8192 and truncated the JSON
    # mid-document. max_tokens is a ceiling, not a charge -- billing follows
    # the tokens actually produced -- so headroom here costs nothing, while
    # being short costs the whole document.
    data, _ = request_json(cached_messages(stable, prompt), 16384, "resume")
    resume = order_experience(data, profile)
    return fit_to_two_pages(normalize_skills(resume))


def _sanitize_filename_part(text: str) -> str:
    # scraped titles carry HTML entities through to the filename
    # ("Measurement &amp; Insights"); decode before stripping
    text = html.unescape(text or "")
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80].strip()


# Fixed frame, modelled on letters that have actually been sent. Only the role
# title and the grouped achievement block change per posting. These three
# paragraphs are the most personal thing in the pipeline, so they live in
# config/letter.yaml; the values below are the fallback when that file is
# absent. Edit the file, not this.
_LETTER, AI_TELL_PATTERNS = load_letter()
CL_POSITIONING = _LETTER["positioning"]
CL_LEAD_IN = _LETTER["lead_in"]
CL_CLOSING_PARA = _LETTER["closing_para"]


def _frame() -> dict:
    person = user_config.current()
    return person.letter if person else _LETTER


def _tells() -> list[tuple[str, str]]:
    person = user_config.current()
    return person.tells if person else AI_TELL_PATTERNS


def _letter_schema(frame: dict) -> dict:
    example = dict(COVER_LETTER_SCHEMA_EXAMPLE)
    example["greeting"], example["sign_off"] = frame["greeting"], frame["sign_off"]
    return example


COVER_LETTER_SCHEMA_EXAMPLE = {
    "greeting": _LETTER["greeting"],
    "opening": "Please consider my qualifications for the <exact role title> role at "
               "<company>. <One sentence naming the single most relevant thing about this "
               "candidate for THIS posting.>",
    "positioning": "<leave exactly as given in the prompt>",
    "lead_in": "<leave exactly as given in the prompt>",
    "groups": [
        {
            "header": "Theme drawn from what this posting asks for, with scope if it helps "
                      "(e.g. 'Patient Safety & Quality Improvement')",
            "bullets": [
                "A concrete achievement from the profile with its metric, tied to that theme",
                "A second piece of evidence for the same theme",
            ],
        },
        {"header": "Second theme", "bullets": ["evidence", "evidence"]},
        {"header": "Third theme", "bullets": ["evidence", "evidence"]},
    ],
    "closing_para": "<leave exactly as given in the prompt>",
    "sign_off": _LETTER["sign_off"],
    # placeholders only: both are overwritten from the profile after the
    # model returns, so no real contact details belong in this file
    "name": "<your name>",
    "contact": "<your email>\n<your phone>",
}


# AI_TELL_PATTERNS comes from load_letter() above: the built-in list, plus
# anything listed under `avoid` in config/letter.yaml. These are writing rules,
# not universal truths, so they belong with the letter frame rather than here.


# the prompt targets 210; this ceiling carries slack so the lint doesn't fail
# a good letter over a couple of words
MAX_LETTER_WORDS = _TUNING["max_letter_words"]
MAX_SENTENCE_WORDS = _TUNING["max_sentence_words"]   # nesting was the real problem


def letter_body(letter: dict) -> str:
    """
    The parts of the letter the model actually wrote.

    Excludes the fixed boilerplate (positioning, lead_in, closing_para) --
    linting text that is required to be verbatim would flag it forever.
    """
    parts = [letter.get("opening", "")]
    for g in letter.get("groups", []):
        parts.append(g.get("header", ""))
        parts.extend(g.get("bullets", []))
    parts.extend(letter.get("paragraphs", []))     # older prose-format letters
    return " ".join(p for p in parts if p)


def find_ai_tells(letter: dict) -> list[str]:
    """Scan the drafted letter body for the patterns the prompt bans."""
    body = letter_body(letter)
    found = []
    for pattern, label in _tells():
        if re.search(pattern, body, flags=re.IGNORECASE):
            found.append(label)
    return found


MAX_BULLET_WORDS = _TUNING["max_bullet_words"]   # past this a bullet reads as prose
MIN_GROUPS = _TUNING["min_groups"]

# Vague quantifiers the model reaches for when it has no real number.
INVENTED_SCOPE = re.compile(
    r"\b(dozens|scores|countless|numerous|myriad|a wide (range|variety)|"
    r"many (clients|brands|verticals|industries|companies)|"
    r"across (dozens|numerous|countless))\b", re.I)


def _publications_rule(profile: dict) -> str:
    """
    The rule about citing published writing, only when there is any.

    This line used to assert flatly that the candidate has published writing.
    For a profile without a publications list that is an instruction to use
    something that does not exist, which is the one thing the grounding block
    above it exists to prevent.
    """
    pubs = profile.get("publications")
    if not pubs:
        return ""
    return ("\n- The candidate has published writing listed in the profile. It "
            "can support a\n  bullet in one clause; do not summarize its contents.")


def find_ungrounded_claims(letter: dict, profile: dict) -> list[str]:
    """
    Catch bullets asserting things the profile does not support.

    Two failure modes seen in practice:
      1. numbers that appear nowhere in the profile (invented or derived)
      2. vague quantifiers standing in for scope ("dozens of verticals")

    A third -- lifting an industry out of company_descriptor and presenting
    it as the candidate's own client work -- is handled in the prompt, since
    detecting it reliably needs to know which descriptor a claim came from.
    """
    issues = []
    profile_text = json.dumps(profile).lower()

    # every distinct number in the profile, normalized
    prof_nums = set(re.findall(r"\d+(?:\.\d+)?", profile_text))

    for g in letter.get("groups") or []:
        for b in g.get("bullets") or []:
            if INVENTED_SCOPE.search(b):
                m = INVENTED_SCOPE.search(b)
                issues.append(f'invented scope "{m.group(0)}" in: "{b[:56]}..."')
            for num in re.findall(r"\d+(?:\.\d+)?", b):
                # years and small ordinals show up incidentally; the risk is
                # metrics, so only check numbers that carry weight
                if num in prof_nums or len(num) < 2:
                    continue
                issues.append(f'number "{num}" is not in the profile: "{b[:56]}..."')

    return issues


def _skill_terms(profile: dict) -> list[str]:
    """Skill names from the profile, without the '(Advanced)' and '[notes]'
    that load_skills_csv appends."""
    terms = []
    for items in (profile.get("skills_by_category") or {}).values():
        for item in items or []:
            name = re.sub(r"\s*[\(\[].*$", "", str(item)).strip()
            if len(name) >= 3:
                terms.append(name)
    return terms


def specific_pattern(profile: dict | None = None) -> re.Pattern:
    """
    What counts as a concrete bullet: a number, an employer the candidate
    actually worked for, or a named method.

    Both halves come from the person rather than a hardcoded list, so this
    travels with whoever is using the pipeline. Named methods are their own
    skills plus any they list in their settings; the fixed list this used to
    carry was one marketing analyst's vocabulary, and a nurse's bullet naming
    telemetry is every bit as concrete as one naming geo-holdout tests.
    """
    person = user_config.current()
    words = (person.methods if person else []) + _skill_terms(profile or {})
    METHODS = [rf"(?<!\w){re.escape(w)}(?!\w)" for w in dict.fromkeys(words)]
    names = []
    for job in (profile or {}).get("work_history", []):
        company = (job.get("company") or "").strip()
        if company:
            names.append(re.escape(company))
            first = company.split()[0]
            if len(first) > 3:                 # "Northwind" for "Northwind Retail"
                names.append(re.escape(first))
    return re.compile("|".join([r"\d", "%", r"\$"] + names + METHODS), re.I)


def find_style_issues(letter: dict, profile: dict | None = None) -> list[str]:
    """
    Structural checks for the grouped-bullet format. The old prose-shaped
    rules (total word count, sentence nesting, cadence runs) were retired
    with the prose format; the ones that survive are about bullets doing
    their job.
    """
    issues = []

    # legacy prose letters still get the old length check
    if letter.get("paragraphs") and not letter.get("groups"):
        body = " ".join(letter["paragraphs"])
        if len(body.split()) > _tune("max_letter_words"):
            issues.append(f"too long ({len(body.split())} words)")
        return issues

    groups = letter.get("groups") or []
    min_groups = _tune("min_groups")
    if len(groups) < min_groups:
        issues.append(f"only {len(groups)} achievement group(s); want {min_groups}")

    max_bullet = _tune("max_bullet_words")
    seen_headers = set()
    for g in groups:
        header = (g.get("header") or "").strip()
        bullets = [b for b in (g.get("bullets") or []) if b.strip()]

        if not header:
            issues.append("a group is missing its header")
        elif header.lower() in seen_headers:
            issues.append(f'duplicate group header: "{header[:44]}"')
        seen_headers.add(header.lower())

        if not bullets:
            issues.append(f'group "{header[:34]}" has no bullets')
        for b in bullets:
            n = len(b.split())
            if n > max_bullet:
                issues.append(f'bullet of {n} words (max {max_bullet}): "{b[:64]}..."')

    # Flag only when the block as a whole is vague. An individual bullet
    # without a number is fine ("Built the analytics practice from the ground
    # up"); a letter where most bullets have no number, employer, or named
    # method is the actual failure.
    SPECIFIC = specific_pattern(profile)
    bullets = [b for g in groups for b in (g.get("bullets") or [])]
    vague = [b for b in bullets if not SPECIFIC.search(b)]
    if bullets and len(vague) > len(bullets) / 2:
        issues.append(f"{len(vague)} of {len(bullets)} bullets carry no metric, employer, "
                      f'or named method; e.g. "{vague[0][:58]}..."')

    return issues


def draft_cover_letter(posting: dict, profile: dict) -> dict:
    frame = _frame()
    stable = profile_preamble(
        "Draft a cover letter for this candidate applying to this specific posting.",
        profile)
    prompt = f"""
JOB POSTING:
Title: {posting['title']}
Company: {posting['company']}
Description: {strip_html(posting['description_html'])[:4000]}

FORMAT. This is a fixed template that has worked in real applications. Most
of it is boilerplate that must be reproduced verbatim. Your job is almost
entirely the grouped achievement block in the middle.

Reproduce these EXACTLY, word for word, in the fields named:
  positioning  = "{frame["positioning"]}"
  lead_in      = "{frame["lead_in"]}"
  closing_para = "{frame["closing_para"]}"

Write only two things:

1. OPENING (2 sentences). First sentence: "Please consider my
   qualifications for the <exact role title> role at <company>." Use the
   posting's exact title and the company's own branding of its name.
   Second sentence: the single most relevant fact about the candidate for
   THIS posting, stated concretely. Not "I am a strong fit" but the specific
   thing that makes them one. Keep it to one sentence.

2. THREE GROUPS. Each has a short header naming a capability this posting
   actually asks for, and 2 or 3 bullets of evidence beneath it.
   - Derive the headers from the posting's own requirements. If it asks for
     budget ownership and forecasting, one header is about that. Do not reuse generic
     headers across different postings.
   - Add scope to a header when it strengthens it, e.g.
     "Survey Design & Research Methodology (12+ years)".
   - Every bullet is a concrete achievement from the profile, with its
     metric where one exists. A bullet with no specific in it is wasted.
   - Order the groups so the one this posting cares about most comes first.
   - Bullets are fragments or single sentences, not paragraphs. Aim for 15
     to 35 words each.
   - Do not repeat the same achievement in two groups.
   - If the posting raises an obvious concern (seniority mismatch, industry
     change), use one bullet to address it with evidence rather than
     ignoring it.

CRITICAL -- this must not read as AI-written. Hiring managers screen for
these patterns and they are an instant credibility hit. Hard rules:
- NEVER use an em dash (--- or the character). Use a period, comma, or
  parenthesis. Do not substitute a spaced hyphen as a workaround.
- NEVER use the "not just X, but Y" / "it's not about X, it's about Y" /
  "X isn't just Y" construction. Not once. This includes the inverted form,
  "<good thing>, not <strawman thing>" (e.g. "frameworks that hold up, not
  dashboards that fall apart") and the "X rather than <strawman>" form
  ("building practices inside agencies rather than bolting them on from
  outside" -- nobody describes their own work as bolting it on). State what
  is true and stop. A contrast is only allowed when the alternative is a
  real, common failure mode someone would admit to ("client teams use the
  output rather than filing it away" is fine, because filing reports away
  genuinely happens).
- Every clause must add information. Cut qualifiers that only add texture:
  "most recently from a standing start" says nothing the next sentence
  doesn't already say better. If removing a phrase loses no fact, remove it.
- Every clause must add information. Cut qualifiers that only add texture.
  If removing a phrase loses no fact, remove it. This matters more in
  bullets than anywhere: a bullet is read in about a second.
- Plain verbs over elevated ones ("built", not "spearheaded"; "ran", not
  "orchestrated"). No "I'm drawn to", "resonates", "excited by the
  opportunity to", "passionate about", "at the intersection of".
- Bullets lead with the achievement, not with throat-clearing. Write
  "Cut patient falls 30%..." rather than "I have experience with fall
  prevention, where I cut...".

Guidelines:
- The company name above may come from an applicant-tracking-system token
  and can be lowercase or run-together ("northwind", "beck-and-rowe"). Write
  it the way the company brands itself ("Northwind", "Beck & Rowe"). Getting
  a prospective employer's own name wrong reads as careless.
GROUNDING -- read this carefully, it is the most important rule here.

Not every field in the profile is a claim the candidate can make about
themselves.

  work_history[].highlights   = things the candidate personally did. THE
                                ONLY source for bullets. Every bullet must
                                trace to one of these.
  work_history[].company_descriptor = what the EMPLOYER's business was, its
                                revenue, and the markets THE COMPANY served.
                                This is background so you understand the
                                setting. It is NOT a list of the candidate's
                                clients or their personal scope. If an
                                employer served travel and fashion clients,
                                that does not mean the candidate worked on
                                them. Their accounts are whatever the
                                highlights actually name.
  work_history[].scope        = team size and reporting line. Usable, but
                                state it as written, not inflated.
  skills_by_category          = capabilities, not accomplishments. A skill
                                does not become an achievement bullet.

Hard rules that follow from this:
- Never move an industry, client, vertical, or market from a
  company_descriptor into a bullet as something the candidate did.
- Never invent a quantifier. No "dozens of verticals", "numerous clients",
  "many brands" unless that exact scope appears in a highlight. If the
  profile says 12 brands, write 12 brands. If it gives no number, give no
  number.
- Every metric (percentage, dollar figure, count, time span) must appear in
  the profile. Do not derive, round, combine, or estimate one.
- If a posting asks for something the candidate has not done, leave it out.
  A shorter letter is better than an inaccurate one.
- Draw only on achievements and metrics present in the profile. Do not
  fabricate experience, employers, titles, or numbers.
- {profile.get('positioning_notes', '')}{_publications_rule(profile)}
- If the profile has PLACEHOLDER fields, leave a clear "[FILL IN: ...]"
  marker rather than inventing content.

Respond ONLY with JSON, no other text, matching exactly this shape:
{json.dumps(_letter_schema(frame), indent=2)}
"""
    # The style loop below appends turns to this; the cached first block stays
    # byte-identical, so the corrective pass reads the cache rather than
    # re-sending the profile.
    messages = cached_messages(stable, prompt)
    letter = None

    # The style rules above are hard constraints, and prompt adherence alone
    # isn't reliable for them -- so check the draft and give one corrective
    # pass naming the specific violations before accepting it.
    for attempt in range(2):
        # 16384: the style and structure constraints make the model think at
        # length before writing. At 4096 the thinking block consumed the whole
        # budget and returned no text; at 8192 a letter was still truncated
        # mid-JSON on the second (corrective) pass, which carries the extra
        # context of the rejected draft.
        letter, text = request_json(messages, 16384, "cover letter")
        # The boilerplate is fixed. Overwrite rather than trusting the model
        # to reproduce it verbatim; it paraphrases otherwise.
        letter["positioning"] = frame["positioning"]
        letter["lead_in"] = frame["lead_in"]
        letter["closing_para"] = frame["closing_para"]
        letter.setdefault("greeting", frame["greeting"])
        letter.setdefault("sign_off", frame["sign_off"])
        letter["name"] = profile.get("name", "")
        letter["contact"] = f"{profile.get('email','')}\n{profile.get('phone','')}"

        problems = (find_ungrounded_claims(letter, profile)
                    + find_ai_tells(letter) + find_style_issues(letter, profile))
        if not problems:
            return letter
        if attempt == 0:
            print(f"    [style] rewriting cover letter, found: {'; '.join(problems)}")
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                    "This draft has these problems: " + "; ".join(problems) + ". "
                    "Rewrite fixing them, but do not overcorrect into choppy, "
                    "uniform sentences -- that is worse than the original "
                    "problem. Untangle nested clauses rather than simply "
                    "shortening everything. Cut whole sentences to hit the word "
                    "count instead of trimming words from every sentence. Keep "
                    "the strongest concrete achievements, drop the weakest, and "
                    "let the writing breathe. Respond ONLY with the same JSON "
                    "shape."},
            ]

    remaining = (find_ungrounded_claims(letter, profile)
                 + find_ai_tells(letter) + find_style_issues(letter, profile))
    print(f"    [style] warning: cover letter still has: {'; '.join(remaining)} "
          f"-- review before sending")
    return letter


def resume_filename_base(company: str, title: str, output_dir: Path = OUTPUT_DIR) -> str:
    """
    company_title_YYYY-MM-DD, with a -2/-3/... suffix if that exact name is
    already taken (e.g. the same company posts the identical title twice)
    so results never silently overwrite each other.
    """
    base = f"{_sanitize_filename_part(company)}_{_sanitize_filename_part(title)}_{date.today().isoformat()}"
    candidate = base
    n = 2
    while (output_dir / f"{candidate}.json").exists() or (output_dir / f"{candidate}.docx").exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def run(postings: list[dict]):
    """
    Scores/tailors only postings not already in scored_postings.json (matched
    by url, same as score_batch.py) so re-running the pipeline after new
    postings show up doesn't re-spend API calls -- or generate a fresh
    resume -- for ones already processed. Returns only the newly-processed
    entries; scored_postings.json on disk holds the full merged history.
    """
    require_api_key()
    profile = load_profile()
    OUTPUT_DIR.mkdir(exist_ok=True)
    scored_path = OUTPUT_DIR / "scored_postings.json"

    previous = json.loads(scored_path.read_text(encoding="utf-8")) if scored_path.exists() else []
    seen_urls = {r["url"] for r in previous if "url" in r}
    merged = {r["posting_id"]: r for r in previous}

    new_postings = [p for p in postings if p["url"] not in seen_urls]
    skipped = len(postings) - len(new_postings)
    if skipped:
        print(f"Skipping {skipped} already-scored posting(s)")

    def save():
        scored_path.write_text(json.dumps(list(merged.values()), indent=2), encoding="utf-8")

    new_results, failures = [], []
    for posting in new_postings:
        # One bad API response used to abort the whole run and discard every
        # posting scored before it, because the save happened only at the end.
        # Isolate each posting and checkpoint after each one instead.
        try:
            eval_result = score_posting(posting, profile)
        except Exception as e:
            if is_auth_error(e):
                raise SystemExit(
                    f"[FATAL] the API rejected authentication: {type(e).__name__}: {str(e)[:120]}\n"
                    "        Stopping. Every remaining posting would fail the same way.\n"
                    "        Check ANTHROPIC_API_KEY, then re-run."
                ) from None
            print(f"[skip] scoring failed for {posting['title'][:44]} @ {posting['company']}: "
                  f"{type(e).__name__}: {str(e)[:90]}")
            failures.append((posting, "scoring", str(e)[:120]))
            continue

        entry = {**posting, **eval_result}
        print(f"[{entry['score']}/10] {posting['title']} @ {posting['company']}"
              f"{' (overqualification risk)' if entry.get('overqualification_risk') else ''}")

        if eval_result["score"] >= SCORE_THRESHOLD:
            base = resume_filename_base(posting["company"], posting["title"])
            try:
                resume_data = tailor_resume(posting, profile)
                json_path = OUTPUT_DIR / f"{base}.json"
                json_path.write_text(json.dumps(resume_data, indent=2), encoding="utf-8")
                entry["tailored_resume_json"] = str(json_path)
                entry["tailored_resume_docx"] = str(OUTPUT_DIR / f"{base}.docx")
                print(f"    -> resume drafted: {json_path.name} "
                      f"(run render_resume.js on it to produce the .docx)")
            except Exception as e:
                print(f"    [warn] resume failed: {type(e).__name__}: {str(e)[:90]}")
                failures.append((posting, "resume", str(e)[:120]))

            try:
                cover_data = draft_cover_letter(posting, profile)
                cover_json_path = OUTPUT_DIR / f"{base}_cover.json"
                cover_json_path.write_text(json.dumps(cover_data, indent=2), encoding="utf-8")
                entry["cover_letter_json"] = str(cover_json_path)
                entry["cover_letter_docx"] = str(OUTPUT_DIR / f"{base}_cover.docx")
                print(f"    -> cover letter drafted: {cover_json_path.name}")
            except Exception as e:
                print(f"    [warn] cover letter failed: {type(e).__name__}: {str(e)[:90]}")
                failures.append((posting, "cover letter", str(e)[:120]))

        merged[entry["posting_id"]] = entry
        new_results.append(entry)
        save()          # checkpoint, so a later crash can't undo this posting

    save()
    summary = spend_summary()
    if summary:
        print(f"\n{summary}")
    if failures:
        print(f"\n  {len(failures)} step(s) failed and were skipped:")
        for posting, stage, err in failures:
            print(f"    {posting['company']} - {posting['title'][:40]} [{stage}]: {err}")
        print("  Run `python score_and_tailor.py --repair` to retry the missing "
              "documents without re-scoring.")
    return new_results


def repair():
    """
    Fill in documents missing from already-scored postings.

    A posting whose resume succeeded but whose cover letter failed is still
    written to scored_postings.json, so the normal run skips it on the next
    pass as already-scored. This retries just the missing pieces.
    """
    require_api_key()
    profile = load_profile()
    scored_path = OUTPUT_DIR / "scored_postings.json"
    data = json.loads(scored_path.read_text(encoding="utf-8"))

    todo = [e for e in data
            if e.get("score", 0) >= SCORE_THRESHOLD
            and (not e.get("tailored_resume_json") or not e.get("cover_letter_json"))]
    if not todo:
        print("Nothing to repair: every qualifying posting has both documents.")
        return []

    print(f"Repairing {len(todo)} posting(s) with missing documents\n")
    fixed = []
    for entry in todo:
        label = f"{entry['company']} - {entry['title'][:44]}"
        # Name the repaired document after whichever half already exists, so
        # the pair keeps matching filenames. Falling back to today's date
        # dated a repaired resume a day later than its own cover letter.
        if entry.get("tailored_resume_json"):
            base = Path(entry["tailored_resume_json"]).stem
        elif entry.get("cover_letter_json"):
            base = re.sub(r"_cover$", "", Path(entry["cover_letter_json"]).stem)
        else:
            base = resume_filename_base(entry["company"], entry["title"])
        print(f"[{entry['score']}/10] {label}")

        if not entry.get("tailored_resume_json"):
            try:
                d = tailor_resume(entry, profile)
                p = OUTPUT_DIR / f"{base}.json"
                p.write_text(json.dumps(d, indent=2), encoding="utf-8")
                entry["tailored_resume_json"] = str(p)
                entry["tailored_resume_docx"] = str(OUTPUT_DIR / f"{base}.docx")
                print(f"    -> resume drafted: {p.name}")
            except Exception as e:
                print(f"    [warn] resume still failing: {type(e).__name__}: {str(e)[:90]}")

        if not entry.get("cover_letter_json"):
            try:
                d = draft_cover_letter(entry, profile)
                p = OUTPUT_DIR / f"{base}_cover.json"
                p.write_text(json.dumps(d, indent=2), encoding="utf-8")
                entry["cover_letter_json"] = str(p)
                entry["cover_letter_docx"] = str(OUTPUT_DIR / f"{base}_cover.docx")
                print(f"    -> cover letter drafted: {p.name}")
            except Exception as e:
                print(f"    [warn] cover letter still failing: {type(e).__name__}: {str(e)[:90]}")

        scored_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        fixed.append(entry)
    return fixed


if __name__ == "__main__":
    import sys as _sys
    if "--repair" in _sys.argv:
        repair()
    else:
        from scraper import collect_all_postings
        run(collect_all_postings())
