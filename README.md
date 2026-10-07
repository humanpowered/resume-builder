# Resume Builder

Builds a **master record** of someone's career. It covers every job, what they
achieved in each one with the numbers behind it, and every skill tied to the
work that proves it. Then it hands that record to a job-application pipeline,
which picks the parts that fit each posting and writes the two-page resume.

The record has no page limit, on purpose. A resume is a selection: two
pages, holding the few accomplishments that suited one application. When the
next application starts from that resume, it inherits the ceiling. The master
record holds everything, and the selection happens per posting, downstream.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # Windows: setx ANTHROPIC_API_KEY "sk-ant-..."

python -m resume_builder start             # guided first run
```

`start` asks whether you have a resume or a LinkedIn PDF. If you do, it
imports that. If you don't, it begins by listing your jobs. Then the
interview starts.

## Commands

| Command | What it does |
|---|---|
| `import resume.pdf` | Files an existing resume (.docx, .pdf, .txt) into the record. Copies; never rewrites. |
| `interview` | Asks about the thinnest role first: role context, then your old bullets opened back up, then accomplishments no resume ever held. Ends with education, licences and certifications. |
| `interview --role Acme` | One employer. |
| `interview --education` | Education, licences and certifications, then the optional sections: projects, languages, volunteer work, awards, publications and speaking, memberships, training, testimonials and career breaks. Plain questions, no API calls; Enter skips a section. Keeps your place in the job interview. |
| `interview --section languages` | Just one of those sections. |
| `summary --draft [--emphasis leadership]` | Drafts a professional summary from your record, flags any figure the record doesn't contain, and saves it only if you say so. `summary --set TEXT` writes your own. |
| `skills --build [--field "ICU nursing"]` | Starts your skills list from the standard skills for your field, then fills in what your record shows: skills your resume names are marked yes, skills your accomplishments show get an estimated level, and the rest wait for your answer. Running it again only fills blanks. |
| `skills --verify` | Answer the list: a level, y to accept the estimate, or n if you don't have it. "No" answers are kept as gaps. |
| `skills --add "Wound care" --category Clinical --level Expert` | A skill the list missed. |
| `skills` | Shows the list. To change a level, group or answer later, edit the record file or use the page. |
| `health` | What the record holds, what's unsettled, and the best use of your next hour. No API calls. |
| `export --to DIR` | Shows what writing the pipeline's files would change. |
| `export --to DIR --write` | Writes `master_profile.json` and `skills_inventory.csv` into DIR. |

The record is `record.md` in the current folder. Use `--record path` or
`RESUME_BUILDER_RECORD` to point it somewhere else. Every command prints the
file it's using.

## How the interview finds the numbers

The hard part is recall, not typing. Nobody lists fifteen accomplishments on
demand. Ask what was broken when they arrived, or who they trained, and work
appears that "list your achievements" never reaches.

For each accomplishment it looks for evidence in descending order of strength
and stops at the first rung that holds:

| | |
|---|---|
| `metric` | a number they already knew |
| `derived` | a number worked out together from before and after |
| `scope` | the size of the work: people, budget, patients, sites |
| `qualitative` | a contribution with no number attached; a real answer |

The second rung is where most of the value is. "I automated the reporting"
is not a metric. Asked how long it took before, how long now and how often,
the same person says "three days a month became half a day". That is.

After each accomplishment the interview shows what it saved: the situation, what you did, your part, the result and the skills used, labelled "Has a number" or "Described, no number". On the web page the card has Looks right and Change; Change opens every field for editing, and stays available. Jobs and accomplishments can also be edited from the record at any time. The record and the export keep the exact evidence tiers.

Everything is saved as it's confirmed. Stop with Ctrl-C at any time, and the
next run offers to pick up the half-finished one.

## The order of a session

0. A short opening: what the record is, roughly 15 to 20 minutes a job over as many visits as you like, and that everything saves and can be changed. On a phone the page adds a tip: a keyboard is easier, the microphone button works for answers, and you can finish on a computer. Every question has buttons to skip it (and, inside a story, "Not sure"), so nobody needs to know that an empty answer skips.
1. Contact details and the jobs you're aiming for, each asked once. A skipped
   one is not asked again.
2. The job list: all of it on a new record. On every later visit, "a new job,
   or one missing from that list?", so the record keeps up with a career.
   Each title is its own entry with its own dates. After each job the list
   asks "Any other title at this employer?", so a promotion is entered as two
   titles rather than folded into one; an imported job is asked the same
   question once. Titles at one employer share its company details, and the
   export marks the later one `promoted_from` ("Staff RN, after 36 months").
   The health report flags a promotion mentioned only under Recognition.
3. Each job, thinnest first: its employment type (a choice: full-time,
   part-time, contract and so on), what the organisation was, results against
   targets or rankings, then its accomplishments. A job that ends with fewer
   than three accomplishments and bullets is also asked its regular duties;
   a strong record never is. A job question skipped or answered "no" is not
   asked again on a later visit. Inside the conversation about an
   accomplishment, "no" is an answer the coach hears; only "skip", "done" or
   "move on" leaves it.
4. Skills from an old resume, once. They are believed: the list is shown with
   every box ticked and a one-line reminder that interviewers can ask about
   anything on a resume. Only what the person unticks is dropped (recorded as
   a no, so it is never suggested again); each kept one gets an optional "when
   did you last use it?". Skills their accomplishments already showed are not
   on the list.
5. Work outside paid jobs: volunteering, a board seat, a project, a capstone,
   caring for family. It opens with a short explanation of what counts and how
   to answer, then each one gets the same coached conversation as a job. It is
   stored with the jobs, marked by its type, but exported as `outside_work`,
   never as a job, and never counted in employment gaps.
6. Education, certifications and the optional sections.
7. The career story, asked once: what you're best at, what connects your jobs,
   and what you want next. It never goes on a resume as written; the pipeline
   uses it for cover letters (`differentiator`, `next_move` in the export).

## Skills in context

Each accomplishment keeps a **Skills used** line: the tools, software,
equipment, methods, procedures or specialist knowledge it took, in the words
a job posting would use. The interview asks for them once per accomplishment
(and explains what counts the first time). The skills list points back to
the work that proves each skill, and export passes the pairs to the pipeline
as `skills_in_context`, names only, so tailoring can pick the accomplishment
that proves the skill a posting asks for.

## Without a degree

Employers that drop a degree requirement often keep hiring as before, because
a manager comparing candidates falls back on the degree unless something else
shows the skill ([Burning Glass Institute and Harvard Business School, Feb
2024](https://www.burningglassinstitute.org/research/skills-based-hiring-2024)).
When no bachelor's degree or higher is recorded, the builder asks about formal
training on the job (what it was, how long, and the skills it taught, which
then count as evidence for those skills), and the health report says so.
Promotions inside one employer are listed with how long each took, for
everyone.

## What it will not do

- **Put a figure on a bullet that isn't in your own words.** Every number in
  a drafted bullet is checked against the problem, actions and results you
  gave. After two bad drafts it uses your wording instead.
- **File a team's result as yours alone.** When a result belongs to a group,
  the interview asks what your own part was and keeps it beside the result;
  the bullet says what you did and credits the outcome to the effort.
- **Collect confidential figures.** Each session opens by asking you not to
  enter exact revenue, profit, margins, client budgets, prices or salaries.
  The interviewer asks for safe forms (a percentage, a range, a ranking) and
  never presses for an exact number. A figure you give anyway is taken as
  yours to share and recorded as given.
- **Accept an imported line that isn't in your document.** Every line the
  importer files is matched against the source. Anything it can't find is set
  aside for you to check. Anything it didn't file is kept under "Unplaced".
- **Lose a highlight on export.** If the pipeline's profile holds a highlight
  the record lacks, export refuses. `--adopt` copies it into the record
  first.
- **Overwrite a value someone curated.** Export fills blanks only, reports
  every difference, and backs up all three files before writing.
- **Put an unconfirmed skill on a resume.** Suggested skills export as
  `have_it = verify` until you say yes.

## The record format

Plain Markdown, so you can edit it in any editor and it diffs cleanly. See
`examples/record.example.md`.

## The web app

The hosted product runs the same engine behind a per-user web API and one
page (`web/`). Each user's record lives in a database row scoped to them,
with the last 50 versions kept. They can download everything or delete
everything from the page.

```bash
pip install -r requirements-web.txt
RB_DEV=1 uvicorn web.app:app --reload       # then open http://localhost:8000
```

`RB_DEV=1` turns on a development sign-in, which takes an email address and
nothing else. Without it the service refuses every request, because real
sign-in isn't wired up yet. That is the first thing to add before anyone
else uses it.

## The job-application pipeline

`pipeline/` is the pipeline that finds postings, scores them against a
person's record and drafts the two-page resume and cover letter. From the
command line it reads `config/` as before (copy the `*.example.*` files).
In the hosted product it reads each person's record from their store and
their own settings, set through `/api/settings`:

| Setting | What it decides |
|---|---|
| `tuning` | score threshold, salary floor, bullets per role, word limits |
| `letter` | the letter's fixed paragraphs, and phrases they never want written |
| `titles` | job titles to search for and to exclude |
| `boards` | target companies by applicant-tracking system, locations, blocked companies |
| `methods` | named methods that make a bullet concrete in their field |

`POST /api/match` scores one posting for the signed-in person and, at or
above their threshold, drafts both documents. The model and the paid
search sources belong to the service and cannot be set per person.

## Testing

```bash
python -m unittest discover -s tests        # 101 tests; no API calls, no network
(cd pipeline/tests && python -m unittest discover -s .)   # the pipeline's 154
python evals/simulate.py --people 01        # one simulated person (needs a key)
```

`evals/testset/` holds eight fictional people: an ICU nurse, a teacher, a
plant manager, a software engineer, a retail manager, an accounting graduate,
a military-to-civilian career changer and a VP of sales. Each has the resume
they would arrive with and a `truth.json` of what a good interviewer should
draw out. `simulate.py` has a model play each person, answering only from
their truth file, and scores the record that results:

- **recall:** the share of their true accomplishments recorded
- **quantified:** the share of those that carry a figure
- **fabricated:** figures not in the truth file, and claims the person can't
  make (target: zero)
- **questions per accomplishment**

## Where it came from

The record format, the interview, the evidence ladder and the export
safeguards come from `Master Record Builder`. Each rule there was learned on
a real record. This project makes them work for anyone, and adds:

- resume and LinkedIn import
- the role-context questions
- the skills inventory
- the health report
- a resumable interview
