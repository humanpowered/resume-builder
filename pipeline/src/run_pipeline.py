"""
End-to-end pipeline: scrape postings -> score fit -> tailor resumes -> render docx.
Does NOT submit anything -- reviewing and applying is a deliberate manual step
you run per-posting once you've reviewed a resume.

Usage: python run_pipeline.py
"""
import json
import subprocess
import sys
from pathlib import Path

# Job titles carry characters cp1252 cannot encode, and Windows picks cp1252
# for stdout whenever output is redirected to a file. Without this a single
# posting title crashes the whole run. run_nightly.cmd also sets
# PYTHONIOENCODING; this covers running the pipeline by hand.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scraper import collect_all_postings                    # noqa: E402
from score_and_tailor import run as score_and_tailor_run    # noqa: E402

SRC_DIR = Path(__file__).parent


def _render(script: str, json_path: str, docx_path: str):
    try:
        subprocess.run(
            ["node", str(SRC_DIR / script), json_path, docx_path],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[warn] failed to render {docx_path}: {e}")
    except FileNotFoundError:
        print("[warn] node not found -- install Node.js to render .docx files, "
              f"or run {script} manually.")


def render_all_docx(results: list[dict]):
    for entry in results:
        json_path = entry.get("tailored_resume_json")
        docx_path = entry.get("tailored_resume_docx")
        if json_path:
            print(f"Rendering resume for {entry['company']} - {entry['title']}...")
            _render("render_resume.js", json_path, docx_path)

        cover_json = entry.get("cover_letter_json")
        cover_docx = entry.get("cover_letter_docx")
        if cover_json:
            print(f"Rendering cover letter for {entry['company']} - {entry['title']}...")
            _render("render_cover_letter.js", cover_json, cover_docx)


def main():
    print("Step 1/4: searching job boards...")
    postings = collect_all_postings()
    print(f"  found {len(postings)} postings matching your keyword/location filters\n")

    if not postings:
        print("No matching postings found. Check config/boards.yaml -- "
              "add company tokens, or loosen title_keywords/locations.")
        sys.exit(0)

    print("Step 2/4: scoring fit and drafting tailored resumes...")
    results = score_and_tailor_run(postings)

    print("\nStep 3/4: rendering resumes to .docx...")
    render_all_docx(results)

    print("\nStep 4/4: updating application tracker...")
    try:
        from build_tracker import build, report
        report(build())
    except Exception as e:
        print(f"[warn] tracker update failed: {e}")

    print("\nDone. Review output/scored_postings.json for the full list.")
    print("For anything you want to apply to:")
    print("  review the .docx files above, apply on the employer's site,")
    print("  then record the submit date in output/application_tracker.csv")


if __name__ == "__main__":
    main()
