"""
Score a batch of postings you found yourself, from their URLs alone.

You browse (LinkedIn, Simplify, a company careers page, anywhere), collect
the URLs of anything worth a look, and this fetches each one, pulls out the
title/company/description, scores it, and drafts documents for whatever
clears the threshold. Postings already in scored_postings.json are skipped,
so re-running costs nothing for ones already done.

Usage:
    python score_urls.py                       # uses ../../job_urls.txt
    python score_urls.py <url> <url> ...       # or pass URLs inline
    python score_urls.py other.txt             # or a different file
    python score_urls.py --no-cover            # resume only
    python score_urls.py --dry-run             # fetch + show, don't score

Lines starting with # are ignored, so you can annotate the file.
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

SRC = Path(__file__).parent
OUTPUT_DIR = SRC.parent / "output"
SCORED_PATH = OUTPUT_DIR / "scored_postings.json"
# standing list at the project root, alongside linkedin_postings.csv
DEFAULT_URL_FILE = SRC.parent.parent / "job_urls.txt"

from score_and_tailor import (score_posting, tailor_resume, draft_cover_letter,
                              load_profile, resume_filename_base, SCORE_THRESHOLD,
                              letter_body, find_ai_tells, find_style_issues,
                              find_ungrounded_claims)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"}


def strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def from_jsonld(html: str) -> dict | None:
    """
    Employers publish schema.org JobPosting as JSON-LD so search engines can
    read it. When present it is far cleaner than scraping the rendered page.
    """
    for m in re.finditer(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            if node.get("@type") != "JobPosting":
                continue
            org = node.get("hiringOrganization") or {}
            loc = node.get("jobLocation") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = (loc or {}).get("address") or {}
            parts = [addr.get("addressLocality"), addr.get("addressRegion")]
            location = ", ".join(p for p in parts if p)
            if node.get("jobLocationType") == "TELECOMMUTE":
                location = f"Remote ({location})" if location else "Remote"
            return {
                "title": (node.get("title") or "").strip(),
                "company": (org.get("name") if isinstance(org, dict) else str(org) or "").strip(),
                "location": location,
                "description": strip_tags(node.get("description") or ""),
            }
    return None


def from_greenhouse_api(url: str) -> dict | None:
    """
    Greenhouse job pages are client-rendered, so scraping them yields a few
    hundred characters of shell. They expose the same posting through a
    public API, which is both cleaner and lighter on their servers.
    """
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?]+)/jobs/(\d+)", url)
    if not m:
        return None
    token, job_id = m.group(1), m.group(2)
    try:
        r = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}",
            headers=UA, timeout=20)
        r.raise_for_status()
        j = r.json()
    except Exception:
        return None
    return {
        "title": j.get("title", ""),
        "company": token,
        "location": (j.get("location") or {}).get("name", ""),
        "description": strip_tags(j.get("content", "")),
        "_source": "Greenhouse API",
    }


def fetch(url: str) -> dict | None:
    direct = from_greenhouse_api(url)
    if direct and len(direct["description"]) > 300:
        return direct

    try:
        r = requests.get(url, headers=UA, timeout=30, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        print(f"    fetch failed: {type(e).__name__}: {str(e)[:80]}")
        return None

    html = r.text
    got = from_jsonld(html)
    source = "JSON-LD"
    if not got or len(got.get("description", "")) < 200:
        # fall back to page text; title from <title>, company from the host
        text = strip_tags(html)
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = (m.group(1).strip() if m else "").split("|")[0].split(" at ")[0].strip()
        host = urlparse(url).netloc.replace("www.", "").split(".")[0]
        got = {"title": title or "[FILL IN: title]",
               "company": (got or {}).get("company") or host,
               "location": (got or {}).get("location", ""),
               "description": text}
        source = "page text"

    got["_source"] = source
    return got


def main():
    args = [a for a in sys.argv[1:]]
    no_cover = "--no-cover" in args
    dry_run = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]
    if not args:
        if not DEFAULT_URL_FILE.exists():
            print(__doc__)
            print(f"No URLs given and {DEFAULT_URL_FILE} does not exist.")
            sys.exit(1)
        args = [str(DEFAULT_URL_FILE)]
        print(f"reading {DEFAULT_URL_FILE.name}")

    urls = []
    for a in args:
        p = Path(a)
        if p.exists():
            urls += [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
        else:
            urls.append(a)
    urls = list(dict.fromkeys(urls))          # de-dupe, keep order

    data = json.loads(SCORED_PATH.read_text(encoding="utf-8")) if SCORED_PATH.exists() else []
    seen = {e["url"] for e in data if "url" in e}
    todo = [u for u in urls if u not in seen]
    print(f"{len(urls)} url(s) given, {len(urls)-len(todo)} already scored, {len(todo)} to process\n")
    if not todo:
        return

    profile = load_profile()
    OUTPUT_DIR.mkdir(exist_ok=True)
    added = 0

    for url in todo:
        print(f"  {url[:96]}")
        got = fetch(url)
        if not got:
            continue
        desc = got["description"]
        print(f"    [{got['_source']}] {got['company'][:28]} | {got['title'][:44]} | {len(desc)} chars")
        if len(desc) < 300:
            print("    too little description text to score reliably; skipping. Add it by hand")
            print("    to linkedin_postings.csv if you want this one.")
            continue
        if dry_run:
            continue

        posting = {
            "source": "url-batch",
            "company": got["company"],
            "title": got["title"],
            "location": got["location"],
            "url": url,
            "description_html": desc,
            "posting_id": hashlib.sha1(url.encode()).hexdigest()[:12],
        }

        try:
            entry = {**posting, **score_posting(posting, profile)}
        except Exception as e:
            print(f"    scoring failed: {type(e).__name__}: {str(e)[:80]}")
            continue

        flag = " (overqualification risk)" if entry.get("overqualification_risk") else ""
        print(f"    [{entry['score']}/10]{flag}")

        if entry["score"] >= SCORE_THRESHOLD:
            base = resume_filename_base(entry["company"], entry["title"])
            try:
                rj, rd = OUTPUT_DIR / f"{base}.json", OUTPUT_DIR / f"{base}.docx"
                rj.write_text(json.dumps(tailor_resume(posting, profile), indent=2), encoding="utf-8")
                subprocess.run(["node", str(SRC / "render_resume.js"), str(rj), str(rd)],
                               check=True, capture_output=True)
                entry["tailored_resume_json"], entry["tailored_resume_docx"] = str(rj), str(rd)
                print(f"    resume -> {rd.name}")
            except Exception as e:
                print(f"    [warn] resume failed: {type(e).__name__}: {str(e)[:70]}")

            if not no_cover:
                try:
                    cover = draft_cover_letter(posting, profile)
                    cj, cd = OUTPUT_DIR / f"{base}_cover.json", OUTPUT_DIR / f"{base}_cover.docx"
                    cj.write_text(json.dumps(cover, indent=2), encoding="utf-8")
                    subprocess.run(["node", str(SRC / "render_cover_letter.js"), str(cj), str(cd)],
                                   check=True, capture_output=True)
                    entry["cover_letter_json"], entry["cover_letter_docx"] = str(cj), str(cd)
                    probs = (find_ungrounded_claims(cover, profile)
                             + find_ai_tells(cover) + find_style_issues(cover, profile))
                    print(f"    cover  -> {cd.name} | lint: {'; '.join(probs) or 'clean'}")
                except Exception as e:
                    print(f"    [warn] cover letter failed: {type(e).__name__}: {str(e)[:70]}")

        data.append(entry)
        added += 1
        SCORED_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print()

    if added and not dry_run:
        try:
            from build_tracker import build
            build()
        except Exception as e:
            print(f"[warn] tracker update failed: {e}")
    print(f"Done. {added} posting(s) added.")


if __name__ == "__main__":
    main()
