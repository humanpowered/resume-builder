"""
Does the builder work for people who are not Craig?

For each fictional person in testset/: import the resume they would arrive
with, then run the real interview against a model playing that person. The
player answers only from the person's truth.json, the way a real person
would: briefly, without volunteering numbers nobody asked for, and saying
"I don't know" to anything not in their file.

Then score the record that results against the truth:

  recall          share of true accomplishments that made it into the record
  quantified      of the true accomplishments that have a figure, the share
                  whose recorded result carries one
  fabricated      figures in the record that appear nowhere in the person's
                  truth or starting resume, and any do_not_claim item that
                  surfaced. The target is zero.
  effort          questions the person was asked per accomplishment recorded

Needs an API key and costs real money (roughly $1-3 per person at the default
model). Run one person first:

  python evals/simulate.py --people 01 --max-questions 60
  python evals/simulate.py                       # all eight

Results go to evals/results/<timestamp>/: each person's record.md, the full
transcript, and scores.json; plus summary.md across everyone.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from resume_builder import bullets, importer, interview, llm  # noqa: E402
from resume_builder import record as mr  # noqa: E402

TESTSET = ROOT / "evals" / "testset"

PLAYER = """You are playing a job seeker being interviewed about their career.
Everything you know about yourself is in YOUR FILE below. Stay in character.

How to answer:
- Answer only from YOUR FILE. If asked about something not in it, say you
  don't remember or it didn't happen. Never invent a fact, number, employer,
  skill or result.
- Be a realistic person: short answers, one or two sentences. Do not recite
  your whole file. Do not volunteer a number unless the question asks for
  something it answers.
