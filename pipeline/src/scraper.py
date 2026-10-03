"""
Pulls open postings from public Greenhouse and Lever job board APIs.
No auth required -- these endpoints are meant for public consumption
(they're what powers the "Careers" widget on a company's own site).

Also pulls from search-style aggregators (Jooble, Adzuna, Remotive) that
return postings across many companies for a keyword query, rather than one
company at a time. Those results still pass through the same
title_keywords/locations filter as everything else -- the aggregator's own
search is a coarse first net.
"""
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta

import requests
import yaml
from pathlib import Path

from settings import load_titles

CONFIG_PATH = Path(__file__).parent.parent / "config" / "boards.yaml"


def load_config():
    """
    Read config/boards.yaml, saying what to do when it is not there.

    Every other config file in this project fails with the file name and the
    problem; this one raised a bare FileNotFoundError with an absolute path,
    which is the first thing a new user sees if they clone the repo and run the
    pipeline before copying the example.
    """
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"No {CONFIG_PATH.name} found at {CONFIG_PATH}.\n"
            f"Copy the example and edit it:\n"
            f"  cp config/boards.example.yaml config/boards.yaml\n"
            f"Then run `python src/doctor.py` to check the rest of the setup.")
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"{CONFIG_PATH.name} is not valid YAML: {exc}")


def fetch_greenhouse(token: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "source": "greenhouse",
            "company": token,
            "title": j["title"],
            "location": j.get("location", {}).get("name", ""),
            "url": j["absolute_url"],
            "description_html": j.get("content", ""),
            "posting_id": str(j["id"]),
        }
        for j in jobs
    ]


def fetch_lever(token: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json()
    return [
        {
            "source": "lever",
            "company": token,
            "title": j["text"],
            "location": j.get("categories", {}).get("location", ""),
            "url": j["hostedUrl"],
            "description_html": j.get("descriptionPlain", j.get("description", "")),
            "posting_id": j["id"],
        }
        for j in jobs
    ]


def fetch_workday(tenant: str, host: str, site: str, max_pages: int = 5) -> list[dict]:
    """
    Workday doesn't have one uniform public API, but most tenants expose an
    internal CXS endpoint used by their own careers page, which we can call
    the same way the page's JS does. Structure/behavior can vary by tenant --
    if this returns nothing, open the careers page devtools Network tab,
    filter for 'jobs', and adjust the payload shape to match.
    """
    base = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    headers = {"Content-Type": "application/json"}
    results = []
    offset = 0
    page_size = 20

    for _ in range(max_pages):
        payload = {"limit": page_size, "offset": offset, "searchText": ""}
        resp = requests.post(base, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            results.append({
                "source": "workday",
                "company": tenant,
                "title": p.get("title", ""),
                "location": p.get("locationsText", ""),
                "url": f"https://{tenant}.{host}.myworkdayjobs.com/{site}{p.get('externalPath', '')}",
                "description_html": "",  # Workday needs a per-posting fetch for full description
                "posting_id": p.get("bulletFields", [None])[0] or p.get("externalPath", ""),
            })
        offset += page_size
        if len(postings) < page_size:
            break

    return results


def fetch_ashby(board_name: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board_name}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "source": "ashby",
            "company": board_name,
            "title": j["title"],
            "location": j.get("location", ""),
            "url": j["jobUrl"],
            "description_html": j.get("descriptionHtml", ""),
            "posting_id": j["id"],
        }
        for j in jobs
    ]


def fetch_smartrecruiters(company: str, keywords: list[str]) -> list[dict]:
    """
    The listing endpoint doesn't include the job description, so to avoid an
    expensive per-posting fetch for every listed job, only postings whose
    title already matches title_keywords get a detail fetch.
    """
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    postings = resp.json().get("content", [])
    keywords_lower = [k.lower() for k in keywords]

    results = []
    for p in postings:
        title = p.get("name", "")
        if not _title_matches(title, keywords_lower):
            continue
        loc = p.get("location", {})
        location = loc.get("fullLocation", "") or ("Remote" if loc.get("remote") else "")

        detail_url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{p['id']}"
        detail_resp = requests.get(detail_url, timeout=15)
        detail_resp.raise_for_status()
        detail = detail_resp.json()
        sections = detail.get("jobAd", {}).get("sections", {})
        description = " ".join(s.get("text", "") for s in sections.values())

        results.append({
            "source": "smartrecruiters",
            "company": company,
            "title": title,
            "location": location,
            "url": detail.get("postingUrl", f"https://jobs.smartrecruiters.com/{company}/{p['id']}"),
            "description_html": description,
            "posting_id": p["id"],
        })
    return results


def fetch_workable(account: str) -> list[dict]:
    url = f"https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    results = []
    for j in jobs:
        location = ", ".join(filter(None, [j.get("city", ""), j.get("state", ""), j.get("country", "")]))
        if j.get("telecommuting"):
            location = f"Remote ({location})" if location else "Remote"
        results.append({
            "source": "workable",
            "company": account,
            "title": j["title"],
            "location": location,
            "url": j.get("url", j.get("shortlink", "")),
            "description_html": j.get("description", ""),
            "posting_id": j["shortcode"],
        })
    return results


def fetch_jooble(api_key: str, keywords: str, location: str = "", pages: int = 1) -> list[dict]:
    """
    Jooble is a search-style job aggregator, not a per-company board -- you
    query it with keywords/location instead of a company token. Requires a
    free key from https://jooble.org/api/about, passed via JOOBLE_API_KEY.
    """
    url = f"https://jooble.org/api/{api_key}"
    results = []
    for page in range(1, pages + 1):
        payload = {"keywords": keywords, "location": location, "page": str(page)}
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            results.append({
                "source": "jooble",
                "company": j.get("company", ""),
                "title": j.get("title", ""),
                "location": j.get("location", ""),
                "url": j.get("link", ""),
                "description_html": j.get("snippet", ""),
                "posting_id": str(j.get("id", j.get("link", ""))),
            })
    return results


