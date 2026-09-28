"""The board (design §5): cards, dependencies, events, comments.
All state changes go through `transition`, which writes the card and its
event in one transaction."""
import time

from .db import new_id, now

TRANSITIONS = {
    ("new", "librarian_answered"): "verifying",
    ("new", "triage_single"): "ready",
    ("new", "triage_split"): "waiting",
    ("new", "missing_info"): "blocked",
    ("new", "planned"): "done",           # a plan card is done once its subcards exist
    ("new", "plan_failed"): "failed",
    ("waiting", "plan_fallback"): "ready",  # planner failed: parent tries as a single session
    ("ready", "fire"): "done",            # reminders
    ("ready", "claim"): "running",
    ("running", "finish"): "verifying",
    ("running", "split"): "waiting",
    ("running", "block"): "blocked",
    ("running", "checkpoint"): "done",
    ("running", "fail_retry"): "ready",
    ("running", "fail_final"): "failed",
    ("running", "fail_block"): "blocked",
    ("running", "lease_expired"): "ready",
    ("running", "planned"): "done",
    ("running", "plan_failed"): "failed",
    ("verifying", "verify_pass"): "done",
    ("verifying", "verify_fail"): "ready",
    ("verifying", "verify_fail_block"): "blocked",
    ("waiting", "children_done"): "ready",
    ("blocked", "answered"): "ready",
}
TERMINAL = {"done", "failed", "cancelled"}

MAX_DEPTH = 3
MAX_CHILDREN = 5
MAX_ATTEMPTS = 2
MAX_CONTINUATIONS = 4
MAX_OPEN_PER_ROOT = 25
LEASE_SECONDS = 300


class BoardError(Exception):
    pass


def create_card(db, title, goal, role="research", kind="task", done_when=None, constraints=None, inputs=None,
                parent=None, depends_on=(), origin_topic_id=None, priority="normal", created_by="human",
                state="new", **extra):
    if parent:
        root = parent["root_id"]
        open_n = db.one("SELECT COUNT(*) n FROM cards WHERE root_id=? AND state NOT IN ('done','failed','cancelled')",
                        root)["n"]
        if open_n >= MAX_OPEN_PER_ROOT:
            raise BoardError("too many open cards in this tree")
    cid = new_id("crd")
    with db.tx():
        db.insert("cards", id=cid, kind=kind, role=role, title=title, goal=goal, done_when=done_when or [],
                  constraints=constraints or [], inputs=inputs or [], depth=(parent["depth"] + 1) if parent else 0,
                  parent_id=parent["id"] if parent else None, root_id=parent["root_id"] if parent else cid,
                  origin_topic_id=origin_topic_id or (parent and parent["origin_topic_id"]), state=state,
                  priority=priority, created_by=created_by, created_at=now(), **extra)
        for d in depends_on:
            db.insert("card_deps", card_id=cid, depends_on=d)
        db.insert("card_events", card_id=cid, from_state=None, to_state=state, event="create",
                  payload={"by": created_by}, at=now())
    return db.get("cards", cid)


def transition(db, card_id, event, payload=None, **fields):
    with db.tx():
        c = db.get("cards", card_id)
        if event == "cancel":
            to = "cancelled"
            if c["state"] in TERMINAL:
                raise BoardError(f"{card_id} already {c['state']}")
        else:
            to = TRANSITIONS.get((c["state"], event))
            if to is None:
                raise BoardError(f"no transition {c['state']} --{event}-->")
        if to != "running":
            fields.setdefault("lease_owner", None)
            fields.setdefault("lease_expires_at", None)
        db.update("cards", card_id, state=to, **fields)
        db.insert("card_events", card_id=card_id, from_state=c["state"], to_state=to, event=event,
                  payload=payload, at=now())
    return db.get("cards", card_id)


def cancel_tree(db, card_id):
    for ch in db.q("SELECT id FROM cards WHERE parent_id=?", card_id):
        cancel_tree(db, ch["id"])
    c = db.get("cards", card_id)
    if c["state"] not in TERMINAL:
        transition(db, card_id, "cancel")


def comment(db, card_id, author, body):
    db.insert("comments", id=new_id("cmt"), card_id=card_id, author=author, body=body, created_at=now())


def comments(db, card_id):
    return [f"{r['author']}: {r['body']}" for r in db.q("SELECT * FROM comments WHERE card_id=? ORDER BY created_at",
                                                          card_id)]


def children(db, card_id):
    return db.q("SELECT * FROM cards WHERE parent_id=? ORDER BY created_at", card_id)


def deps_done(db, card_id):
    r = db.one("""SELECT COUNT(*) n FROM card_deps d JOIN cards c ON c.id=d.depends_on
                  WHERE d.card_id=? AND c.state NOT IN ('done','failed','cancelled')""", card_id)
    return r["n"] == 0


PRIO = "CASE priority WHEN 'interactive' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END"


def next_runnable(db):
    """Highest-priority card the dispatcher can act on."""
    for c in db.q(f"SELECT * FROM cards WHERE state IN ('new','ready','verifying') ORDER BY {PRIO}, created_at"):
        if c["kind"] == "reminder":
            continue
        if deps_done(db, c["id"]):
            return c
    return None


def claim(db, card, owner="dispatcher"):
    return transition(db, card["id"], "claim", lease_owner=owner, lease_expires_at=time.time() + LEASE_SECONDS)


def renew(db, card_id):
    db.update("cards", card_id, lease_expires_at=time.time() + LEASE_SECONDS)


def reclaim_expired(db):
    for c in db.q("SELECT * FROM cards WHERE state='running' AND lease_expires_at < ?", time.time()):
        transition(db, c["id"], "lease_expired", attempt=c["attempt"] + 1)
