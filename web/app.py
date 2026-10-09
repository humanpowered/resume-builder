"""
The hosted product's web service: the same engine the command line uses,
behind a per-user API and one page.

  RB_DEV=1 uvicorn web.app:app --reload        # local development

Sign-in is deliberately not built here. In development a dev login sets a
cookie naming the user; in production `current_user` must be replaced by the
chosen identity provider's check. Until it is, the service refuses every
request rather than run without real authentication: a career record holds
someone's address, phone and employment history.
"""
import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resume_builder import export as X  # noqa: E402
from resume_builder import health, importer, interview, journey, matching, skills  # noqa: E402
from resume_builder import summary as summary_mod  # noqa: E402
from resume_builder import record as mr  # noqa: E402
from resume_builder.store import SqlStore  # noqa: E402

DB_PATH = os.environ.get("RB_DB", str(ROOT / "web" / "dev.sqlite3"))
DEV = os.environ.get("RB_DEV") == "1"
# Invite codes for a test round on a public address: with RB_INVITE_CODES set
# (comma-separated), the dev sign-in also needs one of them, so only invited
# testers get in. Unset, local development works as before.
INVITES = {c.strip() for c in os.environ.get("RB_INVITE_CODES", "").split(",") if c.strip()}
MAX_UPLOAD = 5 * 1024 * 1024

app = FastAPI(title="Resume Builder")


def _model_unreachable(exc: BaseException) -> bool:
    """The model has no credentials or rejected them. Every turn of the
    interview, every import and every match needs it, so this is the service
    being unconfigured, not something the person did."""
    name, msg = type(exc).__name__.lower(), str(exc).lower()
    return ("authentication" in name or "permissiondenied" in name
            or "could not resolve authentication method" in msg
            or "invalid x-api-key" in msg)


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    if _model_unreachable(exc):
        return JSONResponse(status_code=503, content={
            "detail": "The AI model isn't available right now, so this step can't run. "
                      "Nothing was changed; try again later."})
    raise exc


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    SqlStore.init(conn)
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request) -> str:
    if not DEV:
        raise HTTPException(503, "Sign-in is not configured; this service will not run "
                                 "without it.")
    user = request.cookies.get("rb_user", "")
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


def store_for(user: str = Depends(current_user), conn=Depends(db)) -> SqlStore:
    return SqlStore(conn, user)


# --------------------------------------------------------------------------
# Development sign-in


class DevLogin(BaseModel):
    email: str
    code: str = ""


@app.post("/dev/login")
def dev_login(body: DevLogin, response: Response):
    if not DEV:
        raise HTTPException(404)
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter an email address")
    if INVITES and body.code.strip() not in INVITES:
        raise HTTPException(403, "That invite code isn't right. Check the message it came in.")
    response.set_cookie("rb_user", email, httponly=True, samesite="lax")
    return {"user": email}


# --------------------------------------------------------------------------
# The record


def summary(rec: mr.Record) -> dict:
    return {
        "name": rec.contact.get("Name", ""),
        "roles": [{"employer": r.employer, "title": r.title,
                   "dates": r.fields.get("Dates", ""),
                   "fields": {f: r.fields.get(f, "") for f in JOB_EDITABLE},
                   "outside": r.outside(),
                   "accomplishments": [{**interview.accomplishment_view(a), "bullet": a.bullet}
                                       for a in r.accomplishments],
                   "resume_bullets": r.recorded_bullets} for r in rec.roles],
        "skills": [{"name": s.name, "category": s.category, "level": s.level,
                    "years": s.years, "last_used": s.last_used,
                    "evidence": s.evidence, "have": s.have, "source": s.source,
                    "pending": skills.is_pending(s)}
                   for s in rec.skills],
        "education": rec.education, "certifications": rec.certifications,
        "summary": rec.summary,
    }


# The job details a person can change on the page, in the order shown.
JOB_EDITABLE = ("Dates", "Employment type", "Location", "Company", "Challenge", "Authority",
                "Budget", "Reported to", "Territory", "Results against targets", "Recognition",
                "Responsibilities")