def fetch_adzuna(app_id: str, app_key: str, country: str, keywords: str,
                  results_per_page: int = 50, pages: int = 1) -> list[dict]:
    """
    Adzuna is another search-style aggregator, spanning many boards per
    country. Requires a free app_id/app_key from
    https://developer.adzuna.com, passed via ADZUNA_APP_ID / ADZUNA_APP_KEY.
    """
    results = []
    for page in range(1, pages + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": keywords,
            "results_per_page": results_per_page,
            "content-type": "application/json",
        }
        # Adzuna returns a bare 503 sporadically, with no Retry-After and no
        # pattern: on 2026-09-23 three of five queries failed while the other
        # two succeeded seconds apart, same key, same page size. A dropped
        # query used to cost the whole night's results for that search, and
        # neighbouring calls were already succeeding, so retry the transient
        # statuses briefly. Auth and bad-request failures are not retried --
        # those will fail identically every time.
        resp, attempts = None, 0
        for attempt in range(3):
            resp = requests.get(url, params=params, timeout=15)
            attempts += 1
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
            if attempt < 2:
                time.sleep(2 * (attempt + 1))        # 2s, then 4s

        try:
            resp.raise_for_status()
        except requests.HTTPError:
            # The request URL carries app_id and app_key as query params, and
            # requests puts the whole URL in the exception message -- which
            # wrote the key in plaintext into every nightly log. Status only.
            tries = f" after {attempts} attempts" if attempts > 1 else ""
            raise RuntimeError(
                f"Adzuna returned {resp.status_code} for query {keywords!r}{tries}"
            ) from None
        jobs = resp.json().get("results", [])
        if not jobs:
            break
        for j in jobs:
            results.append({
                "source": "adzuna",
                "company": j.get("company", {}).get("display_name", ""),
                "title": j.get("title", ""),
                "location": j.get("location", {}).get("display_name", ""),
                "url": j.get("redirect_url", ""),
                "description_html": j.get("description", ""),
                "posting_id": str(j.get("id", "")),
            })
    return results


def fetch_remotive() -> list[dict]:
    """
    Remotive lists remote-only postings and needs no API key. Since every
    listing is already remote, this is a good complement to the
    company-by-company boards for the "include remote roles" net.
    Remotive asks that you not poll this more than a few times a day --
    fine here since the pipeline is run manually, not on a schedule.
    """
    url = "https://remotive.com/api/remote-jobs"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "source": "remotive",
            "company": j.get("company_name", ""),
            "title": j.get("title", ""),
            "location": j.get("candidate_required_location", "") or "Remote",
            "url": j.get("url", ""),
            "description_html": j.get("description", ""),
            "posting_id": str(j.get("id", "")),
        }
        for j in jobs
    ]


def fetch_jobicy(industries: list[str], geo: str = "", count: int = 50) -> list[dict]:
    """
    Jobicy lists remote-only postings and needs no API key. Querying by
    industry is far more productive than the unfiltered feed -- testing
    showed the default feed returns almost no marketing-analytics roles,
    while industry=data-science returns them consistently. Also one of the
    few free sources that returns salary ranges.
    """
    results = []
    for industry in industries or [""]:
        params = {"count": count}
        if industry:
            params["industry"] = industry
        if geo:
            params["geo"] = geo
        resp = requests.get("https://jobicy.com/api/v2/remote-jobs", params=params,
                            headers={"User-Agent": "Mozilla/5.0 (job-search-script)"},
                            timeout=20)
        resp.raise_for_status()
        for j in resp.json().get("jobs", []):
            salary = ""
            if j.get("salaryMin") and j.get("salaryMax"):
                cur = j.get("salaryCurrency", "USD")
                per = j.get("salaryPeriod", "")
                salary = f" [{cur} {j['salaryMin']}-{j['salaryMax']} {per}]".rstrip() + "]"
                salary = salary.replace("]]", "]")
            # Every Jobicy listing is remote, but jobGeo reports the eligible
            # region ("USA") rather than saying so -- which the location
            # filter would otherwise reject. Mark it explicitly.
            geo_text = (j.get("jobGeo") or "").strip()
            location = f"Remote ({geo_text})" if geo_text else "Remote"
            results.append({
                "source": "jobicy",
                "company": j.get("companyName", ""),
                "title": j.get("jobTitle", ""),
                "location": location,
                "url": j.get("url", ""),
                "description_html": (j.get("jobDescription") or j.get("jobExcerpt") or "") + salary,
                "posting_id": str(j.get("id", "")),
            })
    return results


def fetch_careerjet(api_key: str, keywords: str, location: str = "usa",
                    pages: int = 1, page_size: int = 100) -> list[dict]:
    """
    Careerjet is a broad search aggregator. Free key from
    https://www.careerjet.com/partners/register/as-publisher, passed via
    CAREERJET_API_KEY. Auth is HTTP Basic with the key as username and an
    empty password. user_ip/user_agent are required by the API for
    attribution; they aren't used for filtering.
    """
    results = []
    ua = "Mozilla/5.0 (compatible; job-search-script)"
    for page in range(1, pages + 1):
        resp = requests.get(
            "https://search.api.careerjet.net/v4/query",
            params={
                "keywords": keywords,
                "location": location,
                "locale_code": "en_US",
                "page": page,
                "page_size": page_size,
                "sort": "date",
                "user_ip": "127.0.0.1",
                "user_agent": ua,
            },
            auth=(api_key, ""),
            headers={"User-Agent": ua},
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
        jobs = data.get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            salary = f" [{j.get('salary')}]" if j.get("salary") else ""
            results.append({
                "source": "careerjet",
                "company": j.get("company", ""),
                "title": j.get("title", ""),
                "location": j.get("locations", ""),
                "url": j.get("url", ""),
                "description_html": (j.get("description") or "") + salary,
                "posting_id": str(abs(hash(j.get("url", ""))))[:12],
            })
        if len(jobs) < page_size:
            break
    return results


def fetch_company_watchlist(api_key: str, companies: list[str], location: str = "",
                            pages: int = 1) -> list[dict]:
    """
    Watch specific employers regardless of which ATS they run.

    Some companies sit on systems with no usable public feed -- Hershey is on
    SAP SuccessFactors, which doesn't expose one -- so a per-ATS integration
    isn't possible. Jooble indexes them anyway, so query it by company name
    and keep only the hits whose company field actually matches. Broad
    keyword queries return plenty of neighbours (Coca-Cola, Barry Callebaut)
    when you ask for "Hershey", hence the post-filter.
    """
    results, seen = [], set()
    for company in companies:
        short = re.sub(r"[^a-z0-9]", "", company.lower()
                       .replace("the ", "").replace(" company", "")
                       .replace(" inc", "").replace(" corporation", ""))
        # Bare company name alone surfaces mostly unrelated roles, so pair it
        # with the terms that matter. Keeps the call count small and still
        # catches things like "Mgr Commercial Insights Shopper Strategy".
        queries = [company, f"{company} analytics", f"{company} insights",
                   f"{company} data"]
        for q in queries:
            for page in range(1, pages + 1):
                payload = {"keywords": q, "location": location, "page": str(page)}
                resp = requests.post(f"https://jooble.org/api/{api_key}", json=payload, timeout=20)
                resp.raise_for_status()
                for j in resp.json().get("jobs", []):
                    found = re.sub(r"[^a-z0-9]", "", (j.get("company") or "").lower())
                    # Prefix match in either direction. Substring matching let
                    # "Milton Hershey School" through on a Hershey watch, which
                    # is a different organisation entirely.
                    if not found or not (found.startswith(short) or short.startswith(found)):
                        continue
                    key = str(j.get("id", j.get("link", "")))
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                    "source": "watchlist",
                    "company": j.get("company", company),
                    "title": j.get("title", ""),
                    "location": j.get("location", ""),
                    "url": j.get("link", ""),
                    "description_html": j.get("snippet", ""),
                    "posting_id": str(j.get("id", j.get("link", ""))),
                })
    return results


