# Job Application Assistant

Searches Greenhouse/Lever job boards, scores fit, drafts tailored resumes,
and pre-fills application forms for your review. It never auto-submits --
you always click the final button yourself.

## Setup

```bash
cd job-app-assistant
python -m venv venv && source venv/bin/activate
pip install requests pyyaml anthropic

export ANTHROPIC_API_KEY=your_key_here

# for rendering resumes to .docx:
npm install docx

# optional -- only needed if you enable the jooble/adzuna aggregators below
export JOOBLE_API_KEY=your_key_here     # free key: https://jooble.org/api/about
export ADZUNA_APP_ID=your_app_id_here   # free key: https://developer.adzuna.com
export ADZUNA_APP_KEY=your_app_key_here
```

## 1. Fill in your profile

The profile lives in two files:

**`profile/master_profile.json`** -- work history, education, publications,
certifications, positioning notes. The more specific and metric-driven the
achievements, the better the tailored resumes. Don't invent numbers; the
tailoring step is told not to fabricate anything not present here.

**`profile/skills_inventory.csv`** -- **the source of truth for skills.**
Edit this in Excel/Sheets rather than hand-editing JSON. Columns:

| column | meaning |
|---|---|
| `category` | grouping (e.g. Marketing Measurement, Languages & Query). Rows in a `Certifications` category are routed to the certifications list instead. |
| `skill` | the skill itself |
| `have_it` | `yes` puts it on resumes. Anything else (`no`) excludes it -- use for development targets you don't yet have. |
| `proficiency` | optional (Expert / Advanced / Working / Familiar). Appended in parentheses so the tailoring step can lead with real strengths. |
| `source` | where it came from; informational only |
| `notes` | optional free text, e.g. "used at Deutsch LA"; appended in brackets |

When this CSV is present it **replaces** the `skills` and `technical_stack`
fields in the JSON, so the model never sees two contradictory skill lists.
Add rows freely -- new categories are picked up automatically. If the file
is missing or has no `have_it=yes` rows, the pipeline falls back to the
`skills` array in `master_profile.json` and prints a warning.

To regenerate the CSV from the JSON (e.g. after a big JSON edit), there is
no script by design -- the CSV is authoritative once it exists, so
regenerating would discard your edits.

## 2. Configure which companies to search

Edit `config/boards.yaml`:
- Add Greenhouse/Lever/Ashby/SmartRecruiters/Workable company tokens (found
  in their careers page URL) and/or Workday tenants
- Adjust `title_keywords` to match the roles you want
- Adjust `locations`

SmartRecruiters postings are pre-filtered by `title_keywords` against the
listing endpoint before a per-posting detail fetch (their listing API doesn't
include the description) -- keep that in mind if you loosen keywords a lot,
since it multiplies the number of detail requests.

