"""
User-editable configuration, loaded from config/ instead of living in code.

Three files, each optional. When one is absent the built-in defaults apply, so
an existing checkout keeps working untouched; when one is present but wrong,
loading stops with a message naming the file and the problem. Silent fallback
is the failure mode this project keeps getting bitten by: a config that is
quietly ignored looks identical to one that works.

  config/titles.csv    what to search for, and what to exclude
  config/letter.yaml   the fixed cover-letter frame and the style rules
  config/tuning.yaml   thresholds, limits, and the model

Nothing here reads the profile; that stays in profile/.
"""
import csv
import sys
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).parent.parent / "config"
TITLES_CSV = CONFIG_DIR / "titles.csv"
LETTER_YAML = CONFIG_DIR / "letter.yaml"
TUNING_YAML = CONFIG_DIR / "tuning.yaml"


class ConfigError(ValueError):
    """A setting that cannot be used. The file loaders turn it into a stop
    naming the file; the hosted product turns it into a message to the user."""


def _fail(path: Path, problem: str) -> None:
    raise SystemExit(
        f"[FATAL] {path.name} could not be used: {problem}\n"
        f"        File: {path}\n"
        f"        Fix it or delete it to fall back to the built-in defaults."
    )


# --- tuning -----------------------------------------------------------------

TUNING_DEFAULTS = {
    "model": "claude-sonnet-5",
    "score_threshold": 6,          # only tailor documents at or above this
    # At or above, overqualification is not held against a role. None means no
    # floor: whether someone is overqualified is judged on the work alone.
    "salary_floor": None,
    "max_resume_words": 900,
    "max_summary_words": 80,
    "bullets_by_position": [5, 5, 4, 3, 3],    # per role, newest first
    "bullets_tail": 2,             # roles beyond the list above
    "max_skill_categories": 5,
    "max_terms_per_category": 10,
    "max_letter_words": 265,
    "max_sentence_words": 38,
    "max_bullet_words": 42,
    "min_groups": 3,
    # Where the morning brief is written, relative to the project root. The
    # default keeps it inside the project, because anything above the root
    # writes a stray file into the parent of a clone. Set it to something like
    # "../MORNING_BRIEF.md" if you would rather read it somewhere else -- moving
    # this output without saying so loudly leaves a stale copy at the old path
    # that looks current, which is worse than no brief at all.
    "brief_path": "MORNING_BRIEF.md",
}
_INT_KEYS = {k for k, v in TUNING_DEFAULTS.items() if isinstance(v, int)}
_OPTIONAL_INT_KEYS = {"salary_floor"}


def tuning_from(raw) -> dict:
    """Defaults overlaid with `raw`, or ConfigError saying what is wrong."""
    values = dict(TUNING_DEFAULTS)
    if not isinstance(raw, dict):
        raise ConfigError("expected a mapping of setting: value")

    unknown = sorted(set(raw) - set(TUNING_DEFAULTS))
    if unknown:
        # a typo'd key that is silently ignored is worse than a hard stop
        raise ConfigError(f"unknown setting(s): {', '.join(unknown)}. "
                          f"Valid keys: {', '.join(sorted(TUNING_DEFAULTS))}")

    for key, value in raw.items():
        if key == "bullets_by_position":
            if not (isinstance(value, list) and value
                    and all(isinstance(n, int) and n > 0 for n in value)):
                raise ConfigError("bullets_by_position must be a list of positive whole numbers")
        elif key in _OPTIONAL_INT_KEYS:
            if value in (None, 0):
                value = None
            elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"{key} must be a whole number, or empty for none, "
                                  f"got {value!r}")
        elif key in _INT_KEYS:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{key} must be a positive whole number, got {value!r}")
        elif not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{key} must be a non-empty string, got {value!r}")
        values[key] = value

    if not 0 <= values["score_threshold"] <= 10:
        raise ConfigError("score_threshold must be between 0 and 10")
    return values


