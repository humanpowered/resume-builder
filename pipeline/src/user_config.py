"""
One person's settings, for a pipeline that serves more than one person.

Run from the command line, the pipeline reads config/ the way it always has.
Run inside the hosted product, each request carries the signed-in person's
settings instead, held in their own row of the database:

    cfg = UserConfig.from_dict(stored)       # ConfigError if anything is wrong
    with using(cfg):
        result = score_posting(posting, profile)

Inside `using`, every limit, the letter frame, the search terms and the
target companies come from that person. Outside it, nothing changes.

The settings that belong to a person are the ones that describe them: the pay
at which a role stops looking junior, how much room each job gets on two
pages, their letter's fixed paragraphs, the words they would never write,
what they search for and which companies they want. What belongs to whoever
runs the service (API keys, paid scraping sources, the email inbox) stays in
config/boards.yaml and is never taken from a person's settings.
"""
import contextvars
import re
from contextlib import contextmanager
from dataclasses import dataclass, field

from settings import (AI_TELL_DEFAULTS, LETTER_DEFAULTS, TUNING_DEFAULTS, ConfigError,
                      letter_from, tuning_from)

# Company lists a person may set, by applicant-tracking system. Each is a list
# of board tokens, except workday, which needs three parts to find a board.
BOARD_LISTS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable")
WORKDAY_PARTS = ("tenant", "host", "site")
SECTIONS = ("tuning", "letter", "titles", "boards", "methods")
# Tuning that belongs to whoever runs the service: which model it pays for,
# and where the command line's morning brief is written.
OPERATOR_TUNING = ("model", "brief_path")

# A person's settings can be pasted from anywhere; these keep one person from
# making the service do unbounded work on their behalf.
MAX_TERMS = 200
MAX_COMPANIES = 500


@dataclass
class UserConfig:
    tuning: dict = field(default_factory=lambda: dict(TUNING_DEFAULTS))
    letter: dict = field(default_factory=lambda: dict(LETTER_DEFAULTS))
    tells: list = field(default_factory=lambda: list(AI_TELL_DEFAULTS))
    include_titles: list = field(default_factory=list)
    exclude_titles: list = field(default_factory=list)
    boards: dict = field(default_factory=dict)
    # Named methods that make a letter bullet concrete in this person's field
    # ("telemetry", "lean"), on top of the skills in their own record.
    methods: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict | None) -> "UserConfig":
        """Validate stored settings. Anything not given takes the default;
        anything given and wrong raises ConfigError naming the section."""
        data = data or {}
        if not isinstance(data, dict):
            raise ConfigError("settings must be a mapping")
        unknown = sorted(set(data) - set(SECTIONS))
        if unknown:
            raise ConfigError(f"unknown section(s): {', '.join(unknown)}. "
                              f"Valid: {', '.join(SECTIONS)}")
        cfg = cls(raw=data)
        tuning = data.get("tuning") or {}
        fixed = sorted(set(tuning) & set(OPERATOR_TUNING)) if isinstance(tuning, dict) else []
        if fixed:
            raise ConfigError(f"tuning: {', '.join(fixed)} is set by the service, not per person")
        try:
            cfg.tuning = tuning_from(tuning)
        except ConfigError as exc:
            raise ConfigError(f"tuning: {exc}")
        try:
            cfg.letter, cfg.tells = letter_from(data.get("letter") or {})
        except ConfigError as exc:
            raise ConfigError(f"letter: {exc}")
        cfg.include_titles, cfg.exclude_titles = _titles(data.get("titles") or {})
        cfg.boards = _boards(data.get("boards") or {})
        cfg.methods = _terms(data.get("methods") or [], "methods")
        return cfg

    def to_dict(self) -> dict:
        """What to store: exactly what the person set, so a default that
        improves later reaches them instead of being frozen in their row."""
        return dict(self.raw)


def _terms(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise ConfigError(f"{where} must be a list of words or phrases")
    out = [t.strip() for t in value if t.strip()]
    if len(out) > MAX_TERMS:
        raise ConfigError(f"{where} has {len(out)} entries; the limit is {MAX_TERMS}")
    return out


def _titles(value) -> tuple[list[str], list[str]]:
    if not isinstance(value, dict) or set(value) - {"include", "exclude"}:
        raise ConfigError("titles must have 'include' and/or 'exclude' lists")
    return (_terms(value.get("include") or [], "titles.include"),
            _terms(value.get("exclude") or [], "titles.exclude"))


def _boards(value) -> dict:
    allowed = set(BOARD_LISTS) | {"workday", "exclude_companies", "locations"}
    if not isinstance(value, dict):
        raise ConfigError("boards must be a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"boards: {', '.join(unknown)} cannot be set per person. "
                          f"Valid: {', '.join(sorted(allowed))}")
    out, count = {}, 0
    for key in (*BOARD_LISTS, "exclude_companies", "locations"):
        if key in value:
            out[key] = _terms(value[key] or [], f"boards.{key}")
            if key in BOARD_LISTS:
                for token in out[key]:
                    # a token ends up in a URL path; nothing but a slug belongs there
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", token):
                        raise ConfigError(f"boards.{key}: {token!r} is not a board token")
                count += len(out[key])
    if "workday" in value:
        boards = value["workday"] or []
        if not isinstance(boards, list):
            raise ConfigError("boards.workday must be a list")
        for wd in boards:
            if not (isinstance(wd, dict) and all(isinstance(wd.get(k), str) and wd[k].strip()
                                                 for k in WORKDAY_PARTS)):
                raise ConfigError("each boards.workday entry needs tenant, host and site")
            if not re.fullmatch(r"[a-z0-9-]+\.myworkdayjobs\.com", wd["host"].strip()):
                raise ConfigError(f"boards.workday: {wd['host']!r} is not a Workday host")
        out["workday"] = [{k: wd[k].strip() for k in WORKDAY_PARTS} for wd in boards]
        count += len(boards)
    if count > MAX_COMPANIES:
        raise ConfigError(f"boards lists {count} companies; the limit is {MAX_COMPANIES}")
    return out


# --------------------------------------------------------------------------
# The person this run is for

_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("user_config", default=None)


def current() -> UserConfig | None:
    """The settings of the person this run is for, or None when the run is
    the command line reading config/."""
    return _ACTIVE.get()


@contextmanager
def using(cfg: UserConfig):
    """Run the enclosed pipeline calls with one person's settings. A context
    variable rather than a global, so two requests served at once each see
    their own person."""
    token = _ACTIVE.set(cfg)
    try:
        yield cfg
    finally:
        _ACTIVE.reset(token)