@app.get("/api/record")
def get_record(store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    return {"exists": store.exists(), "markdown": mr.render(rec), "summary": summary(rec),
            "intro": interview.INTRO, "phone_tip": interview.PHONE_TIP,
            "interview_started": store.load_state(interview.STATE) is not None}


class RecordText(BaseModel):
    markdown: str


@app.put("/api/record")
def put_record(body: RecordText, store: SqlStore = Depends(store_for)):
    """Save a hand edit. The previous version is kept in history."""
    rec = mr.parse(body.markdown)
    store.save_record(rec)
    return {"summary": summary(rec)}


async def _read_upload(file: UploadFile) -> str:
    """The text of an uploaded resume, or the reason it can't be read."""
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "That file is over 5 MB")
    suffix = Path(file.filename or "resume.txt").suffix.lower()
    if suffix not in (".docx", ".pdf", ".txt", ".md"):
        raise HTTPException(415, "Upload a .docx, .pdf or .txt file")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
    try:
        text = importer.read_text(Path(tmp.name))
    finally:
        os.unlink(tmp.name)
    if len(text.strip()) < 50:
        raise HTTPException(422, "No text could be read. If it is a scanned PDF, "
                                 "upload it as Word or text instead.")
    return text


@app.post("/api/import")
async def import_resume(file: UploadFile = File(...), store: SqlStore = Depends(store_for)):
    text = await _read_upload(file)
    from datetime import date
    incoming, rep = importer.to_record(importer.extract(text), text,
                                       file.filename or "upload", date.today().isoformat())
    if store.exists():
        rec = store.load_record()
        changes = importer.merge(rec, incoming)
    else:
        rec, changes = incoming, ["new record"]
    store.save_record(rec)
    return {"changes": len(changes), "set_aside": rep["rejected"],
            "unplaced": rep["unplaced"], "summary": summary(rec)}


# --------------------------------------------------------------------------
# The interview, one turn per request


class Step(BaseModel):
    answer: str | None = None
    role: str = ""


@app.post("/api/interview/step")
def interview_step(body: Step, store: SqlStore = Depends(store_for)):
    try:
        iv = interview.Interview(store, only=body.role, ask_targets=False)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    p = iv.step(body.answer)
    return {"text": p.text, "kind": p.kind, "hint": p.hint, "notes": p.notes,
            "options": p.options, "saved": p.saved, "why": p.why}


@app.post("/api/interview/restart")
def interview_restart(store: SqlStore = Depends(store_for)):
    store.clear_state(interview.STATE)
    store.clear_state(interview.BACKGROUND_STATE)
    for key in interview.SECTION_KEYS:
        store.clear_state(f"{interview.BACKGROUND_STATE}:{key}")
    return {"ok": True}


# --------------------------------------------------------------------------
# Jobs and accomplishments, editable any time
#
# Addressed by position, with the title the page last showed as a check: if
# the record changed underneath (another tab, the interview), the edit is
# refused rather than landing on the wrong entry.

class AccEdit(BaseModel):
    expect_title: str = Field("", max_length=300)
    title: str | None = Field(None, max_length=300)
    problem: str | None = Field(None, max_length=4000)
    actions: str | None = Field(None, max_length=4000)
    contribution: str | None = Field(None, max_length=4000)
    results: str | None = Field(None, max_length=4000)
    skills: list[str] | None = Field(None, max_length=60)


class JobEdit(BaseModel):
    expect: str = Field("", max_length=600)       # the job's label as the page showed it
    employer: str | None = Field(None, max_length=300)
    title: str | None = Field(None, max_length=300)
    fields: dict[str, str] = Field(default_factory=dict)


STALE = "Your record changed since this page loaded. Reload and try again."


def _job(rec: mr.Record, i: int, expect: str) -> mr.Role:
    if not 0 <= i < len(rec.roles):
        raise HTTPException(404, "No such job")
    role = rec.roles[i]
    if expect and expect != role.label():
        raise HTTPException(409, STALE)
    return role


