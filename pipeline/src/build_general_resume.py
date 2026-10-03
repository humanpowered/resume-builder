"""
Builds a general, non-tailored resume for autofill tools (Simplify Copilot)
and for any application that just wants a resume on file.

Differs from the pipeline's tailored resumes in three ways:
  - covers the full range of your background rather than mirroring one
    posting's language
  - selects the strongest metric-bearing highlights per role instead of
    reordering for a specific job
  - skills come from skills_inventory.csv grouped by category, so the
    document stays in sync with the source of truth

Regenerate any time the profile or skills CSV changes:
    python build_general_resume.py
"""
import csv
import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).parent
PROFILE_DIR = SRC.parent / "profile"
OUTPUT_DIR = SRC.parent / "output"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Named from the profile rather than hardcoded, so this file carries no
# personal details.
BASENAME_TEMPLATE = "{name} - Resume (General)"

# Which highlights to carry, by company. Chosen for metrics and breadth;
# keeping every bullet from master_profile would run past three pages. This
# lives in config/ rather than here, so the code carries nobody's employment
# history. Each string is a PREFIX of a highlight in the profile.
KEEP_PATH = SRC.parent / "config" / "general_resume_keep.json"


def load_keep() -> dict:
    if KEEP_PATH.exists():
        return json.loads(KEEP_PATH.read_text(encoding="utf-8"))
    print(f"  no {KEEP_PATH.name}; falling back to the first bullets of each role")
    return {}

# Skill categories are shown in the order the inventory lists them, which is
# the order the person chose. Certifications and industries are handled
# separately. (This used to be a fixed list of one analyst's categories, so
# anyone else's skills never reached the page.)
SEPARATE = ("industry experience",)


def load_skills() -> dict:
    path = PROFILE_DIR / "skills_inventory.csv"
    by_cat = {}
    if not path.exists():
        return by_cat
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if (row.get("have_it") or "").strip().lower() != "yes":
                continue
            cat = (row.get("category") or "").strip()
            skill = (row.get("skill") or "").strip()
            if not skill or cat.lower().startswith("certification"):
                continue
            # drop the parenthetical detail; a resume line wants the short form
            by_cat.setdefault(cat, []).append(skill.split(" (")[0].split(":")[0].strip())
    return by_cat


def main():
    profile = json.loads((PROFILE_DIR / "master_profile.json").read_text(encoding="utf-8"))
    skills = load_skills()

    keep = load_keep()
    experience = []
    for job in profile["work_history"]:
        wanted = keep.get(job["company"], [])
        bullets = [h for h in job["highlights"]
                   if any(h.startswith(p) for p in wanted)]
        if not bullets:
            bullets = job["highlights"][:4]
        experience.append({
            "title": job["title"],
            "company": "  |  ".join(x for x in (job["company"], job.get("location")) if x),
            "dates": job["dates"],
            "bullets": bullets,
        })

    # Cap per category. The full inventory is 78 skills; dumping all of them
    # reads as keyword stuffing and buries the ones that matter.
    PER_CATEGORY = 8
    skill_lines = []
    for cat in [c for c in skills if c.lower() not in SEPARATE]:
        items = skills.get(cat)
        if not items:
            continue
        seen, unique = set(), []
        for s in items:
            k = s.lower().rstrip("s")
            if k in seen or any(k.startswith(p) or p.startswith(k) for p in seen):
                continue          # drops "Time series forecasting methods" next to "Time series forecasting"
            seen.add(k)
            unique.append(s)
        skill_lines.append(f"{cat}: " + ", ".join(unique[:PER_CATEGORY]))
    industries = skills.get("Industry Experience")
    if industries:
        skill_lines.append("Industry Experience: " + ", ".join(industries))

    resume = {
        "name": f"{profile['name']}, {profile.get('credential_suffix','')}".strip().rstrip(","),
        "contact": " | ".join(x for x in (
            profile.get("location"), profile.get("email"), profile.get("phone"),
            (profile.get("linkedin") or "").replace("https://www.", "").rstrip("/")) if x),
        "summary": profile.get("summary", ""),
        "experience": experience,
        "skills": skill_lines,
        "education": profile.get("education", []) + profile.get("certifications", [])[:3],
    }

    OUTPUT_DIR.mkdir(exist_ok=True)
    basename = BASENAME_TEMPLATE.format(name=profile["name"])
    json_path = OUTPUT_DIR / f"{basename}.json"
    docx_path = OUTPUT_DIR / f"{basename}.docx"
    json_path.write_text(json.dumps(resume, indent=2), encoding="utf-8")

    try:
        subprocess.run(["node", str(SRC / "render_resume.js"), str(json_path), str(docx_path)],
                       check=True, capture_output=True)
        print(f"wrote {docx_path.name}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"[warn] render failed: {e}")

    total = sum(len(j["bullets"]) for j in experience)
    print(f"  {len(experience)} roles, {total} bullets, {len(skill_lines)} skill lines")
    for j in experience:
        print(f"    {j['dates']:<22} {j['company'].split('|')[0].strip():<28} {len(j['bullets'])} bullets")


if __name__ == "__main__":
    main()