def fetch_apify_linkedin(token: str, actor: str, searches: list[dict],
                         date_posted: str = "", limit_per_search: int = 25,
                         title_include: list[str] | None = None,
                         title_exclude: list[str] | None = None) -> list[dict]:
    """
    LinkedIn postings by way of an Apify actor.

    LinkedIn has no public jobs API and its terms forbid scraping, so this
    delegates to a paid third-party actor rather than fetching anything here.
    It bills per result, which is why title_include/title_exclude are pushed
    to the actor: filtering server-side means fewer results returned, fewer
    billed, and fewer junk postings reaching the scorer.

    The actor and its input live in boards.yaml, so swapping actors when one
    breaks (LinkedIn moved to AI-powered search in August 2026 and deprecated
    the old filter parameters) is a config change, not a code change.
    """
    endpoint = (f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}"
                f"/run-sync-get-dataset-items")
    results = []

    for search in searches:
        payload = {"limit": search.get("limit", limit_per_search)}
        if search.get("keywords"):
            payload["keywords"] = search["keywords"]
        if search.get("location"):
            payload["location"] = search["location"]
        if search.get("url_params"):
            payload["urlParam"] = [{"key": p["key"], "value": str(p["value"])}
                                   for p in search["url_params"]]
        if date_posted:
            payload["datePosted"] = date_posted
        if title_include:
            payload["titleInclude"] = title_include
        if title_exclude:
            payload["titleExclude"] = title_exclude

        label = f"{search.get('keywords','')} / {search.get('location','anywhere')}"
        # the sync endpoint holds the connection while the actor runs
        resp = requests.post(endpoint, json=payload,
                             headers={"Authorization": f"Bearer {token}"},
                             timeout=310)
        if resp.status_code >= 400:
            # the token rides in the Authorization header, not the URL, so the
            # status line is safe to log as-is
            raise RuntimeError(f"Apify returned {resp.status_code} for "
                               f"search {label!r}: {resp.text[:120]}")

        for j in resp.json():
            job_id = str(j.get("id") or j.get("jobId") or "")
            url = (j.get("url") or j.get("link") or j.get("jobUrl")
                   or (f"https://www.linkedin.com/jobs/view/{job_id}" if job_id else ""))
            description = j.get("description") or j.get("descriptionText") or ""
            # the scorer reads pay ranges out of the description text, and this
            # actor returns salary as its own field
            if j.get("salary"):
                description = f"Salary: {j['salary']}\n\n{description}"
            results.append({
                "source": "linkedin",
                "company": j.get("companyName") or j.get("company") or "",
                "title": j.get("title", ""),
                "location": j.get("location", ""),
                "url": url,
                "description_html": description,
                "posting_id": job_id or hashlib.sha1(url.encode()).hexdigest()[:12],
            })

    return results


def _indeed_pay_range(pay) -> tuple[int, int, str] | None:
    """
    Pull (low, high, unit) out of an Indeed baseSalary, whose shape varies.

    Most postings give flat min/max, but some nest them schema.org style under
    value/minValue/maxValue. The flat reading crashed on the nested form, so
    read both and give up quietly rather than guessing.
    """
    if not isinstance(pay, dict):
        return None
    inner = pay.get("value") if isinstance(pay.get("value"), dict) else pay
    low = inner.get("min", inner.get("minValue"))
    high = inner.get("max", inner.get("maxValue"))
    unit = (inner.get("unitOfWork") or inner.get("unitText")
            or pay.get("unitOfWork") or "YEAR")
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return int(low), int(high), str(unit).lower()