@app.patch("/api/roles/{ri}/accomplishments/{ai}")
def accomplishment_edit(ri: int, ai: int, body: AccEdit, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    role = _job(rec, ri, "")
    if not 0 <= ai < len(role.accomplishments):
        raise HTTPException(404, "No such accomplishment")
    acc = role.accomplishments[ai]
    if body.expect_title and body.expect_title != acc.title:
        raise HTTPException(409, STALE)
    if body.title is not None and not body.title.strip():
        raise HTTPException(400, "Give it a short name")
    mr.edit_accomplishment(rec, acc, body.model_dump(exclude={"expect_title"}))
    store.save_record(rec)
    return {**interview.accomplishment_view(acc), "role": ri, "index": ai, "job": role.label()}


@app.patch("/api/roles/{ri}")
def job_edit(ri: int, body: JobEdit, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    role = _job(rec, ri, body.expect)
    old_key = interview.role_key(role)
    for name, value in (("employer", body.employer), ("title", body.title)):
        if value is not None:
            if not value.strip():
                raise HTTPException(400, f"The {name} can't be blank")
            setattr(role, name, value.strip())
    for name, value in body.fields.items():
        if name not in JOB_EDITABLE:
            raise HTTPException(400, f"Can't edit {name!r} here")
        if value.strip():
            role.fields[name] = value.strip()
        else:
            role.fields.pop(name, None)
    store.save_record(rec)
    interview.rename_job(store, old_key, interview.role_key(role))
    return {"label": role.label()}


# --------------------------------------------------------------------------
# Summary and the list sections, editable any time

class SummaryText(BaseModel):
    text: str = Field("", max_length=3000)


@app.get("/api/summary")
def summary_get(store: SqlStore = Depends(store_for)):
    text = store.load_record().summary
    return {"text": text, "issues": summary_mod.check(text)}


@app.put("/api/summary")
def summary_put(body: SummaryText, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    rec.summary = " ".join(body.text.split())
    store.save_record(rec)
    return {"text": rec.summary, "issues": summary_mod.check(rec.summary)}


class Targets(BaseModel):
    Titles: str = Field("", max_length=1000)
    Industries: str = Field("", max_length=1000)
    Locations: str = Field("", max_length=1000)


def targets_view(rec: mr.Record) -> dict:
    return {**{k: rec.target.get(k, "") for k in interview.CARD_TARGETS},
            "suggested_titles": interview.suggested_titles(rec)}


@app.get("/api/target")
def target_get(store: SqlStore = Depends(store_for)):
    return targets_view(store.load_record())


@app.put("/api/target")
def target_put(body: Targets, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    for k in interview.CARD_TARGETS:
        v = " ".join(getattr(body, k).split()).strip(" ;")
        if v:
            rec.target[k] = v
        else:
            rec.target.pop(k, None)
    store.save_record(rec)
    return targets_view(rec)


class DraftAsk(BaseModel):
    emphasis: str = Field("", max_length=300)


@app.post("/api/summary/draft")
def summary_draft(body: DraftAsk, store: SqlStore = Depends(store_for)):
    """A draft to edit; nothing is saved until the person saves it."""
    out = summary_mod.draft(store.load_record(), body.emphasis)
    return {**out, "issues": summary_mod.check(out["text"])}


@app.get("/api/sections")
def sections_get(store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    return [{"key": k, "label": interview.LABELS[k],
             "items": list(interview.section_items(rec, k))} for k in interview.SECTION_KEYS]


class SectionItems(BaseModel):
    items: list[str] = Field(..., max_length=200)


@app.put("/api/sections/{key}")
def sections_put(key: str, body: SectionItems, store: SqlStore = Depends(store_for)):
    """Replace one section's entries: how the page edits, removes and reorders."""
    if key not in interview.SECTION_KEYS:
        raise HTTPException(404, "No such section")
    items = [" ".join(i.split()) for i in body.items if i.strip()]
    if any(len(i) > 1000 for i in items):
        raise HTTPException(400, "An entry is longer than 1,000 characters")
    rec = store.load_record()
    target = interview.section_items(rec, key)
    target[:] = items
    store.save_record(rec)
    return {"key": key, "items": items}


# --------------------------------------------------------------------------
# Health, skills, export


@app.get("/api/health")
def get_health(store: SqlStore = Depends(store_for)):
    rep = health.check(store.load_record())
    return {"text": health.render(rep), "total": rep.total, "quantified": rep.quantified,
            "next_steps": rep.next_steps,
            "roles": [vars(r) for r in rep.roles], "issues": rep.record_issues}


# The skills list. Built once from the person's field and record, then theirs
# to answer, adjust and add to whenever they like.

SKILLS_FIELD = "skills_field"           # the field the list was last built for


class BuildSkills(BaseModel):
    field: str = Field("", max_length=200)


@app.get("/api/skills")
def skills_list(store: SqlStore = Depends(store_for)):
    return {"field": store.load_state(SKILLS_FIELD) or "",
            "hint": skills.field_hint(store.load_record()),
            "levels": list(mr.LEVELS),
            "skills": summary(store.load_record())["skills"]}


@app.post("/api/skills/build")
def skills_build(body: BuildSkills, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    out = skills.build(rec, body.field)
    store.save_record(rec)
    store.save_state(SKILLS_FIELD, out["field"])
    return out


class NewSkill(BaseModel):
    name: str = Field(..., max_length=200)
    category: str = Field("", max_length=100)
    level: str = ""


@app.post("/api/skills")
def skills_add(body: NewSkill, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    try:
        s = skills.add_skill(rec, body.name, body.category, body.level)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    store.save_record(rec)
    return {"name": s.name}


class SkillChange(BaseModel):
    have: str | None = None             # yes, no, or verify (not sure yet)
    level: str | None = None            # "" clears it
    category: str | None = Field(None, max_length=100)
    rename: str | None = Field(None, max_length=200)
    years: str | None = Field(None, max_length=20)          # e.g. "8"
    last_used: str | None = Field(None, max_length=20)      # a year, or "current"


@app.patch("/api/skills/{name}")
def skills_change(name: str, body: SkillChange, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    try:
        s = skills.set_skill(rec, name, body.have, body.level, body.category, body.rename,
                             body.years, body.last_used)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    store.save_record(rec)
    return {"name": s.name, "have": s.have, "level": s.level, "category": s.category,
            "years": s.years, "last_used": s.last_used}


@app.delete("/api/skills/{name}")
def skills_remove(name: str, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    try:
        skills.remove_skill(rec, name)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    store.save_record(rec)
    return {"ok": True}


@app.post("/api/skills/accept-shown")
def skills_accept_shown(store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    done = skills.accept_shown(rec)
    store.save_record(rec)
    return {"accepted": done}


def _build(store: SqlStore):
    rec = store.load_record()
    prints = store.load_state("bullets") or {}
    ex = X.Export(rec, {}, prints)
    profile = ex.build()
    store.save_record(rec)                  # drafted bullets kept beside their source
    store.save_state("bullets", ex.prints)
    return rec, profile


@app.get("/api/export/master_profile.json")
def export_profile(store: SqlStore = Depends(store_for)):
    _, profile = _build(store)
    return Response(json.dumps(profile, indent=2, ensure_ascii=False),
                    media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="master_profile.json"'})


@app.get("/api/export/skills_inventory.csv")
def export_skills(store: SqlStore = Depends(store_for)):
    rows = X.skills_rows(store.load_record())
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=X.CSV_COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="skills_inventory.csv"'})


# --------------------------------------------------------------------------
# Job search settings and matching, through the pipeline


@app.get("/api/settings")
def get_settings(store: SqlStore = Depends(store_for)):
    """What the person set, and every value in effect once defaults apply."""
    cfg = matching.settings_for(store)
    return {"set": cfg.to_dict(),
            "effective": {"tuning": {k: v for k, v in cfg.tuning.items()
                                     if k not in ("model", "brief_path")},
                          "letter": cfg.letter,
                          "titles": {"include": cfg.include_titles,
                                     "exclude": cfg.exclude_titles},
                          "boards": cfg.boards, "methods": cfg.methods}}


@app.put("/api/settings")
def put_settings(body: dict, store: SqlStore = Depends(store_for)):
    try:
        cfg = matching.save_settings(store, body)
    except ValueError as exc:          # the pipeline's ConfigError, naming the section
        raise HTTPException(422, str(exc))
    return {"set": cfg.to_dict()}


class Posting(BaseModel):
    title: str
    company: str = ""
    location: str = ""
    description: str
    documents: bool = True


@app.post("/api/match")
def match_posting(body: Posting, store: SqlStore = Depends(store_for)):
    """Score one posting against the person's record and, if it clears their
    threshold, draft the resume and cover letter."""
    if not store.exists():
        raise HTTPException(409, "Build your record first")
    if len(body.description) > 60_000:
        raise HTTPException(413, "That posting is too long")
    return matching.match(store, body.model_dump(), documents=body.documents)


# --------------------------------------------------------------------------
# The user's own data


@app.get("/api/me/download")
def download_everything(store: SqlStore = Depends(store_for)):
    """Everything held about this user, in one file."""
    return Response(mr.render(store.load_record()), media_type="text/markdown",
                    headers={"Content-Disposition": 'attachment; filename="my-record.md"'})


@app.delete("/api/me")
def delete_me(response: Response, store: SqlStore = Depends(store_for)):
    store.delete_everything()
    response.delete_cookie("rb_user")
    return {"deleted": True}


# --------------------------------------------------------------------------
# The guided build (the new page, served at /new while the old page stays up)
#
# Jobs are addressed by position with the label the page last showed, and
# lines by kind and position with the text the page last showed, so an edit
# made against an out-of-date page is refused rather than landing on the
# wrong thing.

def _j(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except journey.Stale:
        raise HTTPException(409, STALE)
    except IndexError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _jstate(store: SqlStore, rec: mr.Record | None = None) -> dict:
    rec = rec if rec is not None else store.load_record()
    return {"exists": store.exists(), "screen": journey.start_screen(store),
            "step": journey.load_step(store), **journey.view(rec)}


@app.get("/api/j/state")
def j_state(store: SqlStore = Depends(store_for)):
    return _jstate(store)


class JStep(BaseModel):
    screen: str = Field(..., max_length=40)


@app.put("/api/j/step")
def j_step(body: JStep, store: SqlStore = Depends(store_for)):
    return _j(journey.save_step, store, body.screen)


@app.post("/api/j/import")
async def j_import(file: UploadFile = File(...), store: SqlStore = Depends(store_for)):
    """Read one file into the record. Several files are sent one at a time,
    so the page can say how each one went."""
    text = await _read_upload(file)
    from datetime import date
    incoming, rep = importer.to_record(importer.extract(text), text,
                                       file.filename or "upload", date.today().isoformat())
    left_out = journey.leave_out_deleted(store, incoming)
    if store.exists():
        rec = store.load_record()
        before_jobs = len(rec.roles)
        before_lines = sum(len(r.recorded_bullets) for r in rec.roles)
        importer.merge(rec, incoming)
    else:
        rec, before_jobs, before_lines = incoming, 0, 0
    store.save_record(rec)
    return {"file": file.filename, "new_jobs": len(rec.roles) - before_jobs,
            "new_lines": sum(len(r.recorded_bullets) for r in rec.roles) - before_lines,
            "set_aside": len(rep["rejected"]), "left_out_deleted": left_out,
            **_jstate(store, rec)}


class JContact(BaseModel):
    fields: dict[str, str] = Field(default_factory=dict)


@app.patch("/api/j/contact")
def j_contact(body: JContact, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    _j(journey.edit_contact, rec, body.fields)
    store.save_record(rec)
    return _jstate(store, rec)


class JJob(BaseModel):
    expect: str = Field("", max_length=600)
    employer: str | None = Field(None, max_length=300)
    title: str | None = Field(None, max_length=300)
    dates: str | None = Field(None, max_length=100)


@app.post("/api/j/jobs")
def j_add_job(body: JJob, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    at = _j(journey.add_job, rec, body.employer or "", body.title or "", body.dates or "")
    store.save_record(rec)
    return {"added": at, **_jstate(store, rec)}


@app.patch("/api/j/jobs/{ri}")
def j_edit_job(ri: int, body: JJob, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    role = _j(journey.edit_job, rec, ri, body.expect, body.employer, body.title, body.dates)
    store.save_record(rec)
    return _jstate(store, rec)


@app.delete("/api/j/jobs/{ri}")
def j_delete_job(ri: int, expect: str, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    gone = _j(journey.delete_job, store, rec, ri, expect)
    store.save_record(rec)
    return {"deleted": gone, **_jstate(store, rec)}


class JLine(BaseModel):
    expect: str = Field("", max_length=600)
    kind: str = Field(journey.RESUME, max_length=10)
    expect_text: str = Field("", max_length=4000)
    text: str = Field("", max_length=4000)
    story: dict | None = None


@app.post("/api/j/jobs/{ri}/lines")
def j_add_line(ri: int, body: JLine, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    _j(journey.add_line, rec, ri, body.expect, body.text)
    store.save_record(rec)
    return _jstate(store, rec)


@app.patch("/api/j/jobs/{ri}/lines/{li}")
def j_edit_line(ri: int, li: int, body: JLine, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    _j(journey.edit_line, store, rec, ri, body.expect, body.kind, li, body.expect_text, body.text)
    store.save_record(rec)
    return _jstate(store, rec)


@app.post("/api/j/jobs/{ri}/lines/{li}/delete")
def j_delete_line(ri: int, li: int, body: JLine, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    gone = _j(journey.delete_line, store, rec, ri, body.expect, body.kind, li, body.expect_text)
    store.save_record(rec)
    return {"deleted": gone, **_jstate(store, rec)}


@app.post("/api/j/jobs/{ri}/lines/{li}/restore")
def j_restore_line(ri: int, li: int, body: JLine, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    _j(journey.restore_line, store, rec, ri, body.expect, body.kind, li, body.text, body.story)
    store.save_record(rec)
    return _jstate(store, rec)


@app.get("/new")
def new_page():
    return FileResponse(Path(__file__).parent / "static" / "new.html")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
