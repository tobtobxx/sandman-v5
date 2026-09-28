"""Offline tests of the harness control flow with a scripted fake model.
These check the code (state machine, routing, memory), not the model."""
import datetime

import jsonschema
import pytest

from bench.corpus import PAGES
from sandman import board, calls, memory
from sandman.conversation import Conversation, Courier, parse_when
from sandman.db import DB
from sandman.dispatcher import Dispatcher
from sandman.llm import LLMFailure
from sandman.tools import FakeWeb


class FakeGW:
    """Answers each call_type from a script: a dict or a list (consumed in order)
    or a function(system, user, schema). Validates against the real schema."""

    def __init__(self, script):
        self.script, self.log = script, []

    def call(self, call_type, system, user, schema, temperature=0.0, check=None, meta=None, max_tokens=None):
        s = self.script.get(call_type)
        if s is None:
            raise LLMFailure(f"no script for {call_type}")
        out = s.pop(0) if isinstance(s, list) else (s(system, user, schema) if callable(s) else s)
        jsonschema.validate(out, schema)
        if check:
            check(out)
        self.log.append((call_type, out))
        return out


@pytest.fixture
def db():
    return DB(":memory:")


def test_transition_table_rejects_invalid(db):
    c = board.create_card(db, "t", "g")
    with pytest.raises(board.BoardError):
        board.transition(db, c["id"], "finish")
    board.transition(db, c["id"], "triage_single")
    assert db.get("cards", c["id"])["state"] == "ready"
    events = db.q("SELECT * FROM card_events WHERE card_id=?", c["id"])
    assert [e["to_state"] for e in events] == ["new", "ready"]


def test_last_turn_schema_only_terminal():
    s = calls.worker_schema("research", ["web_search"], [], [], ["finish", "fail"], allow_tools=False)
    actions = [a["properties"]["action"]["enum"][0] for a in s["anyOf"]]
    assert actions == ["finish", "fail"]


def test_single_card_end_to_end(db, tmp_path):
    gw = FakeGW({
        "triage": {"fits_one_session": "yes", "recipe_id": "none", "missing_info": None},
        "extract_entities": {"entities": ["Gardena"]},
        "worker_step": [
            {"action": "web_search", "query": "gardena micro drip price"},
            {"action": "finish", "summary": "CHF 89.90, covers 15 m².", "sources": ["https://www.gardena.com"],
             "facts": [{"subject": "GARDENA Micro-Drip", "claim": "Costs CHF 89.90", "source": "https://x",
                        "volatility": "volatile"}], "open_questions": []}],
        "verify_criterion": {"reason": "ok", "verdict": "pass"},
    })
    d = Dispatcher(db, gw, FakeWeb(PAGES), str(tmp_path), log=lambda *a: None)
    c = board.create_card(db, "Gardena price", "Find the price", done_when=["states the price"])
    d.run_until_idle()
    c = db.get("cards", c["id"])
    assert c["state"] == "done" and "89.90" in c["result"]["summary"]
    assert db.one("SELECT COUNT(*) n FROM facts")["n"] == 1


def test_verify_fail_retries_then_asks(db, tmp_path):
    fin = {"action": "finish", "summary": "no idea", "sources": [], "facts": [], "open_questions": []}
    gw = FakeGW({"triage": {"fits_one_session": "yes", "recipe_id": "none", "missing_info": None},
                 "extract_entities": {"entities": []}, "worker_step": [fin, fin]})
    topic = Conversation(db, gw).new_topic("Test")
    d = Dispatcher(db, gw, FakeWeb(PAGES), str(tmp_path), log=lambda *a: None)
    c = board.create_card(db, "x", "y", done_when=["cites at least 1 source"], origin_topic_id=topic["id"])
    d.run_until_idle()
    c = db.get("cards", c["id"])
    assert c["state"] == "blocked" and c["attempt"] == 2
    assert db.one("SELECT * FROM questions")["status"] == "open"
    assert any("sources has 0 items" in x for x in board.comments(db, c["id"]))