def fetch_apify_indeed(token: str, actor: str, searches: list[dict],
                       country: str = "us", date_posted: str = "",
                       limit_per_search: int = 25) -> list[dict]:
    """
    Indeed postings by way of a paid Apify actor, on the same terms as
    LinkedIn: Indeed's own terms forbid scraping, so a third party does it and
    bills per result.

    Three things differ from the LinkedIn actor, all established by probing it
    rather than from its documentation, which names none of these fields:

      - location "remote" actually filters to remote roles, and datePosted
        actually narrows the window. Neither worked on LinkedIn.
      - there is no titleInclude, so whatever comes back is billed. What takes
        its place is Indeed's own query syntax, which passes through: a title
        term of title:((phrase OR phrase) AND (director OR head)) filters at
        the actor, before billing. A plain query matches posting bodies, so it
        billed four irrelevant results for every good one. See boards.yaml.
      - country must be lowercase; "US" is rejected with a 400.

    Shapes: employer and location are objects, description is {text, html},
    salary arrives as a structured baseSalary, and jobUrl points at the
    employer's own ATS while url points back at Indeed.
    """
    endpoint = (f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}"
                f"/run-sync-get-dataset-items")
    results = []

    for search in searches:
        payload = {"country": country,
                   "limit": search.get("limit", limit_per_search)}
        if search.get("title"):
            payload["title"] = search["title"]
        if search.get("location"):
            payload["location"] = search["location"]
        if date_posted:
            payload["datePosted"] = str(date_posted)

        label = f"{search.get('title','')} / {search.get('location','anywhere')}"
        resp = requests.post(endpoint, json=payload,
                             headers={"Authorization": f"Bearer {token}"},
                             timeout=310)
        if resp.status_code >= 400:
            raise RuntimeError(f"Apify returned {resp.status_code} for Indeed "
                               f"search {label!r}: {resp.text[:120]}")

        for j in resp.json():
            loc = j.get("location") or {}
            city = (loc.get("city") or "").strip()
            state = (loc.get("admin1Code") or "").strip()
            where = city if city.lower() == "remote" else ", ".join(p for p in (city, state) if p)

            # description is an object of {text, html}, not a string
            desc = j.get("description")
            if isinstance(desc, dict):
                description = desc.get("html") or desc.get("text") or ""
            else:
                description = desc or ""

            pay = _indeed_pay_range(j.get("baseSalary"))
            if pay:
                # the scorer reads pay ranges out of the description text, and
                # it needs the unit: it discards a range followed by "per hour"
                # and would otherwise read $50-$65 as $50K-$65K
                low, high, unit = pay
                description = (f"Salary: ${low:,} - ${high:,} per {unit}\n\n"
                               + description)

            key = str(j.get("key") or j.get("refNum") or "")
            results.append({
                "source": "indeed",
                "company": (j.get("employer") or {}).get("name", ""),
                "title": j.get("title", ""),
                "location": where or (loc.get("countryName") or ""),
                # jobUrl is the employer's own posting; prefer it so an apply
                # link is real and so a posting already pulled from that ATS
                # can dedupe against it
                "url": j.get("jobUrl") or j.get("url") or "",
                "description_html": description,
                "posting_id": key or hashlib.sha1(
                    (j.get("url") or j.get("title", "")).encode()).hexdigest()[:12],
            })

    return results


HTML_TAG = re.compile(r"<[^>]+>")
URL_IN_TEXT = re.compile(r"https?://[^\s\"'>)\]]+")
# Boards worth linking to when an email mentions one. LinkedIn alert links are
# tracking redirects, so they are kept only as a last resort.
REAL_BOARD = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|smartrecruiters\.com|"
    r"workable\.com|icims\.com|taleo\.net|oraclecloud\.com|successfactors|jobvite\.com|"
    r"careers?\.[a-z0-9-]+\.[a-z]{2,})", re.I)

# Subjects that are about an application already sent, or an interview already
# arranged. These are not openings, and no corroboration is needed to know it:
# a posting alert never says "your application".
#
# This exists because tying a reply to a tracker row -- the only test there used
# to be -- cannot work for the mail that matters. An acknowledgement arrives
# through the ATS, so its sender is jobvite.com or myworkday.com and its
# employer name is one company_keys() deliberately discards as a generic host,
# and the row has to carry a submit date you remembered to type. Four of these
# were scored as jobs in one night: an acknowledgement sent through Workday
# reached 6/10, and an interview invitation reached 8/10 and had a resume and
# cover letter drafted for it.
#
# Checked against 243 real subjects from a three-week window: 20 matched, all
# of them genuinely replies, and nothing that mentions interviews in passing
# ("How to land a job interview!", "AI Interview Buddy") was touched.
APPLICATION_SUBJECT = [re.compile(p, re.I) for p in (
    r"thank(?:s| you) for (?:applying|your application)",
    r"thank(?:s| you) for (?:your )?interest in",
    # "your application", and "your Data Scientist application to Acme Corp"
    r"\byour\b(?:\s+\S+){0,4}\s+application\b",
    r"\bapplication\b[^.]{0,20}\b(?:received|submitted|confirmation|status|update)\b",
    r"\bapplication (?:to|for|with)\b",
    r"\binterview\b[^.]{0,30}\b(?:confirmation|invitation|invite|scheduled|scheduling|reminder|availability)\b",
    r"\b(?:schedul\w+|confirm\w*|reschedul\w+|invitation|invite) (?:your |the |an |a )?(?:\w+ ){0,2}interview\b",
    # A reply about a conversation that already happened. One of these reached
    # 8/10 and had a resume drafted for it: "RE: [EXTERNAL] Thank you for the
    # call today - Director, Marketing Analytics - <name>". It mentions neither
    # an application nor an interview, so every rule above missed it.
    r"thank(?:s| you) for (?:the|your|our) "
    r"(?:call|time|conversation|chat|meeting|discussion)",
)]


def _candidate_name_pattern(name: str | None = None) -> re.Pattern | None:
    """
    Your own full name in the subject, next to a sign that this is a thread you
    are already in.

    Two shapes, both seen in real mail. An interview subject carries the name:
    "<Employer> Interview | <Your Name> | <Role>". And a reply carries it after
    an Re:/Fwd: prefix: "RE: [EXTERNAL] Thank you for the call today -
    Director, Marketing Analytics - <Your Name>". Nothing else in an inbox does
    either -- a job alert addresses you by first name at most -- so the full
    name plus one of those markers is specific enough to drop the message.

    The name alone is not enough: a recruiter pitching a genuinely new role
    writes "<Your Name> - Director Marketing Analytics opportunity", and that is
    a posting worth scoring.

    Read from the profile rather than hardcoded, so this works for whoever
    runs it. No name, no rule: the other patterns still apply.

    `name` is for tests, so they can exercise this rule rather than restate it.
    A test that builds its own copy of the pattern passes while the real one is
    out of date, which is what happened the first time this grew a branch.
    """
    if name is None:
        try:
            profile = json.loads((Path(__file__).parent.parent / "profile" /
                                  "master_profile.json").read_text(encoding="utf-8"))
            name = (profile.get("name") or "").strip()
        except Exception:
            return None
    name = name.strip()
    if len(name.split()) < 2:
        return None                 # a first name alone matches every job alert
    who = re.escape(name)
    return re.compile(rf"\binterview\b.*{who}"
                      rf"|{who}.*\binterview\b"
                      rf"|^\s*(?:re|fw|fwd)\s*:.*{who}", re.I)


_CANDIDATE_INTERVIEW = _candidate_name_pattern()


def is_application_subject(subject: str) -> bool:
    """True when the subject is about an application or interview, not a job."""
    subject = subject or ""
    if any(p.search(subject) for p in APPLICATION_SUBJECT):
        return True
    return bool(_CANDIDATE_INTERVIEW and _CANDIDATE_INTERVIEW.search(subject))


