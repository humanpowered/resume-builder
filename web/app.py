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
from resume_builder import health, importer, interview, matching, skills  # noqa: E402
from resume_builder import record as mr  # noqa: E402
from resume_builder.store import SqlStore  # noqa: E402

DB_PATH = os.environ.get("RB_DB", str(ROOT / "web" / "dev.sqlite3"))
DEV = os.environ.get("RB_DEV") == "1"
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


@app.post("/dev/login")
def dev_login(body: DevLogin, response: Response):
    if not DEV:
        raise HTTPException(404)
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter an email address")
    response.set_cookie("rb_user", email, httponly=True, samesite="lax")
    return {"user": email}


# --------------------------------------------------------------------------
# The record


def summary(rec: mr.Record) -> dict:
    return {
        "name": rec.contact.get("Name", ""),
        "roles": [{"employer": r.employer, "title": r.title,
                   "dates": r.fields.get("Dates", ""),
                   "accomplishments": [{"title": a.title, "results": a.results,
                                        "evidence": a.evidence, "bullet": a.bullet}
                                       for a in r.accomplishments],
                   "resume_bullets": r.recorded_bullets} for r in rec.roles],
        "skills": [{"name": s.name, "category": s.category, "level": s.level,
                    "evidence": s.evidence, "have": s.have, "source": s.source,
                    "pending": skills.is_pending(s)}
                   for s in rec.skills],
        "education": rec.education, "certifications": rec.certifications,
    }


@app.get("/api/record")
def get_record(store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    return {"exists": store.exists(), "markdown": mr.render(rec), "summary": summary(rec)}


class RecordText(BaseModel):
    markdown: str


@app.put("/api/record")
def put_record(body: RecordText, store: SqlStore = Depends(store_for)):
    """Save a hand edit. The previous version is kept in history."""
    rec = mr.parse(body.markdown)
    store.save_record(rec)
    return {"summary": summary(rec)}


@app.post("/api/import")
async def import_resume(file: UploadFile = File(...), store: SqlStore = Depends(store_for)):
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
    iv = interview.Interview(store, only=body.role)
    p = iv.step(body.answer)
    return {"text": p.text, "kind": p.kind, "hint": p.hint, "notes": p.notes}


@app.post("/api/interview/restart")
def interview_restart(store: SqlStore = Depends(store_for)):
    store.clear_state(interview.STATE)
    store.clear_state(interview.BACKGROUND_STATE)
    return {"ok": True}


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


@app.patch("/api/skills/{name}")
def skills_change(name: str, body: SkillChange, store: SqlStore = Depends(store_for)):
    rec = store.load_record()
    try:
        s = skills.set_skill(rec, name, body.have, body.level, body.category, body.rename)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    store.save_record(rec)
    return {"name": s.name, "have": s.have, "level": s.level, "category": s.category}


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


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
