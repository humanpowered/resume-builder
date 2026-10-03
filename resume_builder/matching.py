"""
The handover to the job-application pipeline, for one person.

The pipeline was written to read profile/master_profile.json and
config/*.yaml from disk, for one person. Here it reads the person's record
and settings from their store instead, so the hosted product can score a
posting and draft the documents for whoever is signed in:

    result = matching.match(store, posting)

Nothing is written to disk. The profile is built in memory from the record
by the same export the command line uses, so what the pipeline sees is what
`export --write` would have handed it.
"""
import sys
from pathlib import Path

from . import export as X

PIPELINE_SRC = Path(__file__).resolve().parent.parent / "pipeline" / "src"
SETTINGS = "pipeline_settings"          # the store state holding a person's settings


def _pipeline():
    """The pipeline's modules, imported on first use: they are scripts on a
    path rather than a package, and the engine runs without them."""
    if str(PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(PIPELINE_SRC))
    import score_and_tailor
    import settings
    import user_config
    return score_and_tailor, user_config, settings


def settings_for(store):
    """The person's validated settings; the defaults if they set none."""
    _, uc, _ = _pipeline()
    return uc.UserConfig.from_dict(store.load_state(SETTINGS) or {})


def save_settings(store, data: dict):
    """Validate, then store exactly what the person set. Raises the
    pipeline's ConfigError, naming the section, if any of it is wrong."""
    _, uc, _ = _pipeline()
    cfg = uc.UserConfig.from_dict(data)
    store.save_state(SETTINGS, cfg.to_dict())
    return cfg


def profile_for(store) -> dict:
    """The pipeline's profile, built from the record in memory. Drafted
    bullets are kept beside their source, as the export endpoint keeps them,
    so a bullet is compiled once rather than on every posting."""
    st, _, _ = _pipeline()
    rec = store.load_record()
    ex = X.Export(rec, {}, store.load_state("bullets") or {})
    profile = ex.build()
    store.save_record(rec)
    store.save_state("bullets", ex.prints)
    return st.with_skills(profile, st.skills_from_rows(X.skills_rows(rec)))


def match(store, posting: dict, documents: bool = True) -> dict:
    """
    Score one posting for this person and, at or above their threshold,
    draft the tailored resume and cover letter.

    `posting` needs title, company, location and description (plain text or
    HTML).
    """
    st, uc, _ = _pipeline()
    cfg = settings_for(store)
    profile = profile_for(store)
    job = {"title": posting.get("title", ""), "company": posting.get("company", ""),
           "location": posting.get("location", ""),
           "description_html": posting.get("description_html")
           or posting.get("description", "")}
    with uc.using(cfg):
        out = {"score": st.score_posting(job, profile),
               "threshold": st.score_threshold()}
        if documents and out["score"].get("score", 0) >= out["threshold"]:
            out["resume"] = st.tailor_resume(job, profile)
            out["cover_letter"] = st.draft_cover_letter(job, profile)
    return out
