# Where this stands

Written 2026-10-03. The pipeline runs nightly at 19:00 and works; this is the
list of things known to be imperfect, so nobody has to rediscover them.

## This is now two projects

The master-accomplishment tools — the interview, the record format, the intake
importer, the profile build — live in `Documents\Master Record Builder`. They
were split out so that developing them could not break a process that runs every
night. Nothing the nightly run calls imports any of them.

**The data stayed here.** `profile/master.md` and `profile/master_profile.json`
are read and written by that project through a pointer file, because this
pipeline reads the JSON every night and keeping two copies of a career record in
step is a worse problem than a pointer. So `profile/` can change without any
change in this repository, and `master_profile.json` is no longer only written
by hand.

`score_and_tailor.py` keeps the `schema` argument on `request_json` that the
interview needed. It is opt-in, the three callers here do not pass it, and its
tests are in this suite.

## Open items

**29 salary figures were corrected; the scores beside them were not.** The pay
parser was rewritten on 2026-10-01 after it reported $192K–$304K for a role
paying $160,300–$253,600: it had been taking the largest range in a posting,
which is usually a location premium, and it discarded ranges written with cents.
The stored values were backfilled, but those postings were already scored and
will not be re-scored, so their stored score was set against the old number. Six
of them cross the $160,000 salary floor, which is an input to the seniority
logic: Marqeta, Affirm, Zocdoc, Reddit, FanDuel, Zip Co. Re-scoring just those
six is about fifteen cents.

**The public mirror carries a feature this pipeline no longer has.**
`Documents\job-app-pipeline` was pushed with `interview.py` and
`master_record.py` on 2026-10-02, the day before the split. They were left there
rather than removing a working feature from a published portfolio repository, so
the mirror and this pipeline have diverged. Decide at merge time whether the
mirror takes the finished builder or drops the early copy.

**The `data scien` stem pulls in general data-science roles.** It exists because
`data science` does not match `Data Scientist`, which hid every Staff Data
Scientist posting until someone noticed. The cost is that Indeed and the boards
return data-engineering and data-science work with no marketing in it — one
Pittsburgh "Surveillance & Marketing Analytics Data Engineer" got through, and a
LinkedIn newsletter about machine learning scores 0 every run. Tightening it
risks hiding the roles it was added for, so it has been left alone deliberately.

**`profile_audit.csv` has 116 claims awaiting keep/fix/cut verdicts.** 114 are
marked keep, 1 cut, 1 fix. It mirrors the profile rather than exceeding it, so it
inherited the same two-page ceiling the record project exists to escape. Its
schema — claim, source, verdict, correction — is the right shape for reviewing a
larger pool, if that is ever wanted.

## Things that bit once and are now guarded

Worth knowing because the guard is the only reason they are not still biting.

- **The morning brief's location is a setting**, `brief_path` in `tuning.yaml`,
  pointing at `../MORNING_BRIEF.md`. It was moved by a refactor on 2026-10-01
  and the stale copy at the old path looked current for two days. A file read
  every morning should not move because of a refactor.
- **`doctor.py` reads `HKCU:\Environment` directly** because Claude Code strips
  `ANTHROPIC_API_KEY` from the environment it hands to tool processes. Without
  that, doctor reports no credential on a machine whose scheduled run is using
  one. Do not re-diagnose it as a missing key.
- **The email source drops replies on the subject alone.** It leaked twice: once
  with acknowledgements and interview invitations, once with "RE: Thank you for
  the call today". Both reached 8/10 and had documents drafted. The patterns are
  tested against 243 real subjects. If it leaks a third time, the signal is that
  subject matching is the wrong instrument and `In-Reply-To` / `References`
  headers answer the question directly.
- **`build_tracker.py` falls back to `application_tracker.NEW.csv`** when Excel
  holds the real file open, and says so. Close the tracker before the nightly
  run or the update lands in the wrong file.
- **Prompt caching depends on the profile preamble being byte-identical** across
  calls. One reordered key and every call writes a fresh entry, reads nothing,
  and costs more than before caching, silently. The run's closing line reports
  the cache saving, and warns when writes happen with no reads.

## The shape of every bug worth remembering here

A guard that reads as protection and enforces nothing. It has happened enough
times to be the first thing to suspect:

- a comment documenting that company descriptors get stripped, with no code
  doing it
- `[OK] pipeline completed` printed after three days of failed scoring
- a placeholder check comparing against a string the shipped example does not
  use
- `-> application_tracker.csv` printed after writing `.NEW.csv`
- a test building its own copy of the pattern it was meant to be testing

When something looks protected, check that the protection runs.