# "Acme is hiring a Director of Analytics"  ->  company, title
ALERT_HIRING = re.compile(r"^(.+?)\s+is hiring an?\s+(.+)$", re.I)
# "Director of Analytics at Acme: up to $189K/year"  ->  title, company
ALERT_AT = re.compile(r"^(.+?)\s+at\s+([^:]+?)(?::.*)?$", re.I)


def _decode_header(raw: str) -> str:
    """
    Subjects arrive RFC2047-encoded ('=?UTF-8?B?...?=') more often than not.

    Long headers are also folded across lines, so a decoded subject can carry
    a CRLF mid-sentence. The subject patterns below are line-anchored, so an
    unflattened subject silently fails to parse and the company falls back to
    the sender's domain ("linkedin" instead of the employer). Collapse first.
    """
    from email.header import decode_header, make_header
    try:
        decoded = str(make_header(decode_header(raw or "")))
    except Exception:
        decoded = raw or ""
    return " ".join(decoded.split())


def _message_text(msg) -> str:
    """Plain text body, falling back to HTML with the tags stripped."""
    plain, html_body = [], []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        (plain if ctype == "text/plain" else html_body).append(text)

    text = "\n".join(plain) or HTML_TAG.sub(" ", "\n".join(html_body))
    text = html.unescape(text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text)).strip()


