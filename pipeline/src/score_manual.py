"""
Scores and (if it clears the threshold) tailors a resume for a single posting
you paste in by hand -- for sources like LinkedIn that don't expose a public
API the way Greenhouse/Lever/Workday do, so scraper.py can't reach them.

Copy the posting details yourself (title, company, location, url, and the
job description text) and pass them here. Merges into the same
output/scored_postings.json used by the rest of the pipeline, so results
show up alongside everything else.

Usage:
  python score_manual.py --company "Acme Inc" --title "Director, Marketing Analytics" \\
      --location "Remote, USA" --url "https://linkedin.com/jobs/view/1234567890" \\
      --description-file posting.txt

  (--description "<text>" also works for short postings, in place of --description-file)
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from score_and_tailor import (score_posting, tailor_resume, draft_cover_letter,
                              load_profile, SCORE_THRESHOLD, resume_filename_base)

SRC_DIR = Path(__file__).parent
OUTPUT_DIR = SRC_DIR.parent / "output"
SCORED_PATH = OUTPUT_DIR / "scored_postings.json"


def build_posting(args) -> dict:
    description = args.description
    if args.description_file:
        description = Path(args.description_file).read_text(encoding="utf-8")
    posting_id = hashlib.sha1(args.url.encode()).hexdigest()[:12]
    return {
        "source": args.source,
        "company": args.company,
        "title": args.title,
        "location": args.location,
        "url": args.url,
        "description_html": description,
        "posting_id": posting_id,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--location", default="")
    parser.add_argument("--url", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--description-file")
    parser.add_argument("--source", default="linkedin")
    args = parser.parse_args()

    if not args.description and not args.description_file:
        parser.error("provide --description or --description-file")

    posting = build_posting(args)
    profile = load_profile()

    eval_result = score_posting(posting, profile)
    entry = {**posting, **eval_result}
    print(f"[{entry['score']}/10] {posting['title']} @ {posting['company']}"
          f"{' (overqualification risk)' if entry.get('overqualification_risk') else ''}")
    print(f"  reasoning: {entry['reasoning']}")

    if eval_result["score"] >= SCORE_THRESHOLD:
        resume_data = tailor_resume(posting, profile)
        OUTPUT_DIR.mkdir(exist_ok=True)
        base = resume_filename_base(posting["company"], posting["title"], OUTPUT_DIR)
        json_path = OUTPUT_DIR / f"{base}.json"
        docx_path = OUTPUT_DIR / f"{base}.docx"
        json_path.write_text(json.dumps(resume_data, indent=2), encoding="utf-8")
        entry["tailored_resume_json"] = str(json_path)
        entry["tailored_resume_docx"] = str(docx_path)
        print(f"  -> resume drafted: {json_path.name}")
        try:
            subprocess.run(["node", str(SRC_DIR / "render_resume.js"), str(json_path), str(docx_path)], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[warn] failed to render docx: {e}")

        cover_data = draft_cover_letter(posting, profile)
        cover_json_path = OUTPUT_DIR / f"{base}_cover.json"
        cover_docx_path = OUTPUT_DIR / f"{base}_cover.docx"
        cover_json_path.write_text(json.dumps(cover_data, indent=2), encoding="utf-8")
        entry["cover_letter_json"] = str(cover_json_path)
        entry["cover_letter_docx"] = str(cover_docx_path)
        print(f"  -> cover letter drafted: {cover_json_path.name}")
        try:
            subprocess.run(["node", str(SRC_DIR / "render_cover_letter.js"),
                            str(cover_json_path), str(cover_docx_path)], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[warn] failed to render cover letter docx: {e}")

    previous = json.loads(SCORED_PATH.read_text(encoding="utf-8")) if SCORED_PATH.exists() else []
    merged = {r["posting_id"]: r for r in previous}
    merged[entry["posting_id"]] = entry
    OUTPUT_DIR.mkdir(exist_ok=True)
    SCORED_PATH.write_text(json.dumps(list(merged.values()), indent=2), encoding="utf-8")
    print(f"\nDone. scored_postings.json now has {len(merged)} total entries.")


if __name__ == "__main__":
    main()
