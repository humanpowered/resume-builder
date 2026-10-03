"""
Batch version of score_manual.py -- scores every NEW row in a CSV of postings
against the master profile, and tailors + renders a resume for anything that
clears the threshold. Rows already present in output/scored_postings.json
(matched by url) are skipped, so you can re-run this after adding new rows
to the CSV without re-scoring or re-paying for postings already done.

CSV columns required: company, title, location, url, description
(description can contain newlines -- just keep it inside quotes)

Defaults to the persistent CSV at the project root
(../../linkedin_postings.csv relative to this file) -- add rows to that
file over time as you find postings, then just re-run this with no args.

Usage:
  python score_batch.py                 # uses the default persistent CSV
  python score_batch.py other.csv       # or point at a specific file
"""
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from score_and_tailor import (score_posting, tailor_resume, draft_cover_letter,
                              load_profile, SCORE_THRESHOLD, resume_filename_base)

SRC_DIR = Path(__file__).parent
OUTPUT_DIR = SRC_DIR.parent / "output"
SCORED_PATH = OUTPUT_DIR / "scored_postings.json"
DEFAULT_CSV = SRC_DIR.parent.parent / "linkedin_postings.csv"

REQUIRED_COLUMNS = {"company", "title", "location", "url", "description"}


def load_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV is missing required column(s): {', '.join(sorted(missing))}")
        return [row for row in reader if row.get("url", "").strip()]


def row_to_posting(row: dict) -> dict:
    posting_id = hashlib.sha1(row["url"].strip().encode()).hexdigest()[:12]
    return {
        "source": "linkedin",
        "company": row["company"].strip(),
        "title": row["title"].strip(),
        "location": row.get("location", "").strip(),
        "url": row["url"].strip(),
        "description_html": row["description"],
        "posting_id": posting_id,
    }


def main():
    if len(sys.argv) > 2:
        print("Usage: python score_batch.py [postings.csv]")
        sys.exit(1)

    csv_path = Path(sys.argv[1]) if len(sys.argv) == 2 else DEFAULT_CSV
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        sys.exit(1)
    rows = load_rows(csv_path)
    print(f"Read {len(rows)} row(s) from {csv_path.name}")

    previous = json.loads(SCORED_PATH.read_text(encoding="utf-8")) if SCORED_PATH.exists() else []
    seen_urls = {r["url"] for r in previous if "url" in r}
    merged = {r["posting_id"]: r for r in previous}

    postings = [row_to_posting(row) for row in rows]
    new_postings = [p for p in postings if p["url"] not in seen_urls]
    skipped = len(postings) - len(new_postings)
    if skipped:
        print(f"Skipping {skipped} already-scored posting(s)")

    if not new_postings:
        print("Nothing new to score.")
        return

    profile = load_profile()
    OUTPUT_DIR.mkdir(exist_ok=True)

    for posting in new_postings:
        eval_result = score_posting(posting, profile)
        entry = {**posting, **eval_result}
        print(f"[{entry['score']}/10] {posting['title']} @ {posting['company']}"
              f"{' (overqualification risk)' if entry.get('overqualification_risk') else ''}")

        if eval_result["score"] >= SCORE_THRESHOLD:
            resume_data = tailor_resume(posting, profile)
            base = resume_filename_base(posting["company"], posting["title"])
            json_path = OUTPUT_DIR / f"{base}.json"
            docx_path = OUTPUT_DIR / f"{base}.docx"
            json_path.write_text(json.dumps(resume_data, indent=2), encoding="utf-8")
            entry["tailored_resume_json"] = str(json_path)
            entry["tailored_resume_docx"] = str(docx_path)
            print(f"    -> resume drafted: {json_path.name}")
            try:
                subprocess.run(
                    ["node", str(SRC_DIR / "render_resume.js"), str(json_path), str(docx_path)],
                    check=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f"    [warn] failed to render docx: {e}")

            cover_data = draft_cover_letter(posting, profile)
            cover_json_path = OUTPUT_DIR / f"{base}_cover.json"
            cover_docx_path = OUTPUT_DIR / f"{base}_cover.docx"
            cover_json_path.write_text(json.dumps(cover_data, indent=2), encoding="utf-8")
            entry["cover_letter_json"] = str(cover_json_path)
            entry["cover_letter_docx"] = str(cover_docx_path)
            print(f"    -> cover letter drafted: {cover_json_path.name}")
            try:
                subprocess.run(
                    ["node", str(SRC_DIR / "render_cover_letter.js"),
                     str(cover_json_path), str(cover_docx_path)],
                    check=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f"    [warn] failed to render cover letter docx: {e}")

        merged[entry["posting_id"]] = entry

    SCORED_PATH.write_text(json.dumps(list(merged.values()), indent=2), encoding="utf-8")
    print(f"\nDone. Scored {len(new_postings)} new posting(s). "
          f"scored_postings.json now has {len(merged)} total entries.")


if __name__ == "__main__":
    main()