- Where your file gives a before-and-after (e.g. "used to take 45 minutes,
  now 25") but no calculated figure, give the before-and-after when asked;
  do not calculate a percentage yourself. If the interviewer proposes a
  figure, say yes only if it follows from your file.
- If the interviewer asks about something already covered, or you have
  nothing more for this job, say "That's all I have for that job."
- When asked to list your jobs one at a time, give the next one you have not
  given yet as just what is asked (employer, or title, or dates). When you
  have given them all, answer exactly: DONE
- Anything in do_not_claim is something you do NOT have. If asked, say no.

YOUR FILE
{truth}

THE CONVERSATION SO FAR
{history}

THE INTERVIEWER NOW ASKS
{question}

Reply with only what you would say."""


class Player:
    def __init__(self, truth: dict, limit: int):
        self.truth = json.dumps(truth, indent=1)
        self.history = []
        self.limit = limit
        self.asked = 0

    def ask(self, question: str) -> str:
        # Mechanical prompts a real user answers with a keypress.
        if question.startswith("From your resume:"):
            return ""                            # yes, open it up
        if question.startswith("You stopped part-way"):
            return ""
        self.asked += 1
        if self.asked > self.limit:
            raise interview.Stop()
        q = question.split("\n  (Enter to skip)")[0]
        reply = llm.request_json([{"role": "user", "content": PLAYER.format(
            truth=self.truth, history="\n".join(self.history[-30:]) or "(none)",
            question=q) + '\n\nReturn JSON: {"answer": "..."}'}],
            1024, "player", schema={"type": "object", "properties": {
                "answer": {"type": "string"}}, "required": ["answer"],
                "additionalProperties": False})
        answer = (reply.get("answer") or "").strip()
        self.history += [f"Interviewer: {q}", f"Me: {answer}"]
        if answer.upper().startswith("DONE"):
            return ""
        if "that's all i have" in answer.lower():
            return "done"
        return answer


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"matches": {"type": "array", "items": {"type": "object", "properties": {
        "truth_title": {"type": "string"},
        "recorded_title": {"type": "string"}},
        "required": ["truth_title", "recorded_title"], "additionalProperties": False}},
        "claimed": {"type": "array", "items": {"type": "string"}}},
    "required": ["matches", "claimed"],
    "additionalProperties": False,
}

JUDGE = """Match a career record against the truth it was built from.

For each accomplishment in TRUTH, find the RECORDED accomplishment (or
recorded resume bullet) that describes the same piece of work, even if
worded differently. Give its title or bullet text exactly; use "" if none.

Then list in "claimed" any item from DO_NOT_CLAIM that the RECORD asserts the
person has or did.

TRUTH
{truth}

DO_NOT_CLAIM
{dnc}

RECORD
{record}"""


def score(truth: dict, rec: mr.Record, resume_text: str, asked: int) -> dict:
    true_accs = [a for r in truth.get("roles", []) for a in r.get("accomplishments", [])]
    recorded = {a.title: a for _, a in rec.all_accomplishments()}
    rbullets = [b for r in rec.roles for b in r.recorded_bullets]
    verdict = llm.request_json([{"role": "user", "content": JUDGE.format(
        truth=json.dumps([{k: a.get(k) for k in ("title", "results")} for a in true_accs], indent=1),
        dnc=json.dumps(truth.get("do_not_claim", [])),
        record=mr.render(rec))}], 8000, "judge", schema=JUDGE_SCHEMA)
    matched = {m["truth_title"]: m["recorded_title"] for m in verdict.get("matches", [])
               if m.get("recorded_title")}

    with_figure = [a for a in true_accs if bullets.has_figure(a.get("results", ""))]
    quantified = 0
    for a in with_figure:
        got = recorded.get(matched.get(a["title"], ""))
        if got and bullets.has_figure(got.results):
            quantified += 1
        elif matched.get(a["title"]) in rbullets and bullets.has_figure(matched[a["title"]]):
            quantified += 1

    known = json.dumps(truth) + " " + resume_text
    invented = set()
    for _, a in rec.all_accomplishments():
        invented |= bullets.ungrounded(a.results + " " + a.actions + " " + a.bullet, known)
    n_rec = len(recorded) + len(rbullets)
    return {
        "true_accomplishments": len(true_accs),
        "recorded": n_rec,
        "full_records": len(recorded),
        "recall": round(len(matched) / max(1, len(true_accs)), 2),
        "quantified": round(quantified / max(1, len(with_figure)), 2),
        "fabricated_figures": sorted(invented),
        "do_not_claim_hits": verdict.get("claimed", []),
        "questions": asked,
        "questions_per_accomplishment": round(asked / max(1, len(recorded)), 1),
        "unmatched_truth": [a["title"] for a in true_accs if a["title"] not in matched],
    }


def run_person(folder: Path, out: Path, limit: int) -> dict:
    truth = json.loads((folder / "truth.json").read_text(encoding="utf-8"))
    resume = (folder / "starting_resume.txt").read_text(encoding="utf-8")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "record.md"
    if resume.strip().upper() != "NONE":
        rec, _ = importer.to_record(importer.extract(resume), resume, "starting_resume.txt",
                                    f"{datetime.now():%Y-%m-%d}")
        path.write_text(mr.render(rec), encoding="utf-8")
    else:
        resume = ""
    player = Player(truth, limit)
    log = []
    s = interview.Session(path, ask=player.ask, say=lambda *a: log.append(" ".join(map(str, a))))
    try:
        s.run()
    except interview.Stop:
        log.append("[question limit reached]")
    rec = mr.parse(path.read_text(encoding="utf-8"))
    result = score(truth, rec, resume, player.asked)
    (out / "transcript.txt").write_text("\n".join(player.history), encoding="utf-8")
    (out / "scores.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--people", nargs="*", help="folder prefixes, e.g. 01 04")
    ap.add_argument("--max-questions", type=int, default=120)
    args = ap.parse_args()
    llm.require_credentials()
    folders = sorted(p for p in TESTSET.iterdir() if (p / "truth.json").exists())
    if args.people:
        folders = [f for f in folders if any(f.name.startswith(x) for x in args.people)]
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    results = {}
    for f in folders:
        print(f"\n== {f.name}")
        r = run_person(f, ROOT / "evals" / "results" / stamp / f.name, args.max_questions)
        results[f.name] = r
        print(f"   recall {r['recall']:.0%}, quantified {r['quantified']:.0%}, "
              f"fabricated {len(r['fabricated_figures']) + len(r['do_not_claim_hits'])}, "
              f"{r['questions_per_accomplishment']} questions/accomplishment")
    lines = ["| Person | Recall | Quantified | Fabricated | Questions / acc. |",
             "|---|---:|---:|---:|---:|"]
    for name, r in results.items():
        lines.append(f"| {name} | {r['recall']:.0%} | {r['quantified']:.0%} | "
                     f"{len(r['fabricated_figures']) + len(r['do_not_claim_hits'])} | "
                     f"{r['questions_per_accomplishment']} |")
    summary = ROOT / "evals" / "results" / stamp / "summary.md"
    summary.write_text("\n".join(lines) + f"\n\n{llm.spend_summary()}\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\n{llm.spend_summary()}\n{summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