**Sources evaluated and rejected** (tested 2026-08-12 against real
marketing-analytics keywords, so they don't get re-litigated):

| Source | Result |
|---|---|
| Arbeitnow | 7 hits / 576 jobs, almost all Germany + UK. European board. |
| Himalayas | `title` search param appears to be ignored -- four different queries returned an identical 20 rows. |
| RemoteOK | 0 relevant hits; heavily engineering-focused. |
| The Muse | Public API returns near-empty results now (2 rows for "Data Science", 0 for "Business & Strategy"). |
| Dice | API shut down in 2017. |
| Indeed / Glassdoor / ZipRecruiter / Monster | Public APIs deprecated or partner-only. |
| LinkedIn | No public jobs API; ToS prohibits scraping. Use the CSV workflow below. |

Also configured: **Jooble** and **Adzuna** are search-style aggregators
(query by keyword instead of by company) covering many boards at once --
set `jooble.enabled` / `adzuna.enabled` to `true` and export the
corresponding API key env vars (see Setup above) to turn them on. Both are
free to sign up for. **Remotive** and **Jobicy** are remote-only and need no
key; both are on by default and are the main sources of remote-specific
listings alongside the `locations: ["Remote", ...]` filter that already
applies to every other source.

Note on Jobicy: query it **by industry**, not via the unfiltered feed. The
default feed returns almost no marketing-analytics roles, while
`industry: data-science` surfaces them consistently. Also note its `jobGeo`
field reports the eligible region ("USA") rather than saying "Remote", so
`fetch_jobicy` rewrites the location as `Remote (USA)` -- without that, the
location filter silently drops every Jobicy result.

## 3. Run the pipeline

```bash
cd src
python run_pipeline.py
```

This searches all configured boards, prints a fit score (0-10) for every
matching posting, and for anything above the threshold writes **both** a
tailored resume and a matching cover letter, each as JSON plus a formatted
Word doc:

```
output/Acme Inc_Director of Marketing Analytics_2026-08-12.json        <- resume
output/Acme Inc_Director of Marketing Analytics_2026-08-12.docx
output/Acme Inc_Director of Marketing Analytics_2026-08-12_cover.json  <- cover letter
output/Acme Inc_Director of Marketing Analytics_2026-08-12_cover.docx
```

Files are named `company_position title_<date>`, with a `-2`/`-3`/... suffix
added automatically in the rare case that exact company+title+date
combination is already taken. Check `output/scored_postings.json` for the
full list, including an `overqualification_risk` flag per posting.

The cover letter is drafted in a professional-but-enthusiastic tone, cites
concrete achievements pulled from `master_profile.json` (it's told not to
fabricate metrics or experience, same as the resume step), and references
specifics from the posting so it doesn't read as generic. **Read it before
sending** -- tone is subjective and it's going out under your name.

Rendering to `.docx` requires Node.js with the `docx` package available
(`npm install docx` in `src/` if the renderers can't find it). If Node
isn't set up, the pipeline still produces the JSON -- run
`node render_resume.js <in.json> <out.docx>` or
`node render_cover_letter.js <in_cover.json> <out_cover.docx>` manually
once it is.

## 3a. The application tracker

Every pipeline run refreshes `output/application_tracker.csv`. Open it in
Excel or Sheets; it's the working document for the search.

Columns split into two groups:

**Generated** (refreshed each run, don't bother editing): `score`,
`overqualified`, `company`, `title`, `location`, `source`, `url`, `resume`,
`cover_letter`, `first_seen`, `last_seen`.

**Yours** (never overwritten): `status`, `date_submitted`, `follow_up`,
`contact`, `notes`.

Rows are matched by posting URL, so you can fill in your columns, re-run
the pipeline, and keep everything you typed. Suggested `status` values:
leave blank for not applied, then `applied`, `screening`, `interview`,
`offer`, `rejected`, `passed`.

Rows are sorted by score, best first. A posting that disappears from the
job boards keeps its row and gets `[no longer listed as of <date>]`
prepended to its notes, so an application you already sent never silently
vanishes.

Run it standalone with a summary of what's ready to apply to and what's
already submitted:

```bash
python build_tracker.py --report
```

## 3b. Score a one-off posting (e.g. LinkedIn)

LinkedIn doesn't expose a public API the way Greenhouse/Lever/Workday do, so
`scraper.py` can't reach it. For a posting you found there (or anywhere else),
copy the details yourself and score it manually:

```bash
python score_manual.py --company "Acme Inc" --title "Director, Marketing Analytics" \
    --location "Remote, USA" --url "https://linkedin.com/jobs/view/1234567890" \
    --description-file posting.txt
```

(`--description "<text>"` also works in place of `--description-file` for short
postings.) This scores it, tailors + renders a resume if it clears the
threshold, and merges the result into `output/scored_postings.json` alongside
everything else.

### Batch mode via a persistent CSV

There's a standing CSV at the project root -- `../linkedin_postings.csv`
(i.e. `Automated Resume Builder/linkedin_postings.csv`, one level above
`job-app-assistant/`) -- with 5 columns: `company, title, location, url,
description`. Add a row there any time you find a posting (LinkedIn or
otherwise), then just run:

```bash
python score_batch.py
```

with no arguments -- it defaults to that file. It scores + tailors only rows
whose `url` isn't already in `output/scored_postings.json`, so re-running
after adding new rows only spends API calls on what's actually new. Pass a
different path (`python score_batch.py other.csv`) to use a one-off file
instead.

## 4. Review resumes + cover letters, then apply

Open each tailored `.docx` in `output/` -- both the resume and the
`_cover.docx` -- and edit as needed, especially anything marked
`[FILL IN: ...]`. Then apply on the employer's own site and record the
submit date in `application_tracker.csv`.

The pipeline stops here on purpose. An autofill step (`form_filler.py`) was
prototyped and removed in September 2026: it never worked reliably, since
application forms differ per employer even on the same ATS. The Simplify
browser extension handles autofill better, using `output/<your name> -
Resume (Master).docx`.

## Notes / next steps

- **Workday** is wired up with a best-effort fetcher (`fetch_workday` in
  `scraper.py`) using the internal CXS endpoint pattern most tenants use.
  Behavior varies more by company than Greenhouse/Lever -- if a tenant
  returns nothing, open that company's careers page devtools Network tab,
  find the real `jobs` request, and adjust the payload shape to match.
- This deliberately stops short of form filling, auto-submit, and CAPTCHA
  handling -- both for site ToS reasons and because a human check on the
  final application is genuinely worth keeping.
