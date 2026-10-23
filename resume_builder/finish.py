"""
The end of the first build and what comes after it: the career story and
the summary, the measure of a complete record, home's suggestions, the
welcome back after an absence, and a later upload shown as a review.

The story answers go into the record (under "What sets me apart") so cover
letters can draw on them; the summary is the record's one summary. What the
page needs on top (the drafts written so far, which suggestions were put off,
an upload waiting for review) is kept in the store's state.
"""
import re
from datetime import datetime, timezone

from . import health, importer, journey, sections, skills
from . import record as mr
from . import summary as summ

STORY = "journey.story"
SUMMARY = "journey.summary"
DISMISSED = "journey.dismissed"
PENDING = "journey.pending"
BUILT = "journey.built"

STORY_QUESTIONS = (
    ("best", "What are you best at?",
     "Calming a chaotic operation · Training people who end up leading · "
     "Fixing the process nobody else wants to touch"),
    ("thread", "What thread runs through your career?",
     "Every job has been about making a busy place run on time · "
     "I keep getting handed the sites that are struggling"),
    ("next", "What's your next move?",
     "Run a regional distribution network · Move from operations into supply chain planning"),
)
STORY_LABELS = {"best": "Best at", "thread": "The thread through my career", "next": "Next move"}

ABSENCE_HOURS = 12        # away this long, an unfinished build opens on "welcome back"
LONG_ABSENCE_DAYS = 60    # away this long, welcome back also asks what has changed
MAX_SUGGESTIONS = 3


# ---------------------------------------------------------------- the career story

def story_view(store) -> dict:
    a = store.load_state(STORY) or {}
    return {"questions": [{"key": k, "q": q, "ex": ex, "answer": a.get(k, "")}
                          for k, q, ex in STORY_QUESTIONS]}


def save_story(store, rec: mr.Record, answers: dict) -> None:
    """Keep the answers, and put them in the record where cover letters and
    the summary can use them. Lines the person wrote there themselves stay."""
    a = store.load_state(STORY) or {}
    for k, _, _ in STORY_QUESTIONS:
        if k in answers:
            a[k] = " ".join(str(answers[k] or "").split())[:600]
    store.save_state(STORY, a)
    ours = tuple(f"{label}:" for label in STORY_LABELS.values())
    rec.sets_apart = [l for l in rec.sets_apart if not l.startswith(ours)]
    rec.sets_apart += [f"{STORY_LABELS[k]}: {a[k]}" for k, _, _ in STORY_QUESTIONS if a.get(k)]


# ---------------------------------------------------------------- the summary

def _summary_state(store) -> dict:
    st = store.load_state(SUMMARY) or {}
    st.setdefault("drafts", [])
    return st


def summary_view(store, rec: mr.Record) -> dict:
    st = _summary_state(store)
    targets = journey.targets_view(store, rec)["titles"]
    approved = bool(rec.summary) and st.get("approved") == rec.summary
    aimed = {mr._squash(t) for t in st.get("for", [])}
    return {"current": rec.summary, "drafts": st["drafts"], "approved": approved,
            "failed": bool(st.get("failed")),
            "new_target": approved and any(mr._squash(t) not in aimed for t in targets)}


def draft_summary(store, rec: mr.Record) -> dict:
    """One paid call (two at most): a summary drawn only from the record. A
    draft with a figure the record doesn't hold is never shown."""
    st = _summary_state(store)
    targets = journey.targets_view(store, rec)["titles"]
    hint = []
    if targets:
        hint.append(f"aim it at {', '.join(targets)} roles, as far as the record supports")
    if st["drafts"]:
        hint.append(f"make it clearly different from this earlier version: {st['drafts'][-1]}")
    for _ in range(2):
        out = summ.draft(rec, "; ".join(hint))
        if out["text"] and not out["unsupported"]:
            st["drafts"].append(out["text"])
            st["failed"] = False
            store.save_state(SUMMARY, st)
            return {"text": out["text"]}
    st["failed"] = True
    store.save_state(SUMMARY, st)
    return {"text": "", "failed": True}


