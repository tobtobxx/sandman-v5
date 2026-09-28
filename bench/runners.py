"""How each bench `call` is executed. Single calls go straight through
sandman.calls (the same prompts and schemas the harness uses); episodes run
the real worker loop / front desk on scratch state."""
import datetime
import os
import subprocess

from sandman import board, calls
from sandman.conversation import Conversation, ask
from sandman.db import DB
from sandman.tools import ArtifactStore, ToolEnv
from sandman.worker import ROLE_TOOLS, describe, run_worker, transcript_lines

from bench.cases.worker import CODE_FILES, WEB


def worker_step(gw, i, tmp):
    ctx = dict(i["ctx"], tools=ROLE_TOOLS[i["ctx"]["role"]], transcript=transcript_lines(i["steps"]))
    return calls.worker_step(gw, ctx, i["k"], i["n"])


def worker_episode(gw, i, tmp):
    workdir, check = None, None
    if i.get("files"):
        spec = dict(CODE_FILES[i["files"]])
        check = spec.pop("check")
        workdir = os.path.join(tmp, "code")
        os.makedirs(workdir, exist_ok=True)
        for name, body in spec.items():
            open(os.path.join(workdir, name), "w").write(body)
    arts = ArtifactStore(os.path.join(tmp, "artifacts"))
    env = ToolEnv(web=WEB, artifacts=arts, workdir=workdir)
    final, steps = run_worker(gw, dict(i["ctx"]), env, max_turns=i["max_turns"])
    out = {"final": final, "steps": [describe(a) for a, _ in steps], "artifacts": env.written,
           "artifact_text": "\n\n".join(arts.read(a, 0, 20000) for a in env.written),
           "last_artifact": arts.read(env.written[-1], 0, 20000) if env.written else ""}
    if check:
        r = subprocess.run(check, shell=True, cwd=workdir, capture_output=True, text=True, timeout=60)
        out["command_ok"] = r.returncode == 0
        out["command_output"] = (r.stdout + r.stderr)[-500:]
    return out


def frontdesk_episode(gw, i, tmp):
    db = DB(":memory:")
    conv = Conversation(db, gw, owner="Alex", log=lambda *a: None)
    topic = conv.new_topic("Garden irrigation")
    blocked = None
    if i.get("question"):
        blocked = board.create_card(db, "Compare drip kits", "Compare drip kits for 40 m² beds", origin_topic_id=topic["id"],
                                    state="blocked", created_by="frontdesk")
        ask(db, blocked, i["question"]["text"], i["question"]["options"])
    db.x("DELETE FROM outbox")
    conv.frontdesk(db.get("topics", topic["id"]), i["text"])
    cards = db.q("SELECT * FROM cards WHERE kind='task' AND created_by='frontdesk'")
    out = {"cards": [{"title": c["title"], "role": c["role"], "goal": c["goal"], "done_when": c["done_when"]}
                     for c in cards if not blocked or c["id"] != blocked["id"]],
           "reminders": [{"text": c["goal"], "due_at": c["due_at"]} for c in db.q("SELECT * FROM cards WHERE "
                                                                                    "kind='reminder'")],
           "replies": [o["body"] for o in db.q("SELECT * FROM outbox WHERE kind IN ('reply','ack')")],
           "tomorrow": (datetime.date.today() + datetime.timedelta(days=1)).isoformat()}
    if blocked:
        q = db.one("SELECT * FROM questions")
        out["question_status"] = q["status"]
        out["question_answer"] = q["answer"]
        out["blocked_card_state"] = db.get("cards", blocked["id"])["state"]
    return out


RUNNERS = {
    "route_sticky": lambda gw, i, t: calls.route_sticky(gw, **i),
    "route_shortlist": lambda gw, i, t: calls.route_shortlist(gw, **i),
    "topic_title": lambda gw, i, t: calls.topic_title(gw, **i),
    "frontdesk_step": lambda gw, i, t: calls.frontdesk_step(gw, i["ctx"], i["steps"], i["k"], i["n"]),
    "frontdesk_episode": frontdesk_episode,
    "summarize_topic": lambda gw, i, t: calls.summarize_topic(gw, **i),
    "extract_owner_facts": lambda gw, i, t: calls.extract_owner_facts(gw, **i),
    "extract_entities": lambda gw, i, t: calls.extract_entities(gw, **i),
    "librarian": lambda gw, i, t: calls.librarian(gw, i["card"], i["notes"]),
    "render_answer": lambda gw, i, t: calls.render_answer(gw, i["card"], i["notes"]),
    "triage": lambda gw, i, t: calls.triage(gw, **i),
    "plan_fill": lambda gw, i, t: calls.plan_fill(gw, **i),
    "plan_generate": lambda gw, i, t: calls.plan_generate(gw, **i),
    "worker_step": worker_step,
    "worker_episode": worker_episode,
    "verify_criterion": lambda gw, i, t: calls.verify_criterion(gw, **i),
    "match_subject": lambda gw, i, t: calls.match_subject(gw, **i),
    "relevance_rubric": lambda gw, i, t: calls.relevance_rubric(gw, **i),
    "consolidate_fact": lambda gw, i, t: calls.consolidate_fact(gw, **i),
    "render_note": lambda gw, i, t: calls.render_note(gw, **i),
}
