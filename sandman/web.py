"""Read-only web UI: every detail of the Sandman runtime, inspectable.

    python -m sandman --home .sandman web --port 8080

Standard library only. Pages are rendered from the SQLite database and the
workspace; nothing here writes state. Every id (crd_, top_, not_, ses_, cal_,
...) anywhere on a page is a link.
"""
import datetime
import html
import json
import os
import re
import statistics
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import board, calls, conversation, dispatcher, memory, recipes, verifier, worker
from .db import DB

ID_RE = re.compile(r"\b(crd|top|not|clm|ses|cal|qst|out|art|fct|msg|tcl|rev)_[0-9a-f]{6,}\b")
STATE_COLORS = {"new": "#8a8f98", "ready": "#3b82f6", "running": "#f59e0b", "waiting": "#a855f7",
                "blocked": "#ef4444", "verifying": "#06b6d4", "done": "#22c55e", "failed": "#b91c1c",
                "cancelled": "#6b7280", "open": "#ef4444", "answered": "#22c55e", "pending": "#f59e0b",
                "sent": "#22c55e", "active": "#22c55e", "superseded": "#6b7280", "disputed": "#f59e0b",
                "retracted": "#b91c1c", "merged": "#22c55e", "discarded": "#6b7280", "review": "#f59e0b"}
NAV = [("/", "Overview"), ("/board", "Board"), ("/topics", "Topics"), ("/outbox", "Outbox"),
       ("/questions", "Questions"), ("/sessions", "Sessions"), ("/calls", "LLM calls"), ("/stats", "Stats"),
       ("/memory", "Memory"), ("/facts", "Facts"), ("/artifacts", "Artifacts"), ("/events", "Events"),
       ("/prompts", "Prompts & roles"), ("/config", "Config")]

CSS = """
:root{--bg:#fafafa;--fg:#1f2328;--muted:#656d76;--card:#fff;--line:#d8dee4;--code:#f3f4f6;--link:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;--card:#161b22;
 --line:#30363d;--code:#1f242c;--link:#58a6ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);
 padding:8px 16px;display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center}
header b{margin-right:8px}header form{margin-left:auto}header input{padding:3px 6px;border:1px solid var(--line);
 background:var(--bg);color:var(--fg);border-radius:4px;width:190px}
main{padding:16px;max-width:1500px;margin:0 auto}h1{font-size:20px;margin:4px 0 12px}h2{font-size:16px;margin:22px 0 8px}
table{border-collapse:collapse;width:100%;background:var(--card);margin-bottom:8px}
th,td{border:1px solid var(--line);padding:4px 7px;text-align:left;vertical-align:top}
th{background:var(--code);font-weight:600;white-space:nowrap}td.num{text-align:right;font-variant-numeric:tabular-nums}
pre{background:var(--code);padding:8px;border-radius:4px;overflow:auto;white-space:pre-wrap;word-break:break-word;
 margin:4px 0;max-height:640px;font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}
.badge{display:inline-block;padding:0 7px;border-radius:9px;color:#fff;font-size:12px;white-space:nowrap}
.muted{color:var(--muted)}.tiles{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 12px;min-width:120px;color:var(--fg)}
a.tile:hover{text-decoration:none;border-color:var(--link)}
.tile div:first-child{font-size:20px;font-weight:600}.tile div:last-child{color:var(--muted);font-size:12px}
details{margin:3px 0}summary{cursor:pointer}.tree{list-style:none;padding-left:18px;margin:2px 0}
.tree li{margin:2px 0}.cols{display:flex;gap:10px;overflow-x:auto}.col{min-width:220px;flex:1}
.col .c{background:var(--card);border:1px solid var(--line);border-radius:5px;padding:5px 7px;margin-bottom:6px}
.msg{border-left:3px solid var(--line);padding:4px 10px;margin:6px 0;background:var(--card)}
.msg.in{border-color:#3b82f6}.msg.out{border-color:#22c55e}.bad{color:#ef4444}.ok{color:#22c55e}
.filters a{margin-right:8px}.kv th{width:170px}
.step{border:1px solid var(--line);border-radius:5px;padding:6px 10px;margin:8px 0;background:var(--card)}
@media (max-width:700px){main{padding:8px}header form{margin-left:0}}
"""


# ---------- helpers ----------

def esc(v):
    return html.escape("" if v is None else str(v))


def linkify(escaped):
    return ID_RE.sub(lambda m: f'<a href="/id/{m.group(0)}">{m.group(0)}</a>', escaped)


def t(v):
    """Text cell: escaped, ids linked."""
    return linkify(esc(v))


def pre(v, open_=True, label=None, limit=None):
    if v is None or v == "":
        return '<span class="muted">(empty)</span>'
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1, default=str)
    body = f"<pre>{linkify(esc(s))}</pre>"
    if label or not open_ or (limit and len(s) > limit):
        return f"<details{' open' if open_ else ''}><summary>{esc(label or f'{len(s)} chars')}</summary>{body}</details>"
    return body


def badge(state):
    return f'<span class="badge" style="background:{STATE_COLORS.get(state, "#6b7280")}">{esc(state)}</span>'


def table(rows, cols, empty="none"):
    """cols: [(header, fn(row) -> html)]"""
    if not rows:
        return f'<p class="muted">{esc(empty)}</p>'
    h = "".join(f"<th>{esc(c)}</th>" for c, _ in cols)
    b = "".join("<tr>" + "".join(f"<td>{fn(r)}</td>" for _, fn in cols) + "</tr>" for r in rows)
    return f"<table><tr>{h}</tr>{b}</table>"


def kv(d, keys=None):
    keys = keys or list(d)
    rows = []
    for k in keys:
        v = d.get(k)
        cell = pre(v) if isinstance(v, (dict, list)) else t(v)
        rows.append(f"<tr><th>{esc(k)}</th><td>{cell}</td></tr>")
    return f'<table class="kv">{"".join(rows)}</table>'