def load_tuning() -> dict:
    if not TUNING_YAML.exists():
        return dict(TUNING_DEFAULTS)
    try:
        raw = yaml.safe_load(TUNING_YAML.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        _fail(TUNING_YAML, f"not valid YAML ({str(exc)[:120]})")
    try:
        return tuning_from(raw)
    except ConfigError as exc:
        _fail(TUNING_YAML, str(exc))


# --- cover letter frame and style rules -------------------------------------

# Placeholders, not anyone's voice. The real paragraphs belong in
# config/letter.yaml; shipping a specific person's positioning as the default
# would put their words in someone else's letter, and doctor.py warns when
# these are still in use.
PLACEHOLDER_POSITIONING = ("[FILL IN: one sentence saying who you are and what you build. "
                           "Rewrite frame.positioning in config/letter.yaml.]")
LETTER_DEFAULTS = {
    "positioning": PLACEHOLDER_POSITIONING,
    "lead_in": "Select highlights of my career contributions and achievements thus far include:",
    "closing_para": ("For a more detailed illustration of my skills and experience, please see "
                     "my resume. I would welcome the chance to discuss how my background fits "
                     "what you are building."),
    "greeting": "Dear Hiring Committee:",
    "sign_off": "Sincerely,",
}

# (regex, what to call it when the lint reports it)
AI_TELL_DEFAULTS = [
    (r"[—–]", "em/en dash"),
    (r"\bnot just\b", '"not just"'),
    (r"\bisn't just\b", '"isn\'t just"'),
    (r"\bit's not about\b", '"it\'s not about"'),
    # the ", not <contrasting noun phrase>" flourish -- same rhetorical move
    # as "not just X but Y", just inverted
    (r",\s+not\s+(?!only\b)\w+", '", not X" contrast flourish'),
    (r"\brather than a stretch\b", '"rather than a stretch"'),
    (r"\bresonate", '"resonate"'),
    (r"\bdrawn to\b", '"drawn to"'),
    (r"\bpassionate about\b", '"passionate about"'),
    (r"\bat the intersection of\b", '"at the intersection of"'),
    (r"\bexcited by the opportunity\b", '"excited by the opportunity"'),
    (r"\bwhat excites me most\b", '"what excites me most"'),
    (r"\bexactly (?:the|that|this) kind of\b", '"exactly that kind of"'),
    (r"\bcaught my attention for a\b", '"caught my attention for a..."'),
    (r"\bstruck a chord\b", '"struck a chord"'),
    (r"\bspearhead", '"spearhead"'),
    # Filler intensifiers: these add emphasis, never information.
    (r"\bactually\b", 'filler "actually"'),
    (r"\btruly\b", 'filler "truly"'),
    (r"\breally\b", 'filler "really"'),
    (r"\bincredibly\b", 'filler "incredibly"'),
]


def letter_from(raw) -> tuple[dict, list[tuple[str, str]]]:
    """(frame, ai_tell_patterns) from a mapping shaped like letter.yaml, or
    ConfigError saying what is wrong."""
    import re as _re
    frame = dict(LETTER_DEFAULTS)
    tells = list(AI_TELL_DEFAULTS)
    if not isinstance(raw, dict):
        raise ConfigError("expected a mapping with 'frame' and/or 'avoid' keys")

    unknown = sorted(set(raw) - {"frame", "avoid", "keep_defaults"})
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}. "
                          f"Valid sections: frame, avoid, keep_defaults")

    for key, value in (raw.get("frame") or {}).items():
        if key not in LETTER_DEFAULTS:
            raise ConfigError(f"unknown frame field {key!r}. "
                              f"Valid: {', '.join(sorted(LETTER_DEFAULTS))}")
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"frame.{key} must be a non-empty string")
        frame[key] = value.strip()

    avoid = raw.get("avoid")
    if avoid is not None:
        if not isinstance(avoid, list):
            raise ConfigError("avoid must be a list of phrases or {pattern, label} entries")
        # keep_defaults: false replaces the built-in list instead of adding to it
        if raw.get("keep_defaults", True) is False:
            tells = []
        for item in avoid:
            if isinstance(item, str):
                pattern, label = _re.escape(item), f'"{item}"'
            elif isinstance(item, dict) and item.get("pattern"):
                pattern = str(item["pattern"])
                label = str(item.get("label") or item["pattern"])
            else:
                raise ConfigError(f"avoid entry {item!r} must be a phrase or "
                                  f"{{pattern: ..., label: ...}}")
            try:
                _re.compile(pattern)
            except _re.error as exc:
                raise ConfigError(f"avoid pattern {pattern!r} is not a valid regex ({exc})")
            tells.append((pattern, label))

    return frame, tells


def load_letter() -> tuple[dict, list[tuple[str, str]]]:
    """Returns (frame, ai_tell_patterns)."""
    if not LETTER_YAML.exists():
        return dict(LETTER_DEFAULTS), list(AI_TELL_DEFAULTS)
    try:
        raw = yaml.safe_load(LETTER_YAML.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        _fail(LETTER_YAML, f"not valid YAML ({str(exc)[:120]})")
    try:
        return letter_from(raw)
    except ConfigError as exc:
        _fail(LETTER_YAML, str(exc))


# --- what to search for -----------------------------------------------------

def load_titles() -> tuple[list[str], list[str]] | None:
    """
    (include, exclude) from config/titles.csv, or None when there is no file.

    A spreadsheet rather than a YAML list because these change weekly: a row
    can be switched off without losing it, and the notes column records why a
    term is there. Columns: term, mode, active, notes.
    """
    if not TITLES_CSV.exists():
        return None

    with open(TITLES_CSV, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        _fail(TITLES_CSV, "no rows. Delete the file to use boards.yaml instead.")

    have = {(c or "").strip().lower() for c in rows[0]}
    missing = {"term", "mode"} - have
    if missing:
        _fail(TITLES_CSV, f"missing column(s): {', '.join(sorted(missing))}. "
                          f"Expected: term, mode, active, notes")

    include, exclude, skipped = [], [], 0
    for i, row in enumerate(rows, start=2):
        term = (row.get("term") or "").strip()
        if not term:
            continue
        active = (row.get("active") or "yes").strip().lower()
        if active in ("no", "false", "0", "off"):
            skipped += 1
            continue
        mode = (row.get("mode") or "").strip().lower()
        if mode in ("include", "in", "keyword", ""):
            include.append(term)
        elif mode in ("exclude", "ex", "out"):
            exclude.append(term)
        else:
            _fail(TITLES_CSV, f"row {i}: mode must be 'include' or 'exclude', got {mode!r}")

    if not include:
        _fail(TITLES_CSV, "no active include rows, so nothing would ever match")
    print(f"  loaded {len(include)} search term(s) and {len(exclude)} exclusion(s) "
          f"from titles.csv"
          + (f" ({skipped} inactive)" if skipped else ""))
    return include, exclude