def test_split_recipe_fanout_and_join(db, tmp_path):
    def worker(system, user, schema):
        if "Your role is the synthesizer" in system:
            return {"action": "finish", "summary": "Gardena wins", "facts": [], "recommendation": "Gardena", "sources": [],
                    "open_questions": []}
        if "Task: Find candidate" in user:
            return {"action": "finish", "summary": "found 2", "sources": ["u"], "open_questions": [],
                    "facts": [{"subject": n, "claim": f"{n} is a kit", "source": "u", "volatility": "slow"}
                              for n in ("Gardena", "Hozelock")]}
        return {"action": "finish", "summary": "details", "sources": ["u"], "facts": [], "open_questions": []}

    gw = FakeGW({
        "extract_entities": {"entities": []},
        "triage": lambda s, u, sc: ({"fits_one_session": "no", "recipe_id": "rcp_research_compare_recommend",
                                     "missing_info": None} if "Goal: Compare drip kits" in u else
                                    {"fits_one_session": "yes", "recipe_id": "none", "missing_info": None}),
        "plan_fill": {"subject": "drip kits", "criteria": "price", "max_options": 2},
        "worker_step": worker, "verify_criterion": {"reason": "ok", "verdict": "pass"},
    })
    d = Dispatcher(db, gw, FakeWeb(PAGES), str(tmp_path), log=lambda *a: None)
    root = board.create_card(db, "Compare drip kits", "Compare drip kits", done_when=["recommends one"])
    d.run_until_idle()
    kids = board.children(db, root["id"])
    assert sorted(k["recipe_step"] or k["kind"] for k in kids) == ["compare", "detail", "detail", "gather", "plan"]
    compare = next(k for k in kids if k["recipe_step"] == "compare")
    assert len(db.q("SELECT * FROM card_deps WHERE card_id=?", compare["id"])) == 3  # gather + 2 details
    root = db.get("cards", root["id"])
    assert root["state"] == "done" and root["phase"] == "synthesize"


def test_router_command_and_sticky(db):
    gw = FakeGW({"route_sticky": {"same": "yes"}, "topic_title": {"title": "Garden irrigation"},
                 "frontdesk_step": {"action": "reply", "text": "ok"},
                 "summarize_topic": {"summary": "s"}, "extract_owner_facts": {"facts": []}})
    conv = Conversation(db, gw, log=lambda *a: None)
    conv.handle("/new Garden irrigation\nhow about drip?")
    conv.handle("and the price?")
    msgs = db.q("SELECT * FROM messages WHERE direction='in' ORDER BY created_at")
    assert [m["routed_by"] for m in msgs] == ["command", "sticky"]
    assert len({m["topic_id"] for m in msgs}) == 1


def test_correction_reparents_cards(db):
    gw = FakeGW({"route_sticky": {"same": "no"},
                 "route_shortlist": {"choice": "garden", "confidence": "low"},
                 "frontdesk_step": [{"action": "create_card", "title": "Tax", "goal": "g", "done_when": ["d"],
                                     "role": "research"}, {"action": "reply", "text": "on it"}],
                 "summarize_topic": {"summary": "s"}, "extract_owner_facts": {"facts": []}})
    conv = Conversation(db, gw, log=lambda *a: None)
    garden, taxes = conv.new_topic("Garden"), conv.new_topic("Taxes")
    conv.handle("file the extension")
    assert db.one("SELECT * FROM cards")["origin_topic_id"] == garden["id"]
    conv.handle("→ taxes")
    assert db.one("SELECT * FROM cards")["origin_topic_id"] == taxes["id"]