def approve_summary(store, rec: mr.Record, text: str) -> None:
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("Your summary is empty. Write a few lines, or skip it for now")
    rec.summary = text
    st = _summary_state(store)
    st["approved"] = text
    st["for"] = journey.targets_view(store, rec)["titles"]
    store.save_state(SUMMARY, st)
    sections.mark(store, "summary")


# ---------------------------------------------------------------- how far along

def progress(store, rec: mr.Record, work: dict) -> dict:
    """The finish line is the whole record gone through once: every position
    complete, education, skills and the summary. Beyond paid work is optional."""
    paid = [j for j in work["jobs"] if not j["outside"]]
    done = sections.done(store)
    left = [k for k in ("edu", "skills", "summary") if not done[k]]
    open_jobs = [j for j in paid if not j["complete"]]
    complete = bool(paid) and not open_jobs and not left
    # Once the first build is done, home is where a return visit lands, even
    # if a job added since then is still open.
    built = bool(store.load_state(BUILT))
    if complete and not built:
        store.save_state(BUILT, True)
        built = True
    return {"positions": len(paid), "complete": work["complete_count"],
            "years": work["years"], "sections_left": left, "jobs_left": len(open_jobs),
            "record_complete": complete, "first_build_done": built,
            "minutes_left": minutes_left(open_jobs, left)}


def minutes_left(open_jobs: list, sections_left: list) -> int:
    """A rough guide for the welcome back: a minute a background question,
    three a line, two for "anything else", and a few for each section."""
    m = 0
    for j in open_jobs:
        m += j["open_questions"] + 3 * (j["counts"]["todo"] + j["counts"]["skipped"])
        m += 0 if j["more"] else 2
    m += sum({"edu": 3, "skills": 5, "summary": 5}[k] for k in sections_left)
    return max(5, int(round(m / 5.0)) * 5) if m else 0


def stamp(store, now=None) -> float:
    """Hours since the person was last here, then mark them here now."""
    now = now or datetime.now(timezone.utc)
    step = journey.load_step(store)
    last = step.get("seen_at")
    step["seen_at"] = now.isoformat()
    store.save_state(journey.STEP, step)
    if not last:
        return 0.0
    try:
        return max(0.0, (now - datetime.fromisoformat(last)).total_seconds() / 3600)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- home's suggestions

NUMBER = re.compile(r"\d")


def suggestions(store, rec: mr.Record, work: dict, skill_view: dict, summary: dict) -> list:
    """Specific things that would make the record stronger, each one tap to
    fix and each dismissable. At most three show at a time."""
    gone = set(store.load_state(DISMISSED) or [])
    out = []

    def add(key, text, action, screen, at=""):
        if key not in gone:
            out.append({"key": key, "text": text, "action": action, "screen": screen, "at": at})

    paid = [j for j in work["jobs"] if not j["outside"]]
    for j in paid:
        skipped = j["counts"]["skipped"]
        if skipped:
            add(f"skipped:{j['key']}", f"{_n(skipped, 'line')} at {j['employer']} "
                f"{'is' if skipped == 1 else 'are'} skipped for now.", "Go back to them",
                "lines", j["key"])
    bare = [(sum(1 for l in j["lines"] if not NUMBER.search(l["text"])), j) for j in paid]
    bare = [(n, j) for n, j in bare if n]
    if bare:
        n, j = max(bare, key=lambda x: x[0])
        add(f"num:{j['key']}", f"{_n(n, 'line')} at {j['employer']} "
            f"{'doesn' if n == 1 else 'don'}'t have a number yet.", "Add numbers", "lines", j["key"])
    targets = journey.targets_view(store, rec)["titles"]
    if not targets:
        add("target", "No target job set, so skill suggestions follow your current role.",
            "Set a target", "targets")
    if not summary["current"]:
        add("summary", "Your record has no summary yet.", "Write one", "story")
    elif summary["new_target"]:
        add("target-summary", "Your summary hasn't been checked against your newest target job.",
            "Review summary", "summary")
    for g in skill_view["groups"]:
        for k in g["items"]:
            if k["state"] == "yes" and not k["shown"].startswith("Shown by"):
                add(f"skill:{mr._squash(k['name'])}", f"No line shows your {k['name']} skill yet.",
                    "Add an example", "skills")
                break
        else:
            continue
        break
    if skill_view["unrated"]:
        add("levels", f"{_n(skill_view['unrated'], 'skill')} on your record "
            f"{'has' if skill_view['unrated'] == 1 else 'have'} no level yet.", "Set levels", "skills")
    return out[:MAX_SUGGESTIONS]


