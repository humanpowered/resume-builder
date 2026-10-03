"""
The nightly run: the pipeline, the reply check, and the morning brief, written
to logs/pipeline_YYYY-MM-DD.log as well as to the screen.

Usage:
  python run_nightly.py              # run everything, append to today's log
  python run_nightly.py --keep 60    # keep 60 days of logs instead of 30

This exists because the logging used to live in a Windows .cmd file that
redirected stdout. Three things followed from that, none of them obvious:
`python run_pipeline.py` -- which is what the README tells you to run --
produced no log at all, the morning brief had nothing to read, and doctor's
dead-source and retry-rate checks sat permanently idle reporting "not enough
history". The diagnostics were only ever alive on one machine.

So the orchestration is here, in Python, on any OS. run_nightly.cmd and
run_nightly.sh are one-line wrappers for Task Scheduler and cron.

Two details worth keeping if you edit this:

  - PYTHONIOENCODING=utf-8 for the children. Writing to a pipe makes Python's
    stdout cp1252 on Windows, and a posting title carrying a macron or an
    accent then kills the run mid-scoring. One did, on U+014C.
  - the verdict lines at the end ([OK], [ERROR], [OK with WARNING]) are read
    by doctor and by build_digest. They are an interface, not decoration.
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
LOGS = ROOT / "logs"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass            # no usable stdout; the log is what matters, see say()


def say(text: str, log=None) -> None:
    """
    Write a line to the log, and to the screen when there is one.

    A scheduler can start this with no console at all, and on Windows a write
    to that stdout raises rather than going nowhere quietly. The old wrapper
    never met this because it redirected stdout into the log file, so a handle
    always existed. The log is the record; the screen is a convenience.
    """
    if log is not None:
        log.write(text + "\n")
        log.flush()                 # a crash should not take the log with it
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass

# (script, what to say before it, does its exit code decide the run's verdict)
STEPS = [
    ("run_pipeline.py", "", True),
    # Runs even when the pipeline failed: a rejection that arrived is worth
    # recording either way. Its own failure must not mask the pipeline's code.
    ("check_replies.py", "Checking for replies from employers...", False),
    # Reads the log written above, so it goes last.
    ("build_digest.py", "", False),
]


def run_step(script: str, log) -> int:
    """Run one step, sending its output to the screen and the log together."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, script], cwd=SRC, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for raw in proc.stdout:
        say(raw.decode("utf-8", errors="replace").rstrip("\r\n"), log)
    return proc.wait()


def prune(keep_days: int) -> None:
    cutoff = time.time() - keep_days * 86400
    for old in LOGS.glob("pipeline_*.log"):
        if old.stat().st_mtime < cutoff:
            try:
                old.unlink()
            except OSError:
                pass                # locked or already gone; not worth failing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", type=int, default=30, metavar="DAYS",
                    help="how many days of logs to keep (default 30)")
    args = ap.parse_args()

    LOGS.mkdir(exist_ok=True)
    log_path = LOGS / f"pipeline_{datetime.now():%Y-%m-%d}.log"

    verdict_code = 0
    with open(log_path, "a", encoding="utf-8") as log:
        bar = "=" * 60
        for text in (bar, f"{datetime.now():%Y-%m-%d %H:%M:%S}", bar):
            say(text, log)

        for script, announce, decides in STEPS:
            if announce:
                say(f"\n{announce}", log)
            if not (SRC / script).exists():
                say(f"[warn] {script} is missing; skipping that step", log)
                continue
            code = run_step(script, log)
            if decides:
                verdict_code = code

        # A locked tracker still exits 0, so say so rather than reporting a
        # clean run that silently saved nothing.
        locked = "[TRACKER LOCKED]" in log_path.read_text(encoding="utf-8",
                                                          errors="replace")
        if verdict_code != 0:
            verdict = f"[ERROR] pipeline exited with code {verdict_code}"
        elif locked:
            verdict = ("[OK with WARNING] pipeline completed but the tracker was "
                       "locked -- see above")
        else:
            verdict = "[OK] pipeline completed"
        say(verdict, log)

    prune(args.keep)
    return verdict_code


if __name__ == "__main__":
    sys.exit(main())
