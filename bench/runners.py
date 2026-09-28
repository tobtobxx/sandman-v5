"""How each bench `call` is executed. Single calls go straight through
sandman.calls (the same prompts and schemas the harness uses); episodes run
the real worker loop / front desk on scratch state."""
import datetime
import os
import re
import subprocess
from collections import Counter

from sandman import board, calls, memory
from sandman.conversation import Conversation, ask
from sandman.db import DB, new_id, now
from sandman.dispatcher import Dispatcher
from sandman.tools import ArtifactStore, ToolEnv
from sandman.worker import ROLE_TOOLS, describe, result_text, run_worker, transcript_lines

from bench.cases.worker import CODE_FILES, WEB


def worker_step(gw, i, tmp):
    ctx = dict(i["ctx"], tools=ROLE_TOOLS[i["ctx"]["role"]], transcript=transcript_lines(i["steps"]))
    return calls.worker_step(gw, ctx, i["k"], i["n"])


def worker_episode(gw, i, tmp):
    workdir, check = None, None
    if i.get("files"):
        spec = dict(CODE_FILES[i["files"]] if isinstance(i["files"], str) else i["files"])
        check = spec.pop("check")
        workdir = os.path.join(tmp, "code")
        os.makedirs(workdir, exist_ok=True)
        for name, body in spec.items():
            open(os.path.join(workdir, name), "w").write(body)
    arts = ArtifactStore(os.path.join(tmp, "artifacts"))
    ctx = preload(arts, i)
    env = ToolEnv(web=WEB, artifacts=arts, workdir=workdir, notes=i.get("notes") or {})
    final, steps = run_worker(gw, ctx, env, max_turns=i["max_turns"])
    out = {"final": final, "steps": [describe(a) for a, _ in steps], "tools_used": [a["action"] for a, _ in steps],
           "artifacts": env.written,
           "artifact_text": "\n\n".join(arts.read(a, 0, 20000) for a in env.written),
           "last_artifact": arts.read(env.written[-1], 0, 20000) if env.written else ""}
    if check:
        r = subprocess.run(check, shell=True, cwd=workdir, capture_output=True, text=True, timeout=60)
        out["command_ok"] = r.returncode == 0
        out["command_output"] = (r.stdout + r.stderr)[-500:]
    return out


def preload(arts, i):
    """Artifacts given as {name: content}; `{art:name}` in the ctx inputs becomes the artifact id."""
    ctx = dict(i["ctx"])
    ids = {name: arts.write(name, body) for name, body in (i.get("artifacts") or {}).items()}
    sub = lambda t: re.sub(r"\{art:([\w.]+)\}", lambda m: ids[m.group(1)], t)
    ctx["inputs"] = [sub(x) for x in ctx.get("inputs", [])]
    return ctx


def seed_topic(db, conv, i):
    """Setup: a topic with optional cards (state, result) and open questions."""
    topic = conv.new_topic(i.get("topic", "Garden irrigation"))
    cards = []
    for c in i.get("cards", []):
        card = board.create_card(db, c["title"], c.get("goal", c["title"]), origin_topic_id=topic["id"],
                                 state=c.get("state", "new"), created_by="frontdesk")
        if c.get("result"):
            db.update("cards", card["id"], result=c["result"])
        cards.append(db.get("cards", card["id"]))
    for q in i.get("questions", []):
        ask(db, cards[q["card"]], q["text"], q.get("options", []))
    for m in i.get("history", []):
        db.insert("messages", id=new_id("msg"), topic_id=topic["id"], binding="cli", direction=m[0], text=m[1],
                  created_at=now())
    db.x("DELETE FROM outbox")
    return db.get("topics", topic["id"]), cards


def conversation_out(db, seeded=()):
    seeded_ids = {c["id"] for c in seeded}
    slug = lambda tid: (db.get("topics", tid) or {}).get("slug") if tid else None
    return {
        "cards": [{"title": c["title"], "role": c["role"], "goal": c["goal"], "topic": slug(c["origin_topic_id"])}
                  for c in db.q("SELECT * FROM cards WHERE kind='task' ORDER BY created_at") if c["id"] not in seeded_ids],
        "seeded": [{"title": c["title"], "state": db.get("cards", c["id"])["state"],
                    "comments": board.comments(db, c["id"])} for c in seeded],
        "reminders": [{"text": c["goal"], "due_at": c["due_at"], "topic": slug(c["origin_topic_id"])}
                      for c in db.q("SELECT * FROM cards WHERE kind='reminder'")],
        "questions": [{"handle": q["handle"], "status": q["status"], "answer": q["answer"]}
                      for q in db.q("SELECT * FROM questions ORDER BY handle")],
        "replies": [o["body"] for o in db.q("SELECT * FROM outbox WHERE kind IN ('reply','ack') ORDER BY created_at")],
        "topics": [t["slug"] for t in db.q("SELECT * FROM topics ORDER BY created_at")],
    }


def frontdesk_setup_episode(gw, i, tmp):
    db = DB(":memory:")
    conv = Conversation(db, gw, owner="Alex", log=lambda *a: None)
    topic, cards = seed_topic(db, conv, i)
    conv.frontdesk(topic, i["text"])
    return conversation_out(db, cards)


def conversation_episode(gw, i, tmp):
    """Several owner messages through the full inbound path (router + front desk)."""
    db = DB(":memory:")
    conv = Conversation(db, gw, owner="Alex", log=lambda *a: None)
    cards = []
    if i.get("cards") or i.get("topic"):
        _, cards = seed_topic(db, conv, i)
    for m in i["messages"]:
        conv.handle(m)
    return conversation_out(db, cards)


def pipeline(gw, i, tmp):
    """One root card through the real dispatcher: librarian, triage, planner, workers, verifier."""
    counts = Counter()
    log = gw.log
    gw.log = lambda rec: (counts.update([rec["call_type"]]), log(rec))
    db = DB(":memory:")
    for n in i.get("notes", []):
        nid = memory.create_note(db, n["title"], aliases=n.get("aliases", []), one_liner=n.get("one_liner", ""))
        for c in n["claims"]:
            memory.add_claim(db, nid, c["text"], {"type": "url", "ref": c.get("source", "")},
                             observed_at=c.get("observed_at"), volatility=c.get("volatility", "slow"))
        db.update("notes", nid, dirty=0)
        memory.reindex(db, nid)
    conv = Conversation(db, gw, log=lambda *a: None)
    topic = conv.new_topic("Test")
    c = i["card"]
    card = board.create_card(db, c["title"], c["goal"], role=c.get("role", "research"), done_when=c["done_when"],
                             origin_topic_id=topic["id"], created_by="frontdesk")
    Dispatcher(db, gw, WEB, os.path.join(tmp, "ws"), log=lambda *a: None).run_until_idle(max_ticks=i.get("max_ticks", 60))
    card = db.get("cards", card["id"])
    kids = db.q("SELECT * FROM cards WHERE root_id=? AND id!=?", card["id"], card["id"])
    return {"state": card["state"], "result": card["result"], "result_text": result_text(card["result"] or {}),
            "cards": len(kids) + 1, "calls": dict(counts), "worker_steps": counts["worker_step"],
            "states": sorted(k["state"] for k in kids),
            "questions": [q["text"] for q in db.q("SELECT * FROM questions")]}


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
    "frontdesk_setup_episode": frontdesk_setup_episode,
    "conversation_episode": conversation_episode,
    "pipeline": pipeline,
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