def dismiss(store, key: str) -> None:
    gone = store.load_state(DISMISSED) or []
    if key and key not in gone:
        gone.append(key[:300])
        store.save_state(DISMISSED, gone)


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- a later upload, as a review

def _gone(store) -> tuple:
    d = journey.deleted(store)
    jobs = {(mr._squash(j["employer"]), mr._squash(j["title"])) for j in d["jobs"]}
    lines = {(mr._squash(x["employer"]), mr._squash(x["title"]), importer._norm(x["text"]))
             for x in d["lines"]}
    items = {(x["section"], importer._norm(x["text"])) for x in d.get("items", [])}
    return jobs, lines, items


SECTION_KINDS = {"education": "Degree or course", "certifications": "Licence or certification"}


def review_items(store, rec: mr.Record, incoming: mr.Record) -> list:
    """What the file would change, one item each. New things are ticked;
    things the person deleted before are not, and say so."""
    gone_jobs, gone_lines, gone_items = _gone(store)
    out = []
    for k, v in incoming.contact.items():
        if v and not journey.clean(rec.contact.get(k, "")):
            out.append({"id": f"contact:{k}", "group": "contact", "text": f"{k}: {v}", "pick": True})
    for i, role in enumerate(incoming.roles):
        have = rec.role_by_employer(role.employer, role.title, strict=True)
        key = (mr._squash(role.employer), mr._squash(role.title))
        if have is None:
            before = key in gone_jobs
            out.append({"id": f"job:{i}", "group": "deleted_job" if before else "job",
                        "text": f"{role.title}, {role.employer}",
                        "dates": journey.clean(role.fields.get("Dates", "")),
                        "lines": len(role.recorded_bullets), "pick": not before})
            continue
        known = {importer._norm(b) for b in have.recorded_bullets}
        known |= {importer._norm(a.bullet) for a in have.accomplishments if a.bullet}
        for b, text in enumerate(role.recorded_bullets):
            if importer._norm(text) in known:
                continue
            before = (*key, importer._norm(text)) in gone_lines
            out.append({"id": f"line:{i}:{b}", "group": "deleted_line" if before else "line",
                        "text": text, "job": f"{have.title}, {have.employer}", "pick": not before})
    for section in ("education", "certifications"):
        mine = {importer._norm(x) for x in getattr(rec, section)}
        for n, text in enumerate(getattr(incoming, section)):
            if importer._norm(text) not in mine:
                before = (section, importer._norm(text)) in gone_items
                out.append({"id": f"sec:{section}:{n}", "group": "deleted_item" if before else "item",
                            "kind": SECTION_KINDS[section], "text": text, "pick": not before})
    for key, lines in incoming.extras.items():
        mine = {importer._norm(x) for x in rec.extras.get(key, [])}
        for n, text in enumerate(lines):
            if importer._norm(text) not in mine:
                before = (key, importer._norm(text)) in gone_items
                out.append({"id": f"sec:{key}:{n}", "group": "deleted_item" if before else "item",
                            "kind": sections.KIND_NAMES.get(key, key.replace("_", " ").capitalize()),
                            "text": text, "pick": not before})
    for n, s in enumerate(incoming.skills):
        if not rec.skill(s.name):
            out.append({"id": f"skill:{n}", "group": "skill", "text": s.name, "pick": True})
    if incoming.summary and not rec.summary:
        out.append({"id": "summary", "group": "summary", "text": incoming.summary, "pick": True})
    return out


