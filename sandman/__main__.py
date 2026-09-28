"""CLI: python -m sandman <command>

  chat                 talk to the front desk; cards run between your messages
  card TITLE GOAL      create a root card and run it to completion
  board                list cards
  show CARD            card contract, events, comments, result
  consolidate          run the memory consolidator now
  notes                list memory notes and claims
  web [--port 8080]    read-only web UI over everything in --home
"""
import argparse
import json
import os
import sys

from . import board, memory
from .conversation import Conversation, Courier
from .db import DB
from .dispatcher import Dispatcher
from .llm import Gateway
from .tools import FakeWeb, RealWeb


def setup(a):
    os.makedirs(a.home, exist_ok=True)
    db = DB(os.path.join(a.home, "sandman.db"))
    conn = dict(base_url=a.base_url, api_key=a.api_key, reasoning=a.thinking, idle_timeout=a.idle_timeout)
    gw = Gateway(model=a.model, log=db.log_call, **conn)
    large = Gateway(model=a.large_model, log=db.log_call, **dict(conn, base_url=a.large_base_url or a.base_url)) \
        if a.large_model else None
    if a.fake_web == "bench":
        from bench.corpus import PAGES
        web = FakeWeb(PAGES)
    else:
        web = FakeWeb(json.load(open(a.fake_web))) if a.fake_web else RealWeb()
    log = print if a.verbose else (lambda *x: None)
    disp = Dispatcher(db, gw, web, os.path.join(a.home, "workspace"), gw_large=large, log=log)
    return db, gw, disp, log


def main():
    p = argparse.ArgumentParser(prog="sandman")
    p.add_argument("--home", default=os.environ.get("SANDMAN_HOME", ".sandman"))
    p.add_argument("--model", default=None, help="model name (default $SANDMAN_MODEL or qwen/qwen3.6-35b-a3b)")
    p.add_argument("--base-url", default=None,
                   help="OpenAI-compatible endpoint, e.g. http://localhost:8080/v1 (default $SANDMAN_BASE_URL or OpenRouter)")
    p.add_argument("--api-key", default=None, help="default $SANDMAN_API_KEY; $OPENROUTER_API_KEY only for OpenRouter")
    p.add_argument("--thinking", action="store_true", help="let the model think (default off)")
    p.add_argument("--idle-timeout", type=float, default=None,
                   help="local servers: give up after this many seconds without data (default 600)")
    p.add_argument("--large-base-url", default=None, help="endpoint for --large-model (default: --base-url)")
    p.add_argument("--large-model", default=os.environ.get("SANDMAN_LARGE_MODEL"))
    p.add_argument("--fake-web", help="JSON corpus [{url,title,text}] or 'bench' instead of the live web")
    p.add_argument("-q", "--quiet", dest="verbose", action="store_false")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chat")
    c = sub.add_parser("card")
    c.add_argument("title")
    c.add_argument("goal")
    c.add_argument("--role", default="research")
    c.add_argument("--done-when", action="append", default=[])
    sub.add_parser("board")
    s = sub.add_parser("show")
    s.add_argument("card")
    sub.add_parser("consolidate")
    sub.add_parser("notes")
    w = sub.add_parser("web")
    w.add_argument("--port", type=int, default=8080)
    w.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()
    if a.cmd == "web":
        from .web import serve
        return serve(a.home, a.host, a.port)
    db, gw, disp, log = setup(a)

    if a.cmd == "chat":
        conv = Conversation(db, gw, owner=os.environ.get("SANDMAN_OWNER", "the owner"), log=log)
        courier = Courier(db, lambda body, o: print(f"\n« {body}\n"))
        print("Sandman chat. Commands: /topics /status [slug] /new TITLE /t SLUG msg, → slug, "
              "/consolidate, /quit")
        while True:
            try:
                text = input("» ")
            except EOFError:
                break
            if text.strip() in ("/quit", "/exit"):
                break
            if text.strip() == "/consolidate":
                memory.consolidate(gw, db, log=print)
                continue
            conv.handle(text)
            courier.flush()
            disp.run_until_idle(after_tick=courier.flush)
            courier.flush()
        print(f"(cost ${gw.cost:.4f}, {gw.calls} calls)")

    elif a.cmd == "card":
        card = board.create_card(db, a.title, a.goal, role=a.role, done_when=a.done_when, created_by="human")
        disp.run_until_idle()
        card = db.get("cards", card["id"])
        print(json.dumps({"state": card["state"], "result": card["result"]}, indent=1, ensure_ascii=False))
        print(f"(cost ${gw.cost:.4f}, {gw.calls} calls)")

    elif a.cmd == "board":
        for c in db.q("SELECT * FROM cards WHERE kind != 'reminder' ORDER BY root_id, created_at"):
            print(f"{'  ' * c['depth']}{c['id']} {c['state']:10} {c['kind']:5} {c['role']:10} {c['title']}")

    elif a.cmd == "show":
        c = db.get("cards", a.card)
        print(json.dumps(c, indent=1, ensure_ascii=False))
        for e in db.q("SELECT * FROM card_events WHERE card_id=? ORDER BY id", a.card):
            print(f"  {e['at']} {e['from_state']} --{e['event']}--> {e['to_state']} {e['payload'] or ''}")
        for cm in board.comments(db, a.card):
            print("  comment:", cm)

    elif a.cmd == "consolidate":
        memory.consolidate(gw, db, log=print)

    elif a.cmd == "notes":
        for n in db.q("SELECT * FROM notes WHERE status='active'"):
            print(memory.note_text(db, n["id"]))


if __name__ == "__main__":
    sys.exit(main())
