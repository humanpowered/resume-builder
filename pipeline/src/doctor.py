"""
Checks the setup and reports what works, what is missing, and what that costs
you. Run it after cloning, after changing config, or when a nightly run did
something surprising.

Makes no API calls and writes nothing. The optional dry run scrapes the free
sources so you can see how many postings survive your filters before spending
anything on scoring.

  python doctor.py                 # configuration and credentials only
  python doctor.py --dry-run       # also scrape the free sources and count
  python doctor.py --dry-run --include-paid   # also run the paid LinkedIn/Indeed actors

Exit code is 1 when something is actually broken, so a scheduled wrapper can
tell "misconfigured" from "nothing to do".
"""
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).parent
ROOT = SRC.parent
CONFIG = ROOT / "config"
PROFILE = ROOT / "profile"
OUTPUT = ROOT / "output"
LOGS = ROOT / "logs"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "
_problems = {"fail": 0, "warn": 0}


def line(status: str, what: str, detail: str = "") -> None:
    if status is FAIL:
        _problems["fail"] += 1
    elif status is WARN:
        _problems["warn"] += 1
    print(f"[{status}] {what}" + (f"  —  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# --- runtime ----------------------------------------------------------------

def check_runtime() -> None:
    section("Runtime")
    v = sys.version_info
    if v >= (3, 10):
        line(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        line(FAIL, f"Python {v.major}.{v.minor}", "3.10 or newer is required")

    for module, why in (("anthropic", "scoring and drafting"),
                        ("requests", "every job source"),
                        ("yaml", "reading config")):
        try:
            __import__(module)
            line(OK, f"{module} installed")
        except ImportError:
            line(FAIL, f"{module} missing", f"needed for {why}: pip install -r requirements.txt")

    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=20)
        line(OK, f"Node {out.stdout.strip()}", "renders the .docx files")
    except Exception:
        line(WARN, "Node not found", "scoring still works; .docx rendering does not")
    if (SRC / "node_modules" / "docx").is_dir():
        line(OK, "docx package installed")
    else:
        line(WARN, "docx package missing", "run: cd src && npm install")


# --- credentials ------------------------------------------------------------

def user_env(name: str) -> str:
    """
    Read a Windows user environment variable, ignoring this process.

    Some parents deliberately strip a variable from the environment they hand
    to child processes -- Claude Code removes ANTHROPIC_API_KEY, for instance --
    so os.environ says missing while the scheduled nightly run, which starts
    from the user environment, has it. Checking the registry is what tells a
    key that was never set apart from one this shell simply cannot see.
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value or "")
    except (OSError, ImportError):
        return ""


def check_credentials() -> dict:
    section("Credentials")
    have = {}

    anthropic_ok = bool(os.environ.get("ANTHROPIC_API_KEY")
                        or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if not anthropic_ok and user_env("ANTHROPIC_API_KEY"):
        anthropic_ok = True
        line(OK, "ANTHROPIC_API_KEY set in your user environment",
             "not visible to this shell, which strips it; the nightly run gets it")
    if not anthropic_ok:
        cfg_dir = os.environ.get("ANTHROPIC_CONFIG_DIR")
        base = (Path(cfg_dir) if cfg_dir
                else Path(os.environ.get("APPDATA", "")) / "Anthropic" if os.name == "nt"
                else Path.home() / ".config" / "anthropic")
        creds = base / "credentials"
        anthropic_ok = creds.is_dir() and any(creds.glob("*.json"))
        if anthropic_ok:
            line(OK, "Anthropic sign-in", "OAuth profile from `ant auth login`")
    if anthropic_ok and os.environ.get("ANTHROPIC_API_KEY"):
        line(OK, "ANTHROPIC_API_KEY set")
    elif not anthropic_ok:
        line(FAIL, "No Anthropic credential", "nothing can be scored or drafted")
    have["anthropic"] = anthropic_ok

    optional = {
        "jooble": (["JOOBLE_API_KEY"], "Jooble and the company watchlist"),
        "adzuna": (["ADZUNA_APP_ID", "ADZUNA_APP_KEY"], "the Adzuna source"),
        "apify": (["APIFY_TOKEN"], "LinkedIn and Indeed (paid, billed per result)"),
        "imap": (["IMAP_USER", "IMAP_APP_PASSWORD"], "the mailbox source and reply checking"),
    }
    for name, (vars_, what) in optional.items():
        missing = [v for v in vars_ if not os.environ.get(v)]
        # a variable this shell cannot see may still be set for the user, and
        # the nightly run would find it -- say which case it is
        elsewhere = [v for v in missing if user_env(v)]
        absent = [v for v in missing if v not in elsewhere]
        have[name] = not missing
        if absent:
            line(WARN, f"{', '.join(absent)} not set", f"{what} will be skipped")
        elif elsewhere:
            line(OK, f"{name} credentials set in your user environment",
                 "not visible to this shell; the nightly run gets them")
        else:
            line(OK, f"{name} credentials set", what)
    return have


# --- configuration ----------------------------------------------------------

def check_config() -> dict:
    section("Configuration")
    info = {"titles": 0, "excludes": 0, "sources": [], "threshold": None}
    sys.path.insert(0, str(SRC))

    try:
        import yaml
        cfg = yaml.safe_load((CONFIG / "boards.yaml").read_text(encoding="utf-8")) or {}
        line(OK, "boards.yaml parses")
    except FileNotFoundError:
        line(FAIL, "boards.yaml missing", "copy boards.example.yaml to boards.yaml")
        return info
    except Exception as exc:
        line(FAIL, "boards.yaml is not valid YAML", str(exc)[:90])
        return info

    try:
        import settings
        tuning = settings.load_tuning()
        info["threshold"] = tuning["score_threshold"]
        line(OK, "tuning.yaml" if settings.TUNING_YAML.exists() else "tuning defaults",
             f"model {tuning['model']}, draft at {tuning['score_threshold']}+, "
             f"floor ${tuning['salary_floor']:,}")

        frame, tells = settings.load_letter()
        line(OK, "letter.yaml" if settings.LETTER_YAML.exists() else "letter defaults",
             f"{len(tells)} phrases the lint rejects")
        # By marker, not by equality. This compared against the exact default
        # string, and letter.example.yaml carries a shortened version of it --
        # so the guard fired only for someone with no letter.yaml at all, and
        # never for the likely case: copied the example, not yet edited. Every
        # letter would have gone out with "[FILL IN: ...]" in its first
        # paragraph.
        if "[FILL IN" in (frame.get("positioning") or ""):
            line(FAIL, "Cover letter positioning is still the placeholder",
                 "write frame.positioning in config/letter.yaml in your own voice")

        titles = settings.load_titles()
        if titles:
            info["titles"], info["excludes"] = len(titles[0]), len(titles[1])
        else:
            info["titles"] = len(cfg.get("title_keywords") or [])
            info["excludes"] = len(cfg.get("exclude_title_keywords") or [])
            line(WARN, "No titles.csv", "falling back to the lists in boards.yaml")
        if not info["titles"]:
            line(FAIL, "No search terms", "nothing would ever match")
        else:
            line(OK, f"{info['titles']} search term(s), {info['excludes']} exclusion(s)")
    except SystemExit as exc:
        line(FAIL, "A config file is malformed", str(exc).splitlines()[0].replace("[FATAL] ", ""))

    counts = {
        "greenhouse": len(cfg.get("greenhouse") or []),
        "lever": len(cfg.get("lever") or []),
        "ashby": len(cfg.get("ashby") or []),
        "workday": len(cfg.get("workday") or []),
        "smartrecruiters": len(cfg.get("smartrecruiters") or []),
    }
    for name, n in counts.items():
        if n:
            info["sources"].append(f"{name} ({n})")
    for name in ("jooble", "adzuna", "remotive", "jobicy", "careerjet",
                 "company_watchlist", "email", "apify_linkedin", "apify_indeed"):
        block = cfg.get(name) or {}
        if isinstance(block, dict) and block.get("enabled"):
            info["sources"].append(name)
    line(OK, f"{len(info['sources'])} source(s) enabled", ", ".join(info["sources"]))
    if not cfg.get("locations"):
        line(WARN, "No locations set", "every location will be accepted")
    return info


# --- profile ----------------------------------------------------------------

def check_profile() -> None:
    section("Profile")
    path = PROFILE / "master_profile.json"
    if not path.exists():
        line(FAIL, "master_profile.json missing",
             "copy profile/master_profile.example.json and fill it in")
        return
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        line(FAIL, "master_profile.json is not valid JSON", str(exc)[:90])
        return

    for field in ("name", "email", "location"):
        if (profile.get(field) or "").strip():
            line(OK, f"{field} set")
        else:
            line(FAIL, f"{field} missing", "it appears on every document")

    if (profile.get("name") or "") == "Jordan Avery":
        line(WARN, "Profile is still the shipped example", "documents would go out as Jordan Avery")

    jobs = profile.get("work_history") or []
    if not jobs:
        line(FAIL, "work_history is empty", "there is nothing to build a resume from")
    else:
        thin = [j.get("company", "?") for j in jobs if len(j.get("highlights") or []) < 2]
        line(OK, f"{len(jobs)} role(s), "
                 f"{sum(len(j.get('highlights') or []) for j in jobs)} highlight(s)")
        if thin:
            line(WARN, f"{len(thin)} role(s) with fewer than 2 highlights",
                 ", ".join(thin[:4]))

    skills = PROFILE / "skills_inventory.csv"
    if not skills.exists():
        line(WARN, "No skills_inventory.csv", "skills come from master_profile.json instead")
    else:
        with open(skills, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        yes = [r for r in rows if (r.get("have_it") or "").strip().lower() == "yes"]
        gaps = len(rows) - len(yes)
        if yes:
            line(OK, f"{len(yes)} claimed skill(s)", f"{gaps} tracked as gaps")
        else:
            line(FAIL, "skills_inventory.csv has no have_it=yes rows",
                 "every document would ship without skills")


# --- workspace --------------------------------------------------------------

def check_workspace() -> None:
    section("Workspace")
    try:
        OUTPUT.mkdir(exist_ok=True)
        probe = OUTPUT / ".doctor_write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        line(OK, "output/ is writable")
    except Exception as exc:
        line(FAIL, "output/ is not writable", str(exc)[:80])

    tracker = OUTPUT / "application_tracker.csv"
    if tracker.exists():
        try:
            with open(tracker, "a", encoding="utf-8"):
                pass
            with open(tracker, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            applied = sum(1 for r in rows if (r.get("date_submitted") or "").strip())
            line(OK, f"tracker: {len(rows)} row(s), {applied} applied")
        except PermissionError:
            line(WARN, "application_tracker.csv is locked",
                 "close it in Excel or the run writes a .NEW.csv instead")
    else:
        line(OK, "no tracker yet", "it is created on the first run")

    logs = sorted(LOGS.glob("pipeline_*.log")) if LOGS.exists() else []
    if not logs:
        line(WARN, "No run logs yet", "the pipeline has not run here")
        return
    newest = logs[-1]
    text = newest.read_text(encoding="utf-8", errors="replace")
    age = (datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)).days
    tail = "\n".join(text.splitlines()[-6:])
    if "[ERROR]" in tail:
        line(FAIL, f"last run ended in ERROR ({newest.name})", "see the end of that log")
    elif "[OK" in tail:
        line(OK, f"last run completed ({newest.name})", f"{age} day(s) ago")
    else:
        line(WARN, f"last run has no verdict ({newest.name})", "it may have been interrupted")


# --- sources that have stopped working --------------------------------------

# "[warn] greenhouse/sometoken failed: 404 ..." and "[warn] jobicy failed: ..."
WARN_TOKEN = re.compile(r"\[warn\] ([a-z_]+)/([A-Za-z0-9_\-]+) failed: (.*)")
WARN_SOURCE = re.compile(r"\[warn\] ([a-z_]+)(?: source)? failed: (.*)")
MIN_RUNS = 3          # one bad night is weather, three is a pattern


def check_dead_sources(window_days: int = 7) -> None:
    """
    A board token that has been removed announces itself only as a [warn] line
    that is easy to scroll past. Two board tokens died this way, each failing
    every night for weeks before anyone read the warning. Flag any
    source that failed in every run of the last week.
    """
    section(f"Sources failing (last {window_days} days)")
    logs = sorted(LOGS.glob("pipeline_*.log")) if LOGS.exists() else []
    cutoff = datetime.now().timestamp() - window_days * 86400
    logs = [p for p in logs if p.stat().st_mtime >= cutoff]
    if len(logs) < MIN_RUNS:
        line(OK, f"only {len(logs)} run(s) logged", "not enough history to judge")
        return

    # Per run, newest first: which sources failed and why.
    per_run: list[dict[str, str]] = []
    for log in sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True):
        seen_here: dict[str, str] = {}
        for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = WARN_TOKEN.search(raw)
            if m:
                seen_here[f"{m.group(1)}/{m.group(2)}"] = m.group(3)[:60]
                continue
            m = WARN_SOURCE.search(raw)
            if m and "query" not in raw:
                seen_here[m.group(1)] = m.group(2)[:60]
        per_run.append(seen_here)

    names = {n for run in per_run for n in run}
    dead, flaky = {}, {}
    for name in names:
        # A token removed from a board fails from that day on, so counting
        # across the whole window hides it behind the healthy runs that came
        # before: it would take a full week to surface. Count the streak of
        # consecutive most-recent runs instead, which shows it in three.
        streak, why = 0, ""
        for run in per_run:
            if name not in run:
                break
            streak += 1
            why = why or run[name]
        total = sum(1 for run in per_run if name in run)
        if streak >= MIN_RUNS:
            dead[name] = (streak, why)
        elif total >= MIN_RUNS:
            flaky[name] = (total, next(r[name] for r in per_run if name in r))

    for name, (streak, why) in sorted(dead.items()):
        detail = f"failed in the last {streak} run(s): {why}"
        if "404" in why:
            line(FAIL, f"{name} looks dead", detail + " — remove it from boards.yaml")
        else:
            line(WARN, f"{name} has failed every recent run", detail)
    for name, (total, why) in sorted(flaky.items()):
        line(WARN, f"{name} is flaky", f"failed in {total} of {len(per_run)} run(s): {why}")
    if not dead and not flaky:
        line(OK, f"no source has failed {MIN_RUNS} runs running",
             f"checked {len(per_run)} run(s)")


# --- retries that stopped being transient -----------------------------------

# "    [json] resume response did not parse (Expecting ',' delimiter...)"
JSON_RETRY = re.compile(r"\[json\] (.+?) response did not parse")
STYLE_RETRY = re.compile(r"\[style\] rewriting")
SCORED_LINE = re.compile(r"^\[\d{1,2}/10\]")
DRAFTED_LINE = re.compile(r"-> (?:resume|cover letter) drafted:")
LOST_DOC = re.compile(r"\[warn\] (resume|cover letter) (?:failed|still failing):\s*(.*)")

# One retry in ten responses is no longer an occasional bad draft, and paying
# twice for a quarter of them means something in the prompt or schema is wrong.
RETRY_RATE_WARN = 0.10
RETRY_RATE_FAIL = 0.25


def check_retries(threshold: int | None = None, window_days: int = 7) -> None:
    """
    A retry buys back a transient failure, and it can also hide a real one.

    The JSON retry and the style lint both re-ask the model and carry on, so a
    prompt or schema that has drifted into failing every time looks like a
    working pipeline that is quietly billed twice. The retry lines are easy to
    skim past in a log, which is exactly how two dead board tokens went
    unnoticed for weeks. Count them and say when the rate stops looking like
    bad luck.
    """
    section(f"Retried responses (last {window_days} days)")
    logs = sorted(LOGS.glob("pipeline_*.log")) if LOGS.exists() else []
    cutoff = datetime.now().timestamp() - window_days * 86400
    logs = [p for p in logs if p.stat().st_mtime >= cutoff]
    if len(logs) < MIN_RUNS:
        line(OK, f"only {len(logs)} run(s) logged", "not enough history to judge")
        return

    by_kind, lost, per_run = {}, [], []
    for log in sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True):
        here, style_here, calls_here = 0, 0, 0
        for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            m = JSON_RETRY.search(stripped)
            if m:
                here += 1
                by_kind[m.group(1)] = by_kind.get(m.group(1), 0) + 1
                continue
            if STYLE_RETRY.search(stripped):
                style_here += 1
                continue
            m = LOST_DOC.search(stripped)
            if m:
                lost.append(f"{log.name[9:-4]} {m.group(1)}: {m.group(2)[:50]}")
                continue
            # every one of these lines is a model response that parsed
            if SCORED_LINE.match(stripped) or DRAFTED_LINE.search(stripped):
                calls_here += 1
        per_run.append({"name": log.name, "json": here, "style": style_here,
                        # retries are themselves responses, so they count
                        "total": calls_here + here + style_here})

    json_retries = sum(r["json"] for r in per_run)
    style_retries = sum(r["style"] for r in per_run)
    total = sum(r["total"] for r in per_run)
    runs_with_json = sum(1 for r in per_run if r["json"])
    worst = max(((r["json"], r["name"]) for r in per_run), default=(0, ""))
    rate = json_retries / total if total else 0.0

    # A rate averaged over a week hides a failure that started last night --
    # the same arithmetic that let the dead board tokens sit for weeks. Judge
    # the most recent run on its own too, once it has enough responses to mean
    # something.
    newest = per_run[0]
    newest_rate = (newest["json"] / newest["total"]) if newest["total"] else 0.0
    newest_is_bad = newest["total"] >= 6 and newest_rate >= RETRY_RATE_FAIL
    kinds = ", ".join(f"{k} x{n}" for k, n in sorted(by_kind.items()))
    detail = (f"{json_retries} of {total} response(s), {rate:.0%}, "
              f"in {runs_with_json} of {len(logs)} run(s)"
              + (f" — {kinds}" if kinds else ""))

    if not json_retries:
        line(OK, "no malformed JSON responses to retry", f"across {len(logs)} run(s)")
    elif rate >= RETRY_RATE_FAIL or newest_is_bad:
        where = ("the last run alone" if newest_is_bad and rate < RETRY_RATE_FAIL
                 else "the window")
        line(FAIL, "Malformed JSON is the norm, not an accident",
             f"{detail}; {newest_rate:.0%} in {newest['name']} — over "
             f"{RETRY_RATE_FAIL:.0%} across {where}, so check the prompt and "
             "schema rather than paying twice")
    elif rate >= RETRY_RATE_WARN or runs_with_json >= MIN_RUNS:
        line(WARN, "Malformed JSON in every recent run" if runs_with_json >= MIN_RUNS
             else "Malformed JSON is getting common", detail)
    else:
        line(OK, "a few malformed JSON responses, all recovered", detail)
    if worst[0] >= 3:
        line(WARN, f"{worst[0]} retries in a single run", worst[1])

    # The style lint re-asking is normal; it doing so every time is not.
    if style_retries and total:
        srate = style_retries / total
        if srate >= RETRY_RATE_FAIL:
            line(WARN, "The style lint rewrites most letters",
                 f"{style_retries} rewrite(s), {srate:.0%} — the prompt and the "
                 "lint disagree about something")
        else:
            line(OK, f"{style_retries} style rewrite(s)", f"{srate:.0%} of responses")

    # A draft that failed outright left a document missing, and the run it
    # happened in says so once and never again. Whether that still matters
    # depends on what is missing now, not on what failed then: a warning with
    # nothing to do about it is how warnings stop being read.
    scored_path = OUTPUT / "scored_postings.json"
    missing = None
    if scored_path.exists() and threshold is not None:
        try:
            data = json.loads(scored_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None     # check_workspace already reports an unreadable file
        if data is not None:
            missing = [e for e in data
                       if e.get("score", 0) >= threshold
                       and (not e.get("tailored_resume_json")
                            or not e.get("cover_letter_json"))]

    if missing:
        line(WARN, f"{len(missing)} scored posting(s) missing a document",
             "run: python score_and_tailor.py --repair")
        for item in lost[-3:]:
            line(WARN, "A draft failed outright", item)
    elif lost:
        line(OK, f"{len(lost)} draft(s) failed outright and were repaired since",
             f"most recent: {lost[-1]}")
    elif missing is not None:
        line(OK, "every qualifying posting has both documents")


# --- optional dry run -------------------------------------------------------

def dry_run(info: dict, include_paid: bool) -> None:
    section("Dry run (no scoring, nothing written)")
    sys.path.insert(0, str(SRC))
    import scraper
    import yaml

    if not include_paid:
        # the Apify actors bill per result, so they stay out of a check
        cfg_path = CONFIG / "boards.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        paid = [name for name in ("apify_linkedin", "apify_indeed")
                if (cfg.get(name) or {}).get("enabled")]
        if paid:
            line(WARN, f"Skipping {len(paid)} paid source(s): "
                       + ", ".join(n.replace('apify_', '') for n in paid),
                 "they bill per result; add --include-paid to exercise them")
            off = {name: {"enabled": False} for name in paid}
            scraper.load_config = lambda: {**cfg, **off}

    from collections import Counter
    postings = scraper.collect_all_postings()
    by_source = Counter(p["source"] for p in postings)
    print()
    line(OK, f"{len(postings)} posting(s) survive your filters")
    for source, n in by_source.most_common():
        print(f"         {source:12} {n}")

    scored_path = OUTPUT / "scored_postings.json"
    if scored_path.exists():
        seen = {e.get("url") for e in json.loads(scored_path.read_text(encoding="utf-8"))}
        fresh = [p for p in postings if p["url"] not in seen]
        print()
        line(OK, f"{len(fresh)} of them have never been scored",
             "the rest cost nothing on the next run")
        if fresh and info.get("threshold") is not None:
            print(f"         a real run would score those {len(fresh)}, then draft "
                  f"documents for any that reach {info['threshold']}/10")
    else:
        print()
        line(OK, f"all {len(postings)} would be scored on the first run")


def main() -> int:
    print("job-app-pipeline doctor")
    check_runtime()
    creds = check_credentials()
    info = check_config()
    check_profile()
    check_workspace()
    check_dead_sources()
    check_retries(info.get("threshold"))

    if "--dry-run" in sys.argv:
        if not creds.get("anthropic"):
            print()
            line(WARN, "Running the dry run anyway", "it makes no API calls")
        try:
            dry_run(info, include_paid="--include-paid" in sys.argv)
        except Exception as exc:
            line(FAIL, "Dry run failed", f"{type(exc).__name__}: {str(exc)[:90]}")

    section("Summary")
    if _problems["fail"]:
        print(f"{_problems['fail']} problem(s) to fix, {_problems['warn']} warning(s).")
        return 1
    if _problems["warn"]:
        print(f"Ready to run. {_problems['warn']} warning(s) worth reading above.")
        return 0
    print("Everything checks out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