def money(v):
    return f"${v:.5f}" if v else "—"


def short(s, n=120):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + "…"


def qs_link(path, params, **change):
    p = {**params, **change}
    p = {k: v for k, v in p.items() if v not in (None, "")}
    return path + ("?" + urllib.parse.urlencode(p) if p else "")


# ---------- the app ----------

class App:
    def __init__(self, home):
        self.home = home
        self.workspace = os.path.join(home, "workspace")
        self.db = DB(os.path.join(home, "sandman.db"))

    def page(self, title, body, refresh=None):
        nav = " ".join(f'<a href="{p}">{esc(n)}</a>' for p, n in NAV)
        meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}<title>{esc(title)} · Sandman</title>
<style>{CSS}</style></head><body><header><b>Sandman</b>{nav}
<a href="?refresh=5" title="reload this page every 5 s">auto-refresh</a>
<form action="/find"><input name="q" placeholder="id or text (notes, messages)"></form></header>
<main><h1>{linkify(esc(title))}</h1>{body}</main></body></html>"""

    # ----- routing -----
    def route(self, path, params):
        routes = [
            (r"/", self.overview), (r"/board", self.board), (r"/card/(crd_\w+)", self.card),
            (r"/topics", self.topics), (r"/topic/(top_\w+)", self.topic), (r"/outbox", self.outbox),
            (r"/questions", self.questions), (r"/sessions", self.sessions), (r"/session/(ses_\w+)", self.session),
            (r"/calls", self.calls), (r"/call/(cal_\w+)", self.call), (r"/stats", self.stats),
            (r"/memory", self.memory), (r"/note/(not_\w+)", self.note), (r"/facts", self.facts),
            (r"/artifacts", self.artifacts), (r"/artifact/(art_\w+)", self.artifact), (r"/events", self.events),
            (r"/prompts", self.prompts), (r"/config", self.config), (r"/id/(\w+)", self.by_id),
            (r"/find", self.find),
        ]
        for pat, fn in routes:
            m = re.fullmatch(pat, path)
            if m:
                return fn(params, *m.groups())
        return 404, self.page("Not found", f"<p>No page {esc(path)}</p>")

    def by_id(self, params, i):
        db = self.db
        p = i.split("_", 1)[0]
        target = {"crd": f"/card/{i}", "top": f"/topic/{i}", "not": f"/note/{i}", "ses": f"/session/{i}",
                  "cal": f"/call/{i}", "art": f"/artifact/{i}"}.get(p)
        if p == "clm":
            c = db.get("claims", i)
            target = f"/note/{c['note_id']}#{i}" if c else None
        elif p == "qst":
            q = db.get("questions", i)
            target = f"/card/{q['card_id']}#questions" if q else None
        elif p == "out":
            target = f"/outbox#{i}"
        elif p == "msg":
            m = db.get("messages", i)
            target = f"/topic/{m['topic_id']}#{i}" if m and m["topic_id"] else None
        elif p == "fct":
            target = f"/facts#{i}"
        elif p == "tcl":
            tc = db.get("tool_calls", i)
            target = f"/session/{tc['session_id']}#{i}" if tc else None
        elif p == "rev":
            target = "/memory#reviews"
        if not target:
            return 404, self.page("Not found", f"<p>Unknown id {esc(i)}</p>")
        return 302, target

    def find(self, params):
        q = (params.get("q") or "").strip()
        if ID_RE.fullmatch(q):
            return self.by_id(params, q)
        db = self.db
        like = f"%{q}%"
        notes = [db.get("notes", i) for i in memory.search(db, q, 20)]
        body = "<h2>Notes</h2>" + table(notes, [("note", lambda r: t(r["id"])), ("title", lambda r: t(r["title"])),
                                         ("one-liner", lambda r: t(r["one_liner"]))])
        body += "<h2>Cards</h2>" + table(db.q("SELECT * FROM cards WHERE title LIKE ? OR goal LIKE ? LIMIT 50", like, like),
                                         self.card_cols())
        body += "<h2>Messages</h2>" + table(db.q("SELECT * FROM messages WHERE text LIKE ? ORDER BY created_at DESC LIMIT 50", like),
                                            [("msg", lambda r: t(r["id"])), ("topic", lambda r: t(r["topic_id"])),
                                             ("dir", lambda r: esc(r["direction"])), ("text", lambda r: t(short(r["text"], 200)))])
        return 200, self.page(f"Search: {q}", body)

    # ----- overview -----
    def overview(self, params):
        db = self.db
        n = lambda sql, *a: db.one(sql, *a)["n"]
        tiles = [
            (n("SELECT COUNT(*) n FROM cards WHERE kind!='reminder'"), "cards", "/board"),
            (n("SELECT COUNT(*) n FROM cards WHERE state IN ('ready','running','verifying','new')"), "cards active", "/board?view=columns"),
            (n("SELECT COUNT(*) n FROM cards WHERE state='blocked'"), "cards blocked", "/board?state=blocked"),
            (n("SELECT COUNT(*) n FROM topics"), "topics", "/topics"),
            (n("SELECT COUNT(*) n FROM questions WHERE status='open'"), "open questions", "/questions"),
            (n("SELECT COUNT(*) n FROM outbox WHERE status='pending'"), "outbox pending", "/outbox"),
            (n("SELECT COUNT(*) n FROM notes WHERE status='active'"), "notes", "/memory"),
            (n("SELECT COUNT(*) n FROM claims WHERE status='active'"), "active claims", "/memory"),
            (n("SELECT COUNT(*) n FROM facts WHERE status='pending'"), "facts pending", "/facts?status=pending"),
            (n("SELECT COUNT(*) n FROM sessions"), "sessions", "/sessions"),
            (n("SELECT COUNT(*) n FROM llm_calls"), "LLM calls", "/calls"),
            (n("SELECT COUNT(*) n FROM llm_calls WHERE ok=0"), "invalid LLM outputs", "/calls?ok=0"),
            (money(db.one("SELECT SUM(cost) n FROM llm_calls")["n"]), "LLM cost", "/stats"),
        ]
        body = '<div class="tiles">' + "".join(f'<a class=tile href="{h}"><div>{esc(v)}</div><div>{esc(k)}</div></a>'
                                               for v, k, h in tiles) + "</div>"
        states = db.q("SELECT state, COUNT(*) n FROM cards WHERE kind!='reminder' GROUP BY state")
        body += "<p>" + " ".join(f'<a href="/board?state={s["state"]}">{badge(s["state"])} {s["n"]}</a>' for s in states) + "</p>"
        running = db.q("SELECT * FROM cards WHERE state='running'")
        if running:
            body += "<h2>Running now</h2>" + table(running, self.card_cols() + [
                ("lease until", lambda r: esc(datetime.datetime.fromtimestamp(r["lease_expires_at"]).strftime("%H:%M:%S")
                                              if r["lease_expires_at"] else ""))])
        body += "<h2>Recent sessions</h2>" + self.session_table(db.q("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 15"))
        body += "<h2>Recent card events</h2>" + self.event_table(db.q("SELECT * FROM card_events ORDER BY id DESC LIMIT 25"))
        body += "<h2>Recent LLM calls</h2>" + self.call_table(db.q("SELECT * FROM llm_calls ORDER BY at DESC, rowid DESC LIMIT 15"))
        return 200, self.page("Overview", body, params.get("refresh"))

    # ----- board -----
    def card_cols(self):
        return [("card", lambda r: t(r["id"])), ("state", lambda r: badge(r["state"])),
                ("kind/role", lambda r: esc(f"{r['kind']}/{r['role']}")), ("title", lambda r: t(r["title"])),
                ("depth", lambda r: esc(r["depth"])), ("attempt", lambda r: esc(r["attempt"])),
                ("phase", lambda r: esc(r["phase"])), ("topic", lambda r: t(r["origin_topic_id"])),
                ("created", lambda r: esc(r["created_at"]))]

    def board(self, params):
        db = self.db
        view, state, topic = params.get("view", "tree"), params.get("state"), params.get("topic")
        where, args = ["kind!='reminder'"], []
        if state:
            where.append("state=?"), args.append(state)
        if topic:
            where.append("origin_topic_id=?"), args.append(topic)
        cards = db.q(f"SELECT * FROM cards WHERE {' AND '.join(where)} ORDER BY created_at", *args)
        states = [s["state"] for s in db.q("SELECT DISTINCT state FROM cards")]
        filt = ('<p class="filters">view: ' + " ".join(f'<a href="{qs_link("/board", params, view=v)}">{v}</a>'
                                                      for v in ("tree", "columns", "table"))
                + " · state: " + f'<a href="{qs_link("/board", params, state=None)}">all</a> '
                + " ".join(f'<a href="{qs_link("/board", params, state=s)}">{badge(s)}</a>' for s in states) + "</p>")
        if view == "table" or state:
            body = table(cards, self.card_cols())
        elif view == "columns":
            order = ["new", "ready", "running", "waiting", "blocked", "verifying", "done", "failed", "cancelled"]
            body = '<div class="cols">' + "".join(
                f'<div class="col"><h2>{badge(s)} {sum(1 for c in cards if c["state"] == s)}</h2>' + "".join(
                    f'<div class="c">{t(c["id"])}<br>{esc(c["title"])}<br><span class="muted">'
                    f'{esc(c["kind"])}/{esc(c["role"])} · depth {c["depth"]} · attempt {c["attempt"]}</span></div>'
                    for c in cards if c["state"] == s) + "</div>" for s in order) + "</div>"
        else:
            kids = {}
            for c in cards:
                kids.setdefault(c["parent_id"], []).append(c)
            ids = {c["id"] for c in cards}

            def tree(parent):
                items = []
                for c in kids.get(parent, []):
                    items.append(f'<li>{badge(c["state"])} {t(c["id"])} <b>{esc(c["title"])}</b> '
                                 f'<span class="muted">{esc(c["kind"])}/{esc(c["role"])} · attempt {c["attempt"]}'
                                 f'{" · " + esc(c["recipe_step"]) if c["recipe_step"] else ""}'
                                 f'{" · phase " + esc(c["phase"]) if c["phase"] != "execute" else ""}</span>'
                                 f'{tree(c["id"])}</li>')
                return f'<ul class="tree">{"".join(items)}</ul>' if items else ""
            roots = [c for c in cards if c["parent_id"] not in ids]
            body = "".join(f'<h2>{t(r["id"])} {esc(r["title"])} {badge(r["state"])} '
                           f'<span class="muted">topic {t(r["origin_topic_id"])}</span></h2>'
                           + tree(r["id"]) for r in roots if not r["parent_id"] or r["parent_id"] not in ids) \
                or '<p class="muted">no cards</p>'
            reminders = db.q("SELECT * FROM cards WHERE kind='reminder' ORDER BY due_at")
            if reminders:
                body += "<h2>Reminders</h2>" + table(reminders, [
                    ("card", lambda r: t(r["id"])), ("state", lambda r: badge(r["state"])),
                    ("due", lambda r: esc(r["due_at"])), ("text", lambda r: t(r["goal"])),
                    ("topic", lambda r: t(r["origin_topic_id"]))])
        return 200, self.page("Board", filt + body, params.get("refresh"))

    def card(self, params, cid):
        db = self.db
        c = db.get("cards", cid)
        if not c:
            return 404, self.page("Not found", "")
        chain, p = [], c
        while p and p["parent_id"]:
            p = db.get("cards", p["parent_id"])
            if p:
                chain.append(p)
        body = (f"<p>{badge(c['state'])} <b>{esc(c['title'])}</b> · {esc(c['kind'])}/{esc(c['role'])} · depth "
                f"{c['depth']} · attempt {c['attempt']} · phase {esc(c['phase'])} · created by {esc(c['created_by'])}"
                f"{' · recipe ' + esc(c['recipe_id']) + '/' + esc(c['recipe_step']) if c['recipe_id'] else ''}"
                f"{' · model profile ' + esc(c['model_profile']) if c['model_profile'] else ''}</p>")
        body += kv({"goal": c["goal"], "original goal": c["original_goal"], "done_when": c["done_when"],
                    "constraints": c["constraints"] or None, "topic": c["origin_topic_id"],
                    "parents": " ← ".join(f"{x['id']} {x['title']}" for x in chain) or None, "due": c["due_at"]})
        if c["result"]:
            arts = self.store(c["root_id"])
            body += "<h2>Result</h2>" + pre(c["result"]) + "<details><summary>as parents / the front desk see it</summary>" \
                + pre(worker.result_text(c["result"], arts)) + "</details>"
        body += "<h2>Sessions</h2>" + self.session_table(db.q("SELECT * FROM sessions WHERE card_id=? ORDER BY started_at", cid))
        body += "<h2>Events</h2>" + self.event_table(db.q("SELECT * FROM card_events WHERE card_id=? ORDER BY id", cid), card=False)
        body += "<h2>Children</h2>" + table(board.children(db, cid), self.card_cols())
        deps = db.q("SELECT d.depends_on, c.title, c.state FROM card_deps d JOIN cards c ON c.id=d.depends_on WHERE d.card_id=?", cid)
        rdeps = db.q("SELECT d.card_id, c.title, c.state FROM card_deps d JOIN cards c ON c.id=d.card_id WHERE d.depends_on=?", cid)
        body += "<h2>Dependencies</h2><p>depends on: " + (", ".join(f"{t(d['depends_on'])} {badge(d['state'])} {esc(d['title'])}" for d in deps) or "nothing") \
            + "<br>needed by: " + (", ".join(f"{t(d['card_id'])} {badge(d['state'])} {esc(d['title'])}" for d in rdeps) or "nothing") + "</p>"
        body += "<h2>Comments</h2>" + table(db.q("SELECT * FROM comments WHERE card_id=? ORDER BY created_at", cid), [
            ("at", lambda r: esc(r["created_at"])), ("author", lambda r: esc(r["author"])), ("text", lambda r: t(r["body"]))])
        body += '<h2 id="questions">Questions</h2>' + self.question_table(db.q("SELECT * FROM questions WHERE card_id=?", cid))
        crit = [verifier.classify(x) for x in c["done_when"] or []]
        body += "<h2>done_when as the verifier sees it</h2>" + table(crit, [
            ("type", lambda r: esc(r["type"])), ("criterion", lambda r: t(r.get("text"))),
            ("details", lambda r: esc({k: v for k, v in r.items() if k not in ("type", "text")} or ""))])
        body += '<h2 id="inputs">Inputs as the next session sees them</h2>'
        try:
            body += pre("\n\n".join(dispatcher.Dispatcher(db, None, None, self.workspace).inputs(c)) or "(none)", open_=False,
                        label="inputs")
        except Exception as e:
            body += f"<p class=muted>{esc(e)}</p>"
        body += "<h2>LLM calls</h2>" + self.call_table(db.q("SELECT * FROM llm_calls WHERE card_id=? ORDER BY at, rowid", cid))
        body += "<h2>Outbox items from this card</h2>" + self.outbox_table(db.q("SELECT * FROM outbox WHERE card_id=?", cid))
        body += "<h2>Facts proposed by this card</h2>" + self.fact_table(db.q("SELECT * FROM facts WHERE card_id=?", cid))
        arts = [(a, m) for a, m in self.store(c["root_id"]).index.items() if m.get("card_id") == cid]
        body += "<h2>Artifacts</h2>" + table(arts, [("artifact", lambda r: t(r[0])), ("name", lambda r: esc(r[1]["name"])),
                                                   ("chars", lambda r: esc(r[1]["chars"])),
                                                   ("start", lambda r: esc(short(r[1]["summary"])))])
        if c["role"] == "code":
            wd = os.path.join(self.workspace, c["root_id"], cid)
            if os.path.isdir(wd):
                body += "<h2>Work folder</h2>" + "".join(pre(open(os.path.join(wd, f), errors="replace").read(), open_=False, label=f)
                                                        for f in sorted(os.listdir(wd)) if os.path.isfile(os.path.join(wd, f)))
        body += "<h2>Full card row</h2>" + f"<details><summary>all columns</summary>{kv(c)}</details>"
        return 200, self.page(f"Card {cid}", body, params.get("refresh"))

    def store(self, root):
        from .tools import ArtifactStore
        path = os.path.join(self.workspace, root or "none")
        if not os.path.isdir(path):
            class Empty:
                index = {}
            return Empty()
        return ArtifactStore(path)

    # ----- conversation -----
    def topics(self, params):
        db = self.db
        rows = db.q("""SELECT t.*, (SELECT COUNT(*) FROM messages m WHERE m.topic_id=t.id) msgs,
                       (SELECT COUNT(*) FROM cards c WHERE c.origin_topic_id=t.id AND c.depth=0) cards
                       FROM topics t ORDER BY last_activity_at DESC""")
        body = table(rows, [("topic", lambda r: t(r["id"])), ("slug", lambda r: f'<a href="/topic/{r["id"]}">{esc(r["slug"])}</a>'),
                            ("title", lambda r: esc(r["title"])), ("status", lambda r: badge(r["status"])),
                            ("messages", lambda r: esc(r["msgs"])), ("root cards", lambda r: esc(r["cards"])),
                            ("last activity", lambda r: esc(r["last_activity_at"])),
                            ("summary", lambda r: t(short(r["summary"], 200)))])
        return 200, self.page("Topics", body, params.get("refresh"))

    def topic(self, params, tid):
        db = self.db
        tp = db.get("topics", tid)
        if not tp:
            return 404, self.page("Not found", "")
        body = kv(tp)
        msgs = db.q("SELECT * FROM messages WHERE topic_id=? ORDER BY created_at", tid)
        body += "<h2>Messages</h2>" + ("".join(
            f'<div class="msg {m["direction"]}" id="{m["id"]}"><span class="muted">{esc(m["created_at"])} · '
            f'{"owner" if m["direction"] == "in" else "sandman"} · {t(m["id"])}'
            + (f' · routed by <b>{esc(m["routed_by"])}</b> ({esc(m["route_confidence"])})' if m["direction"] == "in" else "")
            + (' · <span class="bad">provisional</span>' if m["provisional"] else "")
            + f'</span><div>{t(m["text"])}</div></div>' for m in msgs) or '<p class="muted">no messages</p>')
        body += "<h2>Cards</h2>" + table(db.q("SELECT * FROM cards WHERE origin_topic_id=? ORDER BY created_at", tid), self.card_cols())
        body += "<h2>Questions</h2>" + self.question_table(db.q("SELECT * FROM questions WHERE topic_id=?", tid))
        body += "<h2>Outbox</h2>" + self.outbox_table(db.q("SELECT * FROM outbox WHERE topic_id=? ORDER BY created_at", tid))
        body += "<h2>Sessions</h2>" + self.session_table(db.q("SELECT * FROM sessions WHERE topic_id=? ORDER BY started_at", tid))
        return 200, self.page(f"Topic {tp['slug']}: {tp['title']}", body, params.get("refresh"))

    def outbox_table(self, rows):
        return table(rows, [("item", lambda r: f'<span id="{r["id"]}">{t(r["id"])}</span>'), ("status", lambda r: badge(r["status"])),
                            ("kind", lambda r: esc(r["kind"])), ("priority", lambda r: esc(r["priority"])),
                            ("topic", lambda r: t(r["topic_id"])), ("card", lambda r: t(r["card_id"])),
                            ("question", lambda r: t(r["question_id"])), ("dedupe", lambda r: esc(r["dedupe_key"])),
                            ("created / sent", lambda r: esc(f"{r['created_at']} / {r['sent_at'] or '—'}")),
                            ("body", lambda r: pre(r["body"], limit=300))])

    def outbox(self, params):
        return 200, self.page("Outbox", self.outbox_table(self.db.q("SELECT * FROM outbox ORDER BY created_at DESC LIMIT 500")),
                              params.get("refresh"))

    def question_table(self, rows):
        return table(rows, [("question", lambda r: t(r["id"])), ("handle", lambda r: esc(r["handle"])),
                            ("status", lambda r: badge(r["status"])), ("card", lambda r: t(r["card_id"])),
                            ("topic", lambda r: t(r["topic_id"])), ("text", lambda r: t(r["text"])),
                            ("options", lambda r: esc(", ".join(r["options"] or []))), ("answer", lambda r: t(r["answer"])),
                            ("asked", lambda r: esc(r["created_at"]))])

    def questions(self, params):
        return 200, self.page("Questions", self.question_table(self.db.q("SELECT * FROM questions ORDER BY created_at DESC")),
                              params.get("refresh"))

    # ----- sessions & calls -----
    def session_table(self, rows):
        return table(rows, [("session", lambda r: t(r["id"])), ("type", lambda r: esc(r["type"])),
                            ("card", lambda r: t(r["card_id"])), ("topic", lambda r: t(r["topic_id"])),
                            ("model", lambda r: esc(r["model"])), ("turns", lambda r: esc(r["turns"])),
                            ("started", lambda r: esc(r["started_at"])), ("ended", lambda r: esc(r["ended_at"] or "running")),
                            ("outcome", lambda r: t(short(r["outcome"], 300)))])

    def sessions(self, params):
        db = self.db
        typ = params.get("type")
        types = [r["type"] for r in db.q("SELECT DISTINCT type FROM sessions")]
        filt = '<p class="filters">type: <a href="/sessions">all</a> ' + " ".join(
            f'<a href="/sessions?type={x}">{esc(x)}</a>' for x in types) + "</p>"
        rows = db.q("SELECT * FROM sessions WHERE type=? ORDER BY started_at DESC LIMIT 500", typ) if typ else \
            db.q("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 500")
        return 200, self.page("Sessions", filt + self.session_table(rows), params.get("refresh"))

    def session(self, params, sid):
        db = self.db
        s = db.get("sessions", sid)
        if not s:
            return 404, self.page("Not found", "")
        body = kv(s)
        llm = db.q("SELECT * FROM llm_calls WHERE session_id=? ORDER BY rowid", sid)
        tools = db.q("SELECT * FROM tool_calls WHERE session_id=? ORDER BY rowid", sid)
        by_call = {}
        for tc in tools:
            by_call.setdefault(tc.get("after_call"), []).append(tc)
        steps = [("tool", None, tc) for tc in by_call.pop(None, [])]  # rows from before after_call existed
        for r in llm:
            steps.append(("call", None, r))
            steps += [("tool", None, tc) for tc in by_call.pop(r["id"], [])]
        body += "<h2>Timeline</h2>"
        for kind, _, r in steps:
            if kind == "call":
                p = r["parsed"]
                body += (f'<div class="step">🧠 {t(r["id"])} <b>{esc(r["call_type"])}</b> '
                         f'<span class="{"ok" if r["ok"] else "bad"}">{"ok" if r["ok"] else "invalid"}</span> '
                         f'<span class="muted">attempt {esc(r["attempt"])} · {esc(r["provider"])} · {esc(r["ms"])} ms · '
                         f'{esc(r["tokens_in"])}→{esc(r["tokens_out"])} tok · {money(r["cost"])}</span>'
                         + (pre(r["error"]) if r["error"] else pre(p, open_=True, label="output", limit=1))
                         + f'<details><summary>prompt</summary>{pre((r["input"] or {}).get("user"))}</details></div>')
            else:
                body += (f'<div class="step" id="{r["id"]}">🔧 turn {esc(r["turn"])} <b>{esc(r["tool"])}</b> '
                         f'<span class="{"ok" if r["ok"] else "bad"}">{"ok" if r["ok"] else "error"}</span> '
                         f'<span class="muted">{esc(r["ms"])} ms</span>{pre(r["args"], open_=False, label="arguments")}'
                         f'{pre(r["result"], open_=False, label="result")}</div>')
        if not steps:
            body += '<p class="muted">no calls recorded</p>'
        return 200, self.page(f"Session {sid} ({s['type']})", body, params.get("refresh"))

    def call_table(self, rows):
        return table(rows, [("call", lambda r: t(r["id"])), ("at", lambda r: esc(r["at"])),
                            ("type", lambda r: esc(r["call_type"])), ("ok", lambda r: '<span class="ok">✓</span>' if r["ok"]
                                                                        else f'<span class="bad" title="{esc(r["error"])}">✗</span>'),
                            ("attempt", lambda r: esc(r.get("attempt"))), ("model / provider", lambda r: esc(f"{r['model']} / {r.get('provider') or '?'}")),
                            ("ms", lambda r: esc(r["ms"])), ("tokens", lambda r: esc(f"{r['tokens_in']}→{r['tokens_out']}")),
                            ("cost", lambda r: money(r.get("cost"))), ("session", lambda r: t(r.get("session_id"))),
                            ("card", lambda r: t(r.get("card_id"))), ("output", lambda r: t(short(json.dumps(r["parsed"], ensure_ascii=False) if r["parsed"] is not None else r["error"], 140)))])

    def calls(self, params):
        db = self.db
        where, args = [], []
        for k in ("call_type", "session_id", "card_id", "topic_id", "provider"):
            if params.get(k):
                where.append(f"{k}=?"), args.append(params[k])
        if params.get("ok") in ("0", "1"):
            where.append("ok=?"), args.append(int(params["ok"]))
        sql = "SELECT * FROM llm_calls" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY at DESC, rowid DESC LIMIT 500"
        types = [r["call_type"] for r in db.q("SELECT DISTINCT call_type FROM llm_calls ORDER BY call_type")]
        filt = ('<p class="filters">type: ' + f'<a href="{qs_link("/calls", params, call_type=None)}">all</a> '
                + " ".join(f'<a href="{qs_link("/calls", params, call_type=x)}">{esc(x)}</a>' for x in types)
                + f' · <a href="{qs_link("/calls", params, ok="0")}">only invalid</a></p>')
        return 200, self.page("LLM calls", filt + self.call_table(db.q(sql, *args)), params.get("refresh"))

    def call(self, params, cid):
        r = self.db.get("llm_calls", cid)
        if not r:
            return 404, self.page("Not found", "")
        inp = r["input"] or {}
        body = kv(r, ["call_type", "ok", "error", "attempt", "model", "provider", "temperature", "max_tokens", "ms",
                      "tokens_in", "tokens_out", "cost", "at", "session_id", "card_id", "topic_id"])
        body += "<h2>System prompt</h2>" + pre(inp.get("system"))
        if inp.get("system_hint"):
            body += "<p class=muted>+ gateway hint appended:</p>" + pre(inp["system_hint"])
        body += "<h2>User prompt</h2>" + pre(inp.get("user"))
        body += "<h2>Output schema (constrained decoding)</h2>" + pre(r.get("schema") or "(plain text call)", open_=False, label="schema")
        body += "<h2>Raw output</h2>" + pre(r["raw"])
        body += "<h2>Parsed output</h2>" + pre(r["parsed"])
        return 200, self.page(f"LLM call {cid}", body)

    def stats(self, params):
        db = self.db
        rows = db.q("SELECT * FROM llm_calls")

        def agg(key):
            g = {}
            for r in rows:
                g.setdefault(r.get(key) or "?", []).append(r)
            out = []
            for k, rs in sorted(g.items()):
                ms = [r["ms"] for r in rs if r["ms"]]
                out.append({"key": k, "n": len(rs), "invalid": sum(1 for r in rs if not r["ok"]),
                            "median_ms": int(statistics.median(ms)) if ms else 0,
                            "p90_ms": int(sorted(ms)[int(len(ms) * 0.9) - 1]) if ms else 0,
                            "tin": sum(r["tokens_in"] or 0 for r in rs), "tout": sum(r["tokens_out"] or 0 for r in rs),
                            "cost": sum(r.get("cost") or 0 for r in rs)})
            return out
        cols = lambda name: [(name, lambda r: esc(r["key"])), ("calls", lambda r: esc(r["n"])),
                             ("invalid", lambda r: esc(f"{r['invalid']} ({r['invalid'] / r['n']:.0%})")),
                             ("median ms", lambda r: esc(r["median_ms"])), ("p90 ms", lambda r: esc(r["p90_ms"])),
                             ("tokens in", lambda r: esc(r["tin"])), ("tokens out", lambda r: esc(r["tout"])),
                             ("cost", lambda r: money(r["cost"]))]
        body = "<h2>By call type</h2>" + table(agg("call_type"), cols("call type"))
        body += "<h2>By model</h2>" + table(agg("model"), cols("model"))
        body += "<h2>By provider</h2>" + table(agg("provider"), cols("provider"))
        sess = db.q("""SELECT s.type, COUNT(*) n, AVG(s.turns) turns,
                       (SELECT COUNT(*) FROM llm_calls c JOIN sessions s2 ON c.session_id=s2.id WHERE s2.type=s.type) calls,
                       (SELECT SUM(cost) FROM llm_calls c JOIN sessions s2 ON c.session_id=s2.id WHERE s2.type=s.type) cost
                       FROM sessions s GROUP BY s.type""")
        body += "<h2>By session type</h2>" + table(sess, [("session type", lambda r: esc(r["type"])), ("sessions", lambda r: esc(r["n"])),
                                                          ("avg turns", lambda r: esc(f"{r['turns'] or 0:.1f}")),
                                                          ("LLM calls", lambda r: esc(r["calls"])), ("cost", lambda r: money(r["cost"]))])
        tools = db.q("SELECT tool, COUNT(*) n, SUM(1-ok) errors, AVG(ms) ms FROM tool_calls GROUP BY tool")
        body += "<h2>Tool calls</h2>" + table(tools, [("tool", lambda r: esc(r["tool"])), ("calls", lambda r: esc(r["n"])),
                                                     ("errors", lambda r: esc(r["errors"])), ("avg ms", lambda r: esc(int(r["ms"] or 0)))])
        return 200, self.page("Stats", body, params.get("refresh"))

    # ----- memory -----
    def memory(self, params):
        db = self.db
        notes = db.q("""SELECT n.*, (SELECT COUNT(*) FROM claims c WHERE c.note_id=n.id AND c.status='active') active,
                        (SELECT COUNT(*) FROM claims c WHERE c.note_id=n.id) total FROM notes n ORDER BY n.updated_at DESC""")
        body = "<p>Owner profile (injected into every front-desk and worker session):</p>" + pre(memory.profile_text(db) or "(empty)")
        body += "<h2>Notes</h2>" + table(notes, [
            ("note", lambda r: t(r["id"])), ("kind", lambda r: esc(r["kind"])), ("title", lambda r: f'<a href="/note/{r["id"]}">{esc(r["title"])}</a>'),
            ("aliases", lambda r: esc(", ".join(r["aliases"] or []))), ("one-liner", lambda r: t(r["one_liner"])),
            ("claims active/total", lambda r: esc(f"{r['active']}/{r['total']}")), ("status", lambda r: badge(r["status"])),
            ("dirty", lambda r: "yes" if r["dirty"] else ""), ("updated", lambda r: esc(r["updated_at"]))])
        body += '<h2 id="reviews">Review items (conflicts for the owner)</h2>' + table(db.q("SELECT * FROM review_items"), [
            ("item", lambda r: t(r["id"])), ("kind", lambda r: esc(r["kind"])), ("status", lambda r: badge(r["status"])),
            ("payload", lambda r: t(json.dumps(r["payload"])))])
        return 200, self.page("Memory", body, params.get("refresh"))

    def note(self, params, nid):
        db = self.db
        n = db.get("notes", nid)
        if not n:
            return 404, self.page("Not found", "")
        body = kv(n)
        claims = db.q("SELECT * FROM claims WHERE note_id=? ORDER BY observed_at DESC", nid)
        body += "<h2>Claims (all statuses)</h2>" + table(claims, [
            ("claim", lambda r: f'<span id="{r["id"]}">{t(r["id"])}</span>'), ("status", lambda r: badge(r["status"])),
            ("text", lambda r: t(r["text"])), ("source", lambda r: t(json.dumps(r["source"], ensure_ascii=False))),
            ("observed", lambda r: esc(r["observed_at"])), ("volatility", lambda r: esc(r["volatility"])),
            ("stale", lambda r: '<span class="bad">stale</span>' if memory.is_stale(r) else ""),
            ("corroborations", lambda r: esc(r["corroborations"])), ("superseded by", lambda r: t(r["superseded_by"]))])
        body += "<h2>As sessions see it</h2>" + pre(memory.note_text(db, nid))
        facts = db.q("SELECT * FROM facts WHERE decision_reason LIKE ? ORDER BY created_at", f"%{nid}%")
        body += "<h2>Facts consolidated into this note</h2>" + self.fact_table(facts)
        return 200, self.page(f"Note {n['title']}", body)

    def fact_table(self, rows):
        return table(rows, [("fact", lambda r: f'<span id="{r["id"]}">{t(r["id"])}</span>'), ("status", lambda r: badge(r["status"])),
                            ("subject", lambda r: esc(r["subject"])), ("text", lambda r: t(r["text"])),
                            ("source", lambda r: t(json.dumps(r["source"], ensure_ascii=False))),
                            ("volatility", lambda r: esc(r["volatility"])), ("decision", lambda r: esc(r["decision"])),
                            ("reason", lambda r: t(r["decision_reason"])), ("from card", lambda r: t(r["card_id"])),
                            ("created", lambda r: esc(r["created_at"]))])

    def facts(self, params):
        db = self.db
        st = params.get("status")
        rows = db.q("SELECT * FROM facts WHERE status=? ORDER BY created_at DESC", st) if st else \
            db.q("SELECT * FROM facts ORDER BY created_at DESC LIMIT 1000")
        filt = '<p class="filters">' + " ".join(f'<a href="/facts?status={s}">{badge(s)}</a>'
                                                for s in ("pending", "merged", "discarded", "review")) + ' <a href="/facts">all</a></p>'
        return 200, self.page("Fact queue & consolidator decisions", filt + self.fact_table(rows), params.get("refresh"))

    # ----- artifacts -----
    def all_artifacts(self):
        out = []
        if os.path.isdir(self.workspace):
            for root in sorted(os.listdir(self.workspace)):
                st = self.store(root)
                for a, m in st.index.items():
                    out.append((root, a, m))
        return out

    def artifacts(self, params):
        rows = self.all_artifacts()
        return 200, self.page("Artifacts", table(rows, [
            ("artifact", lambda r: t(r[1])), ("card tree", lambda r: t(r[0])), ("card", lambda r: t(r[2].get("card_id"))),
            ("name", lambda r: esc(r[2]["name"])), ("chars", lambda r: esc(r[2]["chars"])),
            ("start", lambda r: esc(short(r[2]["summary"])))]))

    def artifact(self, params, aid):
        for root, a, m in self.all_artifacts():
            if a == aid:
                content = open(m["path"], errors="replace").read()
                return 200, self.page(f"Artifact {aid}", kv({**m, "card tree": root}) + "<h2>Content</h2>" + pre(content))
        return 404, self.page("Not found", "")

    # ----- events -----
    def event_table(self, rows, card=True):
        cols = [("#", lambda r: esc(r["id"])), ("at", lambda r: esc(r["at"]))]
        if card:
            cols.append(("card", lambda r: t(r["card_id"])))
        cols += [("from", lambda r: badge(r["from_state"]) if r["from_state"] else ""), ("event", lambda r: esc(r["event"])),
                 ("to", lambda r: badge(r["to_state"])), ("payload", lambda r: t(json.dumps(r["payload"], ensure_ascii=False) if r["payload"] else ""))]
        return table(rows, cols)

    def events(self, params):
        return 200, self.page("Card events", self.event_table(self.db.q("SELECT * FROM card_events ORDER BY id DESC LIMIT 1000")),
                              params.get("refresh"))

    # ----- static: prompts, roles, config -----
    def prompts(self, params):
        body = ("<p>All prompts, from <code>sandman/prompts.md</code> (system prompt above <code>---</code>, "
                "user prompt below; <code>{placeholders}</code> are filled by the harness). Output schemas are "
                "built in <code>sandman/calls.py</code>; the ones below are examples with made-up ids.</p>")
        body += "<h2>Worker roles</h2>"
        for role, tools in worker.ROLE_TOOLS.items():
            schema = calls.worker_schema(role, tools, [], ["art_example"], calls.TERMINALS)
            body += (f"<h2>role: {esc(role)} <span class=muted>· max {worker.MAX_TURNS[role]} turns · tools: "
                     f"{esc(', '.join(tools))}</span></h2>" + pre(calls.prompts()["role_" + role])
                     + "<ul>" + "".join(f"<li><code>{esc(calls.TOOL_DOCS[x])}</code></li>" for x in tools) + "</ul>"
                     + pre(schema, open_=False, label="worker_step output schema for this role"))
        body += "<h2>Front desk tools</h2><ul>" + "".join(f"<li><code>{esc(v)}</code></li>" for v in calls.FRONTDESK_TOOLS.values()) + "</ul>"
        body += pre(calls.frontdesk_schema(["crd_example"], ["qst_example"], False), open_=False, label="frontdesk_step output schema")
        body += "<h2>All prompt sections</h2>"
        for name, text in calls.prompts().items():
            body += f'<details><summary><b>{esc(name)}</b></summary>{pre(text)}</details>'
        return 200, self.page("Prompts & roles", body)

    def config(self, params):
        conf = {
            "board": {k: getattr(board, k) for k in ("MAX_DEPTH", "MAX_CHILDREN", "MAX_ATTEMPTS", "MAX_CONTINUATIONS",
                                                     "MAX_OPEN_PER_ROOT", "LEASE_SECONDS")},
            "state machine (state, event) → state": {f"{a} --{b}-->": c for (a, b), c in board.TRANSITIONS.items()},
            "worker": {"MAX_TURNS": worker.MAX_TURNS, "ROLE_TOOLS": worker.ROLE_TOOLS,
                       "LAST_RESULT_CHARS": worker.LAST_RESULT_CHARS, "OLD_RESULT_CHARS": worker.OLD_RESULT_CHARS},
            "dispatcher": {"LIBRARIAN_ROLES": sorted(dispatcher.LIBRARIAN_ROLES), "INPUT_CHARS": dispatcher.INPUT_CHARS},
            "conversation": {k: getattr(conversation, k) for k in ("STICKY_MINUTES", "SHORTLIST", "FRONTDESK_STEPS",
                                                                   "HISTORY", "SUMMARY_REBUILD_EVERY", "HINT_TIMES")},
            "memory": {"MAX_AGE_DAYS": memory.MAX_AGE_DAYS, "SOURCE_RANK": memory.SOURCE_RANK},
            "relevance rubric keys (keep rule in calls.keep_fact)": calls.RUBRIC_KEYS,
            "environment": {k: (v if "KEY" not in k else "(set)" if v else "(unset)") for k, v in os.environ.items()
                            if k.startswith("SANDMAN_") or k == "OPENROUTER_API_KEY"},
            "home": self.home,
        }
        body = "".join(f"<h2>{esc(k)}</h2>{pre(v)}" for k, v in conf.items())
        body += "<h2>Recipes</h2>" + "".join(f"<details><summary><b>{esc(r['id'])}</b> {esc(r['title'])}</summary>{pre(r)}</details>"
                                             for r in recipes.RECIPES.values())
        return 200, self.page("Config", body)


class Handler(BaseHTTPRequestHandler):
    app = None
    lock = threading.Lock()  # one SQLite connection, one request at a time

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            with self.lock:
                status, body = self.app.route(u.path.rstrip("/") or "/", params)
        except Exception:  # show errors instead of a blank page
            status, body = 500, self.app.page("Error", pre(traceback.format_exc()))
        if status == 302:
            self.send_response(302)
            self.send_header("Location", body)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def serve(home, host="127.0.0.1", port=8080):
    Handler.app = App(home)
    print(f"Sandman web UI on http://{host}:{port} (home {home})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
