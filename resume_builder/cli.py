"""
Build a master record of your career, then hand it to the job-application
pipeline.

  python -m resume_builder start                  # new here? this walks you through it
  python -m resume_builder import resume.pdf      # start from a resume or LinkedIn PDF
  python -m resume_builder interview              # fill gaps, thinnest role first
  python -m resume_builder interview --role Acme  # one employer
  python -m resume_builder interview --education  # education, certifications and the optional sections
  python -m resume_builder interview --section languages   # one of them
  python -m resume_builder summary --draft        # draft your professional summary from your record
  python -m resume_builder skills --build         # your field's skills, filled in from your record
  python -m resume_builder skills --verify        # answer them: a level, or no
  python -m resume_builder skills --add "Telemetry" --category Clinical --level Expert
  python -m resume_builder health                 # what is complete, what to do next
  python -m resume_builder export --to DIR        # show what exporting would change
  python -m resume_builder export --to DIR --write

The record is one Markdown file, record.md, in the current folder unless
--record or RESUME_BUILDER_RECORD says otherwise. Every command prints which
file it is using, so a session cannot quietly work on the wrong copy.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import export as X
from . import health, importer, interview, llm, skills
from . import record as mr
from .store import FileStore


def ask(question: str) -> str:
    try:
        return input(f"\n{question}\n> ").strip()
    except (KeyboardInterrupt, EOFError):
        raise interview.Stop()


def record_path(arg: str | None) -> Path:
    return Path(arg or os.environ.get("RESUME_BUILDER_RECORD") or "record.md").resolve()


def load(path: Path) -> mr.Record:
    if not path.exists():
        raise SystemExit(f"No record at {path}.\nStart one with: python -m resume_builder start")
    return mr.parse(path.read_text(encoding="utf-8"))


def save(path: Path, rec: mr.Record, stamp_backup: bool = False) -> None:
    if stamp_backup and path.exists():
        X.backup(path, f"{datetime.now():%Y%m%d-%H%M%S}")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(mr.render(rec), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------


def cmd_import(args, path: Path) -> int:
    src = Path(args.file)
    if not src.exists():
        raise SystemExit(f"No such file: {src}")
    llm.require_credentials()
    text = importer.read_text(src)
    if len(text.strip()) < 50:
        raise SystemExit(f"Could not read text from {src}. If it is a scanned PDF, "
                         f"save it as Word or paste the text into a .txt file.")
    print(f"  reading {src.name} ({len(text.split())} words)")
    incoming, rep = importer.to_record(importer.extract(text), text, src.name,
                                       f"{datetime.now():%Y-%m-%d}")
    if path.exists():
        rec = load(path)
        changes = importer.merge(rec, incoming)
        save(path, rec, stamp_backup=True)
        print(f"  merged into {path}: {len(changes)} change(s)")
    else:
        save(path, incoming)
        print(f"  wrote {path}: {len(incoming.roles)} role(s), "
              f"{sum(len(r.recorded_bullets) for r in incoming.roles)} bullet(s)")
    if rep["rejected"]:
        print(f"  {len(rep['rejected'])} line(s) the importer returned were not in the "
              f"document and were set aside for you to check.")
    if rep["unplaced"]:
        print(f"  {len(rep['unplaced'])} line(s) from the document were not filed; "
              f"they are under 'Unplaced' at the end of the record.")
    print(f"\n  Next: python -m resume_builder interview")
    return 0


def cmd_interview(args, path: Path) -> int:
    only = args.role or ""
    if getattr(args, "education", False):
        only = interview.BACKGROUND
    if getattr(args, "section", ""):
        only = f"{interview.BACKGROUND}:{args.section}"
    background = only.startswith(interview.BACKGROUND)
    if not background:                          # these sections need no model
        llm.require_credentials()
    print(f"  record: {path}")
    if background:
        print("  Just these sections; your place in the job interview is kept.")
    iv = interview.Interview(FileStore(path), only=only, target=args.target)
    if iv.state:
        print("  Picking up where you left off.")
    try:
        interview.run(iv, ask)
    except interview.Stop:
        print("\n  Stopped. Everything is saved; run again to carry on from here.")
    print(health.render(health.check(iv.rec, args.target)))
    if llm.spend_summary():
        print(f"\n  {llm.spend_summary()}")
    return 0


def cmd_skills(args, path: Path) -> int:
    rec = load(path)
    print(f"  record: {path}")
    if args.build:
        llm.require_credentials()
        out = skills.build(rec, args.field)
        save(path, rec)
        print(f"  Skills list for {out['field'] or 'your field'}: {len(out['added'])} added for "
              f"you to answer, {len(out['yours'])} already in your record, "
              f"{len(out['filled'])} existing skill(s) sorted or filled in. Nothing you had "
              f"set was changed. Next: skills --verify")
    if args.add:
        try:
            s = skills.add_skill(rec, args.add, args.category, args.level)
        except ValueError as exc:
            print(f"  {exc}")
            return 2
        save(path, rec)
        print(f"  added: {s.name} ({s.category}{', ' + s.level if s.level else ''})")
    if args.verify:
        skills.estimate_use(rec)        # dates the skills a record from before this had
        try:
            n = skills.verify(rec, ask)
        except interview.Stop:
            n = 0
        save(path, rec)
        print(f"  {n} skill(s) settled")
    if not (args.build or args.verify or args.add):
        for state, heading in mr.SKILL_SECTIONS.items():
            group = [s for s in rec.skills if s.have == state]
            if not group:
                continue
            print(f"\n  {heading.upper()}")
            cats = {}
            for s in group:
                cats.setdefault(s.category or "Other", []).append(s)
            for cat, items in cats.items():
                print(f"\n  {cat}")
                for s in items:
                    ev = f"  <- {'; '.join(s.evidence)}" if s.evidence else ""
                    bits = [b for b in (s.level,
                                        f"{s.years.lstrip('~')} yrs" if s.years else "",
                                        f"last used {s.last_used.lstrip('~')}" if s.last_used
                                        else "") if b]
                    print(f"    {s.name}{' (' + ', '.join(bits) + ')' if bits else ''}{ev}")
        print("\n  Change a level, group, years or answer by editing the record file, or "
              "with skills --verify. A '~' marks years worked out from your role dates.")
    return 0


def cmd_summary(args, path: Path) -> int:
    from . import summary
    rec = load(path)
    print(f"  record: {path}")
    if args.set:
        rec.summary = " ".join(args.set.split())
        save(path, rec)
        print("  saved.")
    elif args.draft:
        llm.require_credentials()
        out = summary.draft(rec, args.emphasis)
        print(f"\n  {out['text']}\n")
        if out["unsupported"]:
            print(f"  Check these figures; your record doesn't contain them: "
                  f"{', '.join(out['unsupported'])}")
        if (ask("Keep this as your summary? y to keep, Enter to discard") or "").lower() \
                .startswith("y"):
            rec.summary = out["text"]
            save(path, rec)
            print("  saved. Edit it any time in the record file, or with summary --set.")
    else:
        print(f"\n  {rec.summary or '(no summary yet: summary --draft, or summary --set TEXT)'}")
    for issue in summary.check(rec.summary):
        print(f"  note: {issue}")
    return 0


def cmd_health(args, path: Path) -> int:
    print(f"  record: {path}")
    print(health.render(health.check(load(path), args.target)))
    return 0


def cmd_export(args, path: Path) -> int:
    rec = load(path)
    out_dir = Path(args.to).resolve()
    profile_path = out_dir / "master_profile.json"
    csv_path = out_dir / "skills_inventory.csv"
    prints_path = path.with_name("." + path.stem + ".bullets.json")
    profile = (json.loads(profile_path.read_text(encoding="utf-8"))
               if profile_path.exists() else {})
    prints = (json.loads(prints_path.read_text(encoding="utf-8"))
              if prints_path.exists() else {})
    print(f"  record:  {path}\n  profile: {profile_path}"
          f"{'' if profile_path.exists() else ' (new)'}")

    ex = X.Export(rec, profile, prints, with_details=args.details)
    lost = ex.losses()

    if args.adopt:
        n = ex.adopt()
        if n:
            save(path, rec, stamp_backup=True)
        print(f"  copied {n} highlight(s) from the profile into the record. "
              f"Read them, delete duplicates, then export again.")
        return 0

    if lost:
        print("\n  Refusing: the profile has highlights the record does not, and "
              "exporting would drop them:")
        for employer, items in lost.items():
            for h in items:
                print(f"    {employer}: {h[:90]}")
        print("\n  Run with --adopt to copy them into the record first.")
        return 1

    issues = [i for r in health.check(rec).roles for i in r.issues]
    for i in issues:
        print(f"  [note] {i}")

    pending = sum(1 for _, a in rec.all_accomplishments()
                  if not a.is_empty() and not a.bullet)
    rows, added = X.merge_csv(X.read_csv(csv_path), X.skills_rows(rec))
    print(f"\n  {len(rec.roles)} role(s); {pending} bullet(s) to draft; "
          f"{added} new skill row(s) for the CSV")
    if not args.write:
        print("  Nothing written. Add --write when this looks right.")
        return 0

    if pending:
        llm.require_credentials()
    built = ex.build()
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in (profile_path, csv_path, path):
        b = X.backup(p, stamp)
        if b:
            print(f"  backup: {b.name}")
    profile_path.write_text(json.dumps(built, indent=2, ensure_ascii=False), encoding="utf-8")
    X.write_csv(csv_path, rows)
    save(path, rec)                 # keeps the drafted bullets beside their source
    prints_path.write_text(json.dumps(ex.prints, indent=2), encoding="utf-8")
    print(f"  wrote {profile_path.name}: {ex.drafted} bullet(s) drafted, {ex.reused} reused")
    print(f"  wrote {csv_path.name}: {len(rows)} row(s)")
    for c in ex.conflicts:
        print(f"  [kept] {c}")
    if ex.conflicts:
        print("  The profile's value was kept each time; edit either file to settle one.")
    if llm.spend_summary():
        print(f"  {llm.spend_summary()}")
    return 0


def cmd_start(args, path: Path) -> int:
    print("\nThis builds a master record of your career: every job, what you achieved\n"
          "in it, and what you can do. It has no page limit. Later, each job\n"
          "application picks the parts that fit that posting.\n")
    print(f"  record: {path}")
    if path.exists():
        print("  You already have a record. Here is where it stands:")
        return cmd_health(args, path)
    have = ask("Do you have an existing resume or a LinkedIn profile saved as PDF? "
               "Type its path, or press Enter to start from nothing.")
    if have:
        args.file = have.strip('"')
        cmd_import(args, path)
    return cmd_interview(args, path)


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="resume_builder", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", help="the record file (default: ./record.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="guided first run")
    p.add_argument("--target", type=int, default=10)
    p.add_argument("--role", default="")
    p = sub.add_parser("import", help="start from a resume or LinkedIn PDF")
    p.add_argument("file")
    p = sub.add_parser("interview", help="add accomplishments")
    p.add_argument("--role", default="", help="one employer (substring match)")
    p.add_argument("--education", action="store_true",
                   help="just education, certifications and the optional sections")
    p.add_argument("--section", default="", choices=["", *interview.SECTION_KEYS],
                   help="just one of those sections")
    p = sub.add_parser("summary", help="show, draft or set your professional summary")
    p.add_argument("--draft", action="store_true", help="draft one from your record")
    p.add_argument("--emphasis", default="", help="what to lead with (with --draft)")
    p.add_argument("--set", default="", metavar="TEXT", help="your own summary")
    p.add_argument("--target", type=int, default=10, help="accomplishments per role to aim for")
    p = sub.add_parser("skills", help="build, answer, add to or list your skills")
    p.add_argument("--build", action="store_true",
                   help="the standard skills for your field, filled in from your record")
    p.add_argument("--field", default="", help="your field, e.g. 'ICU nursing' (with --build)")
    p.add_argument("--verify", action="store_true", help="answer the skills on your list")
    p.add_argument("--add", default="", metavar="SKILL", help="add a skill the list missed")
    p.add_argument("--category", default="", help="its group (with --add)")
    p.add_argument("--level", default="", choices=["", *mr.LEVELS], help="(with --add)")
    p = sub.add_parser("health", help="what the record holds and what to do next")
    p.add_argument("--target", type=int, default=10)
    p = sub.add_parser("export", help="write the pipeline's profile and skills CSV")
    p.add_argument("--to", required=True, help="the pipeline's profile folder")
    p.add_argument("--write", action="store_true")
    p.add_argument("--adopt", action="store_true",
                   help="copy profile highlights the record lacks into the record")
    p.add_argument("--details", action="store_true",
                   help="also pass each accomplishment's problem/actions/results to the pipeline")

    args = ap.parse_args(argv)
    path = record_path(args.record)
    return {"start": cmd_start, "import": cmd_import, "interview": cmd_interview,
            "skills": cmd_skills, "summary": cmd_summary, "health": cmd_health, "export": cmd_export}[args.cmd](args, path)