def test_question_answer_unblocks(db):
    conv = Conversation(db, FakeGW({}), log=lambda *a: None)
    t = conv.new_topic("Garden")
    c = board.create_card(db, "c", "g", origin_topic_id=t["id"], state="ready")
    board.claim(db, c)
    from sandman.conversation import ask
    ask(db, db.get("cards", c["id"]), "Which one?", ["A", "B"])
    board.transition(db, c["id"], "block")
    conv.handle("Q1: B please")
    assert db.get("cards", c["id"])["state"] == "ready"
    sent = []
    Courier(db, lambda b, o: sent.append(b)).flush()
    assert sent[0].startswith("[garden]")


def test_consolidator_duplicate_and_contradiction(db):
    nid = memory.create_note(db, "Gardena kit")
    c1 = memory.add_claim(db, nid, "covers 15 m²", {"type": "url"}, volatility="evergreen")
    memory.enqueue_facts(db, [{"subject": "Gardena kit", "claim": "covers about 15 m²", "source": "https://a",
                               "volatility": "evergreen"},
                              {"subject": "Gardena kit", "claim": "covers 40 m²", "source": "https://b",
                               "volatility": "evergreen"}])
    keep = {k: False for k in calls.RUBRIC_KEYS} | {"reusable": True, "durable": True}
    gw = FakeGW({"relevance_rubric": keep, "render_note": {"one_liner": "Gardena kit coverage"},
                 "consolidate_fact": [{"decision": "duplicate", "target_claim_id": c1},
                                      {"decision": "contradicts", "target_claim_id": c1}]})
    memory.consolidate(gw, db, log=lambda *a: None)
    assert db.get("claims", c1)["corroborations"] == 1
    assert db.get("claims", c1)["status"] == "disputed"
    assert db.one("SELECT COUNT(*) n FROM review_items")["n"] == 1


def test_keep_rule():
    base = {k: False for k in calls.RUBRIC_KEYS}
    assert calls.keep_fact(base | {"reusable": True, "durable": True})
    assert not calls.keep_fact(base | {"reusable": True, "durable": True, "trivial": True})
    assert calls.keep_fact(base | {"costly": True}, "volatile")
    assert not calls.keep_fact(base | {"reusable": True}, "volatile")


def test_parse_when():
    ref = datetime.datetime(2026, 9, 29, 10, 2)  # a Tuesday
    assert parse_when("friday", ref).date() == datetime.date(2026, 10, 2)
    assert parse_when("tomorrow at 8am", ref) == datetime.datetime(2026, 9, 30, 8, 0)
    assert parse_when("in 2 hours", ref) == datetime.datetime(2026, 9, 29, 12, 2)


def test_worker_repeat_guard(tmp_path):
    from sandman.tools import ArtifactStore, ToolEnv
    from sandman.worker import run_worker
    search = {"action": "web_search", "query": "gardena"}
    gw = FakeGW({"worker_step": [search, search, {"action": "fail", "category": "impossible", "reason": "r"}]})
    env = ToolEnv(web=FakeWeb(PAGES), artifacts=ArtifactStore(str(tmp_path)))
    final, steps = run_worker(gw, {"role": "research", "title": "t", "goal": "g", "done_when": []}, env, max_turns=5)
    assert steps[1][1].startswith("You already did exactly this in step 1")


def test_frontdesk_ack_when_model_forgets_reply(db):
    gw = FakeGW({"frontdesk_step": [{"action": "remind", "when": "tomorrow 8am", "text": "call the plumber"},
                                    {"action": "no_reply"}]})
    conv = Conversation(db, gw, log=lambda *a: None)
    conv.frontdesk(conv.new_topic("Home"), "remind me tomorrow at 8 to call the plumber")
    acks = db.q("SELECT * FROM outbox WHERE kind='ack'")
    assert len(acks) == 1 and acks[0]["body"].startswith("Reminder")


def test_not_repetitive():
    calls.not_repetitive({"content": "def f(x):\n    return x + 1\n" * 3})
    with pytest.raises(ValueError):
        calls.not_repetitive({"content": "email._qprint, " * 300})