def imap_messages(host: str, user: str, password: str, mailbox: str = "INBOX",
                  since_days: int = 3, max_messages: int = 60,
                  gmail_query: str | None = None):
    """
    Read recent messages. Shared by the job-posting source and the reply
    checker, so there is one place that knows how to talk to a mailbox.

    Read-only by construction: the mailbox is selected with readonly=True, so
    nothing is ever marked read, moved, or deleted.

    Yields dicts of sender, subject, body, date, message_id, uid.
    """
    import imaplib
    from email import message_from_bytes
    from email.utils import parseaddr, parsedate_to_datetime

    # Google shows app passwords as "abcd efgh ijkl mnop"; pasted with the
    # spaces, the login is rejected.
    password = (password or "").replace(" ", "")
    since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")

    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        conn.select(mailbox, readonly=True)

        # A plain SINCE search returns everything, and max_messages then keeps
        # only the newest N -- which on a busy inbox (40+ a day here) means a
        # 45-day window really reached back about four days, and older replies
        # were never seen. Gmail's X-GM-RAW runs the search server-side, so
        # only matching mail is fetched.
        status, data = "NO", None
        if gmail_query:
            try:
                # The query carries its own double quotes, so it cannot be
                # passed as a plain argument -- imaplib mangles it and the
                # search fails, which silently fell back to fetching the whole
                # inbox. Send it as a literal instead.
                conn.literal = gmail_query.encode("utf-8")
                status, data = conn.search("UTF-8", "X-GM-RAW")
            except Exception as exc:
                print(f"  [warn] Gmail server-side search failed ({type(exc).__name__}: "
                      f"{str(exc)[:60]}); falling back to a date-only search, which "
                      f"may miss older mail")
                status = "NO"
        if status != "OK":
            if gmail_query:
                print("  [warn] Gmail server-side search unavailable; "
                      "falling back to a date-only search")
            status, data = conn.search(None, f"(SINCE {since})")
        if status != "OK":
            return
        for uid in (data[0] or b"").split()[-max_messages:]:
            status, payload = conn.fetch(uid, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            msg = message_from_bytes(payload[0][1])
            try:
                when = parsedate_to_datetime(msg.get("Date", ""))
            except Exception:
                when = None
            yield {
                "sender": (parseaddr(msg.get("From", ""))[1] or "").lower(),
                "subject": _decode_header(msg.get("Subject", "")).strip(),
                "body": _message_text(msg),
                "date": when,
                "message_id": (msg.get("Message-ID") or "").strip("<> "),
                "uid": uid.decode(errors="replace"),
            }
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def fetch_email(host: str, user: str, password: str, mailbox: str = "INBOX",
                since_days: int = 3, include_senders: list[str] | None = None,
                exclude_senders: list[str] | None = None,
                max_messages: int = 60, max_chars: int = 6000) -> list[dict]:
    """
    Postings that arrived by email: recruiter outreach and job alerts.

    This is the one source that finds roles no board exposes -- a recruiter
    writing to you directly is not listed anywhere. Read-only: the mailbox is
    opened with readonly=True, so nothing is marked read, moved, or deleted.

    Needs an app password, not your account password (Google requires 2FA,
    then myaccount.google.com/apppasswords). Restrict include_senders to the
    addresses worth reading; the default pulls your whole inbox.

    NOTE: message bodies are untrusted text written by strangers. They are
    treated as posting descriptions and nothing else -- never as instructions.
    """
    include = [s.lower() for s in (include_senders or [])]
    exclude = [s.lower() for s in (exclude_senders or [])]
    results: list[dict] = []

    # A reply about an application you already sent is not a new opening. The
    # interview invitation arrived here as an 8/10 "role" once, because its
    # subject matched the title keywords. Reuse the
    # reply checker's own classifier and matcher rather than a second set of
    # phrases: skipping on the classifier alone would drop a recruiter asking
    # for your availability about a genuinely new job, so a message is only
    # dropped when it also ties to an application in the tracker.
    from check_replies import classify as classify_reply, match_row, parse_date
    tracker = Path(__file__).parent.parent / "output" / "application_tracker.csv"
    applied_rows: list[dict] = []
    if tracker.exists():
        try:
            import csv as _csv
            with open(tracker, newline="", encoding="utf-8-sig") as f:
                applied_rows = [r for r in _csv.DictReader(f)
                                if parse_date(r.get("date_submitted", ""))]
        except Exception as exc:
            print(f"  [warn] could not read the tracker to filter replies: "
                  f"{type(exc).__name__}")
    replies_skipped = 0
    subject_skips = 0

    for m in imap_messages(host, user, password, mailbox, since_days, max_messages):
        sender, subject, body = m["sender"], m["subject"], m["body"]
        if any(x in sender for x in exclude):
            continue
        if include and not any(x in sender for x in include):
            continue
        if not subject and not body:
            continue

        # Judge this on the subject alone. A reply announces itself there
        # ("Your application to...", "<Employer> Interview | ..."), while a real
        # posting mentions interviews and availability in its description: a
        # genuinely new opening was skipped as a reply because its body said
        # "our interview process has four stages" and an application to that
        # same employer was already on file.
        #
        # Some subjects settle it by themselves and need no tracker row to
        # corroborate them -- which is just as well, because the mail that
        # leaked is exactly the mail a tracker row cannot corroborate.
        if is_application_subject(subject):
            replies_skipped += 1
            subject_skips += 1
            continue

        # Weaker signals -- a bare "interview", a classifier hit on wording
        # that a recruiter might also use about a genuinely new role -- still
        # have to tie to something you actually applied to.
        looks_like_reply = (classify_reply(subject, "")
                            or re.search(r"\binterview\b", subject, re.I))
        if applied_rows and looks_like_reply:
            _row, score, _runner = match_row(m, applied_rows)
            if score >= 5:
                replies_skipped += 1
                continue

        company, title = "", subject
        parsed = ALERT_HIRING.match(subject)
        if parsed:
            company, title = parsed.group(1).strip(), parsed.group(2).strip()
        else:
            parsed = ALERT_AT.match(subject)
            if parsed:
                title, company = parsed.group(1).strip(), parsed.group(2).strip()
        if not company:
            # recruiter mail rarely names the employer in the subject
            company = sender.split("@")[-1].split(".")[0] or "email"

        links = [u for u in URL_IN_TEXT.findall(body) if REAL_BOARD.search(u)]
        url = links[0] if links else f"imap://{m['message_id'] or m['uid']}"

        results.append({
            "source": "email",
            "company": company[:80],
            "title": title[:140],
            "location": "",          # rarely stated; see collect_all_postings
            "url": url,
            "description_html": f"From: {sender}\nSubject: {subject}\n\n{body[:max_chars]}",
            "posting_id": hashlib.sha1(
                (m["message_id"] or subject + sender).encode()).hexdigest()[:12],
        })

    if replies_skipped:
        # say which rule did the work, so a leak can be diagnosed from the log
        matched = replies_skipped - subject_skips
        print(f"  email: skipped {replies_skipped} message(s) that are replies "
              f"about applications you already sent "
              f"({subject_skips} by subject, {matched} matched to the tracker)")
    return results


def _title_matches(title: str, keywords_lower: list[str]) -> bool:
    return any(k in title.lower() for k in keywords_lower)


# Postings write states both ways ("Pennsylvania" and "Hershey, PA"), and a
# plain substring test never connects them -- which silently dropped local
# roles. Abbreviations are matched with word boundaries so "PA" doesn't hit
# "Tampa" or "Palo Alto".
STATE_ABBREV = {
    "pennsylvania": "pa", "new york": "ny", "california": "ca", "texas": "tx",
    "florida": "fl", "illinois": "il", "massachusetts": "ma", "washington": "wa",
    "colorado": "co", "georgia": "ga", "north carolina": "nc", "new jersey": "nj",
    "virginia": "va", "ohio": "oh", "michigan": "mi", "arizona": "az",
    "maryland": "md", "minnesota": "mn", "utah": "ut", "oregon": "or",
    "connecticut": "ct", "delaware": "de", "indiana": "in", "tennessee": "tn",
    "wisconsin": "wi", "missouri": "mo", "nevada": "nv",
}


def _location_matches(loc: str, locations_lower: list[str]) -> bool:
    if "remote" in loc:
        return True
    for want in locations_lower:
        if want in loc:
            return True
        abbrev = STATE_ABBREV.get(want)
        if abbrev and re.search(rf"\b{abbrev}\b", loc):
            return True
        # also handle the config itself being an abbreviation
        if len(want) <= 3 and re.search(rf"\b{re.escape(want)}\b", loc):
            return True
    return False


def _title_excluded(title: str, excludes_lower: list[str]) -> bool:
    """
    Drop junior and unrelated roles that broad stems drag in.

    Phrase-level on purpose. A bare "associate" would delete every Associate
    Director posting, which is a real target level, and a bare "specialist"
    would delete senior specialist roles. Match the whole phrase that makes a
    title junior, not the word that merely appears in one.
    """
    return any(x in title.lower() for x in excludes_lower)


def keyword_filter(jobs: list[dict], keywords: list[str], locations: list[str],
                   exclude_titles: list[str] | None = None) -> list[dict]:
    keywords_lower = [k.lower() for k in keywords]
    locations_lower = [l.lower() for l in locations] if locations else []
    excludes_lower = [x.lower() for x in (exclude_titles or [])]

    def matches(job):
        if not _title_matches(job["title"], keywords_lower):
            return False
        if excludes_lower and _title_excluded(job["title"], excludes_lower):
            return False
        if not locations_lower:
            return True
        return _location_matches(job.get("location", "").lower(), locations_lower)
    return [j for j in jobs if matches(j)]


def person_config(person) -> dict:
    """
    The boards to search for one person of the hosted product: only the
    companies they listed, with their locations and blocked companies.

    The aggregator searches in boards.yaml (LinkedIn through Apify, Jooble,
    Adzuna, Careerjet, the company watchlist, the email inbox) carry one
    person's queries and the operator's paid keys, so they are switched off
    here rather than run with someone else's search terms. Remotive and
    Jobicy default to on, and Jobicy's default category is data science, so
    both are switched off explicitly too.
    """
    cfg = dict(person.boards)
    for source in ("jooble", "adzuna", "careerjet", "apify_linkedin", "apify_indeed",
                   "company_watchlist", "email", "remotive", "jobicy"):
        cfg[source] = {"enabled": False}
    return cfg


def collect_all_postings() -> list[dict]:
    import user_config
    from settings import ConfigError
    person = user_config.current()
    if person is not None:
        cfg = person_config(person)
        if not person.include_titles:
            raise ConfigError("titles: add at least one job title to search for")
        title_keywords, exclude_titles = person.include_titles, person.exclude_titles
    else:
        cfg = load_config()
        # config/titles.csv wins when present; boards.yaml is the fallback, so
        # an existing setup keeps working until its owner moves the lists over.
        csv_titles = load_titles()
        if csv_titles:
            title_keywords, exclude_titles = csv_titles
        else:
            title_keywords = cfg.get("title_keywords") or []
            exclude_titles = cfg.get("exclude_title_keywords") or []
    all_jobs = []
    for token in cfg.get("greenhouse") or []:
        try:
            all_jobs.extend(fetch_greenhouse(token))
        except Exception as e:
            print(f"[warn] greenhouse/{token} failed: {e}")
    for token in cfg.get("lever") or []:
        try:
            all_jobs.extend(fetch_lever(token))
        except Exception as e:
            print(f"[warn] lever/{token} failed: {e}")
    for wd in cfg.get("workday") or []:
        try:
            all_jobs.extend(fetch_workday(wd["tenant"], wd["host"], wd["site"]))
        except Exception as e:
            print(f"[warn] workday/{wd.get('tenant')} failed: {e}")
    for board in cfg.get("ashby") or []:
        try:
            all_jobs.extend(fetch_ashby(board))
        except Exception as e:
            print(f"[warn] ashby/{board} failed: {e}")
    for company in cfg.get("smartrecruiters") or []:
        try:
            all_jobs.extend(fetch_smartrecruiters(company, title_keywords))
        except Exception as e:
            print(f"[warn] smartrecruiters/{company} failed: {e}")
    for account in cfg.get("workable") or []:
        try:
            all_jobs.extend(fetch_workable(account))
        except Exception as e:
            print(f"[warn] workable/{account} failed: {e}")

    jooble_cfg = cfg.get("jooble") or {}
    if jooble_cfg.get("enabled"):
        api_key = os.environ.get("JOOBLE_API_KEY")
        if not api_key:
            print("[warn] jooble enabled but JOOBLE_API_KEY not set -- skipping")
        else:
            location = jooble_cfg.get("location", "")
            pages = jooble_cfg.get("pages_per_query", 1)
            for query in jooble_cfg.get("queries") or []:
                try:
                    all_jobs.extend(fetch_jooble(api_key, query, location, pages))
                except Exception as e:
                    print(f"[warn] jooble query '{query}' failed: {e}")

    adzuna_cfg = cfg.get("adzuna") or {}
    if adzuna_cfg.get("enabled"):
        app_id = os.environ.get("ADZUNA_APP_ID")
        app_key = os.environ.get("ADZUNA_APP_KEY")
        if not (app_id and app_key):
            print("[warn] adzuna enabled but ADZUNA_APP_ID/ADZUNA_APP_KEY not set -- skipping")
        else:
            country = adzuna_cfg.get("country", "us")
            results_per_page = adzuna_cfg.get("results_per_page", 50)
            pages = adzuna_cfg.get("pages_per_query", 1)
            for query in adzuna_cfg.get("queries") or []:
                try:
                    all_jobs.extend(fetch_adzuna(app_id, app_key, country, query, results_per_page, pages))
                except Exception as e:
                    print(f"[warn] adzuna query '{query}' failed: {e}")

    remotive_cfg = cfg.get("remotive") or {}
    if remotive_cfg.get("enabled", True):
        try:
            all_jobs.extend(fetch_remotive())
        except Exception as e:
            print(f"[warn] remotive failed: {e}")

    careerjet_cfg = cfg.get("careerjet") or {}
    if careerjet_cfg.get("enabled"):
        api_key = os.environ.get("CAREERJET_API_KEY")
        if not api_key:
            print("[warn] careerjet enabled but CAREERJET_API_KEY not set -- skipping")
        else:
            location = careerjet_cfg.get("location", "usa")
            pages = careerjet_cfg.get("pages_per_query", 1)
            page_size = careerjet_cfg.get("page_size", 100)
            for query in careerjet_cfg.get("queries") or []:
                try:
                    all_jobs.extend(fetch_careerjet(api_key, query, location, pages, page_size))
                except Exception as e:
                    print(f"[warn] careerjet query '{query}' failed: {e}")

    jobicy_cfg = cfg.get("jobicy") or {}
    if jobicy_cfg.get("enabled", True):
        try:
            all_jobs.extend(fetch_jobicy(
                jobicy_cfg.get("industries") or ["data-science"],
                jobicy_cfg.get("geo", ""),
                jobicy_cfg.get("count", 50),
            ))
        except Exception as e:
            print(f"[warn] jobicy failed: {e}")

    watch_cfg = cfg.get("company_watchlist") or {}
    if watch_cfg.get("enabled") and watch_cfg.get("companies"):
        api_key = os.environ.get("JOOBLE_API_KEY")
        if not api_key:
            print("[warn] company_watchlist needs JOOBLE_API_KEY -- skipping")
        else:
            try:
                hits = fetch_company_watchlist(
                    api_key, watch_cfg["companies"],
                    watch_cfg.get("location", ""), watch_cfg.get("pages_per_company", 1))
                print(f"  watchlist: {len(hits)} posting(s) across "
                      f"{len(watch_cfg['companies'])} watched compan(ies)")
                all_jobs.extend(hits)
            except Exception as e:
                print(f"[warn] company_watchlist failed: {e}")

    apify_cfg = cfg.get("apify_linkedin") or {}
    if apify_cfg.get("enabled") and apify_cfg.get("searches"):
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            print("[warn] apify_linkedin needs APIFY_TOKEN -- skipping")
        else:
            try:
                hits = fetch_apify_linkedin(
                    token,
                    apify_cfg.get("actor", "valig/linkedin-jobs-scraper"),
                    apify_cfg["searches"],
                    date_posted=apify_cfg.get("date_posted", ""),
                    limit_per_search=apify_cfg.get("limit_per_search", 25),
                    # bills per result, so filter at the actor rather than here
                    title_include=(apify_cfg.get("title_include")
                                   or title_keywords),
                    title_exclude=(apify_cfg.get("title_exclude")
                                   or exclude_titles),
                )
                print(f"  linkedin (apify): {len(hits)} posting(s) across "
                      f"{len(apify_cfg['searches'])} search(es)")
                all_jobs.extend(hits)
            except Exception as e:
                print(f"[warn] apify_linkedin failed: {type(e).__name__}: {str(e)[:110]}")

    indeed_cfg = cfg.get("apify_indeed") or {}
    if indeed_cfg.get("enabled") and indeed_cfg.get("searches"):
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            print("[warn] apify_indeed needs APIFY_TOKEN -- skipping")
        else:
            try:
                hits = fetch_apify_indeed(
                    token,
                    indeed_cfg.get("actor", "valig/indeed-jobs-scraper"),
                    indeed_cfg["searches"],
                    country=indeed_cfg.get("country", "us"),
                    date_posted=indeed_cfg.get("date_posted", ""),
                    limit_per_search=indeed_cfg.get("limit_per_search", 25),
                )
                print(f"  indeed (apify): {len(hits)} posting(s) across "
                      f"{len(indeed_cfg['searches'])} search(es)")
                all_jobs.extend(hits)
            except Exception as e:
                print(f"[warn] apify_indeed failed: {type(e).__name__}: {str(e)[:110]}")

    email_cfg = cfg.get("email") or {}
    if email_cfg.get("enabled"):
        user = os.environ.get("IMAP_USER")
        password = os.environ.get("IMAP_APP_PASSWORD")
        if not (user and password):
            print("[warn] email source needs IMAP_USER and IMAP_APP_PASSWORD -- skipping")
        else:
            try:
                hits = fetch_email(
                    email_cfg.get("host", "imap.gmail.com"), user, password,
                    mailbox=email_cfg.get("mailbox", "INBOX"),
                    since_days=email_cfg.get("since_days", 3),
                    include_senders=email_cfg.get("include_senders") or [],
                    exclude_senders=email_cfg.get("exclude_senders") or [],
                    max_messages=email_cfg.get("max_messages", 60),
                )
                print(f"  email: {len(hits)} message(s) from watched senders")
                all_jobs.extend(hits)
            except Exception as e:
                print(f"[warn] email source failed: {type(e).__name__}: {str(e)[:90]}")

    excluded = [re.sub(r"[^a-z0-9]", "", c.lower())
                for c in (cfg.get("exclude_companies") or [])]
    if excluded:
        before = len(all_jobs)
        all_jobs = [j for j in all_jobs
                    if not any(x in re.sub(r"[^a-z0-9]", "", (j.get("company") or "").lower())
                               for x in excluded)]
        dropped = before - len(all_jobs)
        if dropped:
            print(f"  excluded {dropped} posting(s) from {len(excluded)} blocked compan(ies)")

    deduped = []
    seen = set()
    for job in all_jobs:
        key = (job["source"], job["company"], job["posting_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)

    # Email postings were addressed to you personally and almost never state a
    # location in the subject line, so the location filter would drop every one
    # of them. They still go through title_keywords, which is the cost control.
    emails = [j for j in deduped if j.get("source") == "email"]
    linkedin = [j for j in deduped if j.get("source") == "linkedin"]
    indeed = [j for j in deduped if j.get("source") == "indeed"]
    others = [j for j in deduped
              if j.get("source") not in ("email", "linkedin", "indeed")]

    filtered = keyword_filter(others, title_keywords, cfg.get("locations", []),
                              exclude_titles)
    if exclude_titles:
        without = keyword_filter(others, title_keywords,
                                 cfg.get("locations", []))
        dropped = len(without) - len(filtered)
        if dropped:
            print(f"  excluded {dropped} junior/unrelated title(s) "
                  f"from {len(exclude_titles)} exclusion term(s)")

    if emails:
        kw = [k.lower() for k in title_keywords]
        ex = [x.lower() for x in exclude_titles]
        kept = [j for j in emails
                if (not kw or _title_matches(j.get("title", ""), kw))
                and not (ex and _title_excluded(j.get("title", ""), ex))]
        if kept:
            print(f"  email: {len(kept)} of {len(emails)} message(s) look like relevant roles")
        filtered.extend(kept)

    if linkedin:
        # LinkedIn labels a remote or nationwide posting "United States"
        # rather than "Remote" -- the plain location filter rejects exactly
        # the postings worth having, after they've already been paid for.
        # Treat a whole-country location as remote-compatible and let the
        # scorer judge; it evaluates location fit anyway.
        locs = [l.lower() for l in (cfg.get("locations") or [])]
        nationwide = ("united states", "usa", "u.s.", "nationwide", "anywhere")

        def wanted(job):
            loc = (job.get("location") or "").lower()
            if not locs:
                return True
            # "VP, Marketing Data Strategy (Remote)" carried a Wilmington, DE
            # location and was dropped as out-of-area. When the title says
            # remote, believe the title.
            if "remote" in (job.get("title") or "").lower():
                return True
            return (_location_matches(loc, locs)
                    or any(n in loc for n in nationwide))

        kept = [j for j in keyword_filter(linkedin, title_keywords,
                                          [], exclude_titles) if wanted(j)]

        # LinkedIn lists one posting per city, so a single remote role arrives
        # four times with four different job ids. The global dedupe keys on
        # posting_id and lets them all through, which means paying to score the
        # same job repeatedly. Keep the first of each company+title.
        seen_roles, unique = set(), []
        for j in kept:
            key = ((j.get("company") or "").strip().lower(),
                   (j.get("title") or "").strip().lower())
            if key in seen_roles:
                continue
            seen_roles.add(key)
            unique.append(j)

        dupes = len(kept) - len(unique)
        print(f"  linkedin: {len(unique)} of {len(linkedin)} posting(s) kept "
              f"after title and location filters"
              + (f" ({dupes} same role in another city)" if dupes else ""))
        filtered.extend(unique)

    if indeed:
        # Indeed's own location filter works, so unlike LinkedIn these carry a
        # real "Remote" or "City, ST" and the ordinary filter is correct. What
        # it shares with LinkedIn is duplicates: the same role comes back
        # several times under different job keys, so keep one per company+title.
        kept = keyword_filter(indeed, title_keywords, cfg.get("locations", []),
                              exclude_titles)
        seen_roles, unique = set(), []
        for j in kept:
            key = ((j.get("company") or "").strip().lower(),
                   (j.get("title") or "").strip().lower())
            if key in seen_roles:
                continue
            seen_roles.add(key)
            unique.append(j)

        dupes = len(kept) - len(unique)
        print(f"  indeed: {len(unique)} of {len(indeed)} posting(s) kept "
              f"after title and location filters"
              + (f" ({dupes} duplicate listing(s))" if dupes else ""))
        filtered.extend(unique)

    return filtered


if __name__ == "__main__":
    postings = collect_all_postings()
    print(f"Found {len(postings)} matching postings:")
    for p in postings:
        print(f" - [{p['company']}] {p['title']} ({p['location']}) -> {p['url']}")