def start_review(store, rec: mr.Record, incoming: mr.Record, filename: str) -> dict:
    store.save_state(PENDING, {"file": filename, "record": mr.render(incoming)})
    return review_view(store, rec)


def review_view(store, rec: mr.Record) -> dict | None:
    pend = store.load_state(PENDING)
    if not pend:
        return None
    return {"file": pend["file"], "items": review_items(store, rec, mr.parse(pend["record"]))}


def _place(rec: mr.Record, role: mr.Role) -> None:
    """A job from a newer file goes where it belongs, newest first: beside
    the same employer's titles, or before the first job that started earlier."""
    same = [n for n, r in enumerate(rec.roles) if mr._squash(r.employer) == mr._squash(role.employer)]
    if same:
        rec.roles.insert(same[0], role)
        return
    mine = health.parse_range(role.fields.get("Dates", ""))
    if mine:
        for n, r in enumerate(rec.roles):
            theirs = health.parse_range(r.fields.get("Dates", ""))
            if theirs and theirs[0] < mine[0]:
                rec.roles.insert(n, role)
                return
    rec.roles.append(role)


def apply_review(store, rec: mr.Record, chosen: list) -> dict:
    """Add what the person ticked; nothing else from the file is saved."""
    pend = store.load_state(PENDING)
    if not pend:
        raise LookupError("There's no file waiting to be reviewed")
    incoming = mr.parse(pend["record"])
    pick = set(chosen)
    items = [x for x in review_items(store, rec, incoming) if x["id"] in pick]
    d = journey.deleted(store)
    added = {"jobs": 0, "lines": 0, "other": 0}
    for it in items:
        kind, _, rest = it["id"].partition(":")
        if kind == "contact":
            rec.contact[rest] = incoming.contact[rest]
            added["other"] += 1
        elif kind == "job":
            role = incoming.roles[int(rest)]
            _place(rec, role)
            d["jobs"] = [j for j in d["jobs"] if not (
                mr._squash(j["employer"]) == mr._squash(role.employer)
                and mr._squash(j["title"]) == mr._squash(role.title))]
            added["jobs"] += 1
        elif kind == "line":
            i, b = (int(x) for x in rest.split(":"))
            src = incoming.roles[i]
            have = rec.role_by_employer(src.employer, src.title, strict=True)
            text = src.recorded_bullets[b]
            have.recorded_bullets.append(text)
            key = importer._norm(text)
            d["lines"] = [x for x in d["lines"] if not (
                importer._norm(x["text"]) == key and mr._squash(x["employer"]) == mr._squash(have.employer)
                and mr._squash(x["title"]) == mr._squash(have.title))]
            added["lines"] += 1
        elif kind == "sec":
            section, n = rest.rsplit(":", 1)
            text = (getattr(incoming, section) if section in SECTION_KINDS
                    else incoming.extras[section])[int(n)]
            (getattr(rec, section) if section in SECTION_KINDS
             else rec.extras.setdefault(section, [])).append(text)
            d["items"] = [x for x in d.get("items", []) if not (
                x["section"] == section and importer._norm(x["text"]) == importer._norm(text))]
            added["other"] += 1
        elif kind == "skill":
            rec.skills.append(incoming.skills[int(rest)])
            added["other"] += 1
        elif kind == "summary":
            rec.summary = incoming.summary
            added["other"] += 1
    store.save_state(journey.DELETED, d)
    store.clear_state(PENDING)
    return added


def drop_review(store) -> None:
    store.clear_state(PENDING)
