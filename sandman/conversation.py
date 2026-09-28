"""Conversation layer (design §6): router cascade, front desk, outbox, courier,
questions and commands. Topics are subjects; bindings are only routes."""
import datetime
import re
import time

from . import board, calls, memory, trace
from .db import new_id, now
from .llm import LLMFailure

STICKY_MINUTES = 20
SHORTLIST = 5
FRONTDESK_STEPS = 4
HISTORY = 8
SUMMARY_REBUILD_EVERY = 20
HINT_TIMES = 3


# ---------- outbox & questions ----------

def post(db, topic_id, kind, body, priority="normal", card_id=None, question_id=None, dedupe_key=None):
    if dedupe_key and db.one("SELECT 1 FROM outbox WHERE dedupe_key=?", dedupe_key):
        return None
    oid = new_id("out")
    db.insert("outbox", id=oid, topic_id=topic_id, kind=kind, priority=priority, body=body, card_id=card_id,
              question_id=question_id, dedupe_key=dedupe_key, status="pending", created_at=now())
    return oid


def topic_of_card(db, card):
    while card and not card["origin_topic_id"] and card["parent_id"]:
        card = db.get("cards", card["parent_id"])
    return card["origin_topic_id"] if card else None


def ask(db, card, question, options=()):
    n = db.one("SELECT COUNT(*) n FROM questions")["n"] + 1
    qid, handle = new_id("qst"), f"Q{n}"
    tid = topic_of_card(db, card)
    db.insert("questions", id=qid, handle=handle, card_id=card["id"], topic_id=tid, text=question,
              options=list(options), status="open", created_at=now())
    opts = f"\nOptions: {' / '.join(options)}" if options else ""
    post(db, tid, "question", f"Card \"{card['title']}\" needs input ({handle}): {question}{opts}\n"
                              f"(answer with \"{handle}: ...\")", card_id=card["id"], question_id=qid)
    return handle


def answer(db, qid, text):
    q = db.get("questions", qid)
    if not q or q["status"] != "open":
        return "Error: question is not open."
    db.update("questions", qid, status="answered", answer=text)
    card = db.get("cards", q["card_id"])
    board.comment(db, card["id"], "owner", f"Q: {q['text']} A: {text}")
    if card["state"] == "blocked":
        if text.strip().lower().startswith("cancel"):
            board.cancel_tree(db, card["id"])
            return f"Card {card['id']} cancelled."
        board.transition(db, card["id"], "answered", attempt=1)
    return f"Answer passed to card {card['id']}."


class Courier:
    """Deterministic delivery (§6.7). The prototype has one binding per adapter
    and no quiet hours/digests; labels every message with its topic slug."""

    def __init__(self, db, send):
        self.db, self.send = db, send

    def flush(self):
        for o in self.db.q("SELECT * FROM outbox WHERE status='pending' ORDER BY created_at"):
            t = self.db.get("topics", o["topic_id"]) if o["topic_id"] else None
            label = f"[{t['slug']}] " if t else ""
            self.send(label + o["body"], o)
            self.db.update("outbox", o["id"], status="sent", sent_at=now())
            self.db.insert("messages", id=new_id("msg"), topic_id=o["topic_id"], binding="cli", direction="out",
                           text=o["body"], created_at=now())
            if t:
                self.db.update("topics", t["id"], last_activity_at=now())


# ---------- time parsing (the harness parses `when`, not the model) ----------

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_when(text, ref=None):
    ref = ref or datetime.datetime.now()
    try:
        import dateparser
        d = dateparser.parse(text, settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": ref})
        if d:
            return d if (d.hour or d.minute) else d.replace(hour=9)
    except ImportError:
        pass
    t = text.lower()
    m = re.search(r"in (\d+) (minute|hour|day|week)s?", t)
    if m:
        unit = {"minute": "minutes", "hour": "hours", "day": "days", "week": "weeks"}[m.group(2)]
        return ref + datetime.timedelta(**{unit: int(m.group(1))})
    day = None
    if "tomorrow" in t:
        day = ref.date() + datetime.timedelta(days=1)
    elif "today" in t or "tonight" in t:
        day = ref.date()
    for i, w in enumerate(WEEKDAYS):
        if w in t or re.search(rf"\b{w[:3]}\b", t):
            delta = (i - ref.weekday()) % 7 or 7
            day = ref.date() + datetime.timedelta(days=delta)
    hour, minute = 9, 0
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", t)
    if m:
        if m.group(3):
            hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
            minute = int(m.group(2) or 0)
        else:
            hour, minute = int(m.group(4)), int(m.group(5))
        day = day or ref.date()
    if day is None:
        return None
    return datetime.datetime.combine(day, datetime.time(hour, minute))


# ---------- conversation ----------

def slugify(title, db):
    base = "-".join(re.findall(r"[a-z0-9]+", title.lower())[:2]) or "topic"
    slug, i = base, 2
    while db.one("SELECT 1 FROM topics WHERE slug=?", slug):
        slug, i = f"{base}{i}", i + 1
    return slug


class Conversation:
    def __init__(self, db, gw, owner="the owner", binding="cli", log=print):
        self.db, self.gw, self.owner, self.binding, self.log = db, gw, owner, binding, log
        self.hints_shown = 0

    # ----- topics -----
    def new_topic(self, title):
        tid = new_id("top")
        self.db.insert("topics", id=tid, slug=slugify(title, self.db), title=title, created_at=now(),
                       last_activity_at=now())
        return self.db.get("topics", tid)

    def topic_lines(self, tid, n=2):
        return [f"{'owner' if m['direction'] == 'in' else 'assistant'}: {m['text'][:200]}"
                for m in reversed(self.db.q("SELECT * FROM messages WHERE topic_id=? ORDER BY created_at DESC "
                                            "LIMIT ?", tid, n))]

    # ----- router cascade (§6.4) -----
    def route(self, text, selected_slug=None):
        """Returns (topic, text, routed_by, confidence, provisional)."""
        db = self.db
        m = re.match(r"^(?:/t\s+|#)([\w-]+)\s*(.*)$", text, re.S)
        if m and db.one("SELECT 1 FROM topics WHERE slug=?", m.group(1)):
            return db.one("SELECT * FROM topics WHERE slug=?", m.group(1)), m.group(2) or text, "command", "high", False
        m = re.match(r"^/new\s+(.+?)(?:\n(.*))?$", text, re.S)
        if m:
            return self.new_topic(m.group(1).strip()), m.group(2) or m.group(1), "command", "high", False
        if selected_slug:
            t = db.one("SELECT * FROM topics WHERE slug=?", selected_slug)
            if t:
                return t, text, "selector", "high", False
        # stage 5: sticky
        last = db.one("SELECT * FROM messages WHERE binding=? ORDER BY created_at DESC LIMIT 1", self.binding)
        sticky = None
        if last and last["topic_id"]:
            age = (datetime.datetime.now() - datetime.datetime.fromisoformat(last["created_at"])).total_seconds()
            if age < STICKY_MINUTES * 60:
                sticky = db.get("topics", last["topic_id"])
        same = None
        if sticky:
            try:
                same = calls.route_sticky(self.gw, sticky["title"], self.topic_lines(sticky["id"]), text)["same"]
            except LLMFailure:
                same = "unsure"
            if same == "yes":
                return sticky, text, "sticky", "high", False
        # stage 6: shortlist
        cands = self.candidates(text)
        if not cands:
            return self.new_topic(self._title(text)), text, "new", "high", False
        try:
            r = calls.route_shortlist(self.gw, [{"slug": t["slug"], "title": t["title"],
                                                 "last": (self.topic_lines(t["id"], 1) or [""])[0][:120]}
                                                for t in cands], text)
        except LLMFailure:
            r = {"choice": sticky["slug"] if sticky else "new", "confidence": "low"}
        # stage 7: low confidence → best guess, provisional
        provisional = r["confidence"] == "low" or (same == "unsure" and sticky and r["choice"] != sticky["slug"])
        if r["choice"] == "new":
            return self.new_topic(self._title(text)), text, "shortlist", r["confidence"], provisional
        return db.one("SELECT * FROM topics WHERE slug=?", r["choice"]), text, "shortlist", r["confidence"], provisional

    def _title(self, text):
        try:
            return calls.topic_title(self.gw, text)["title"]
        except LLMFailure:
            return " ".join(text.split()[:5])

    def candidates(self, text):
        """≤5 topics ranked by recency and word overlap (vectors omitted)."""
        words = set(re.findall(r"\w{4,}", text.lower()))
        scored = []
        for i, t in enumerate(self.db.q("SELECT * FROM topics WHERE status!='archived' "
                                        "ORDER BY last_activity_at DESC LIMIT 30")):
            body = f"{t['title']} {t['summary']} " + " ".join(self.topic_lines(t["id"], 4))
            overlap = len(words & set(re.findall(r"\w{4,}", body.lower())))
            scored.append((overlap * 2 - i * 0.3 - (3 if t["status"] == "dormant" else 0), t))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:SHORTLIST]]

    # ----- inbound -----
    def handle(self, text, selected_slug=None):
        text = text.strip()
        if not text:
            return
        if self.command(text):
            return
        if self.question_answer(text):
            return
        if re.match(r"^(→|->)\s*[\w-]+$", text):
            return self.correct(re.sub(r"^(→|->)\s*", "", text))
        with trace.session(self.db, "router", model=self.gw.model, text=text[:500]) as s:
            topic, body, routed_by, conf, prov = self.route(text, selected_slug)
            s["outcome"] = f"[{topic['slug']}] by {routed_by}, confidence {conf}{', provisional' if prov else ''}"
        self.db.update("sessions", s["id"], topic_id=topic["id"])
        mid = new_id("msg")
        self.db.insert("messages", id=mid, topic_id=topic["id"], binding=self.binding, direction="in", text=body,
                       routed_by=routed_by, route_confidence=conf, provisional=int(bool(prov)), created_at=now())
        self.db.update("topics", topic["id"], last_activity_at=now(), status="active")
        self.log(f"  (routed to [{topic['slug']}] by {routed_by}, {conf}{', provisional' if prov else ''})")
        self.frontdesk(topic, body, prov)
        self.after_turn(topic, body)

    def question_answer(self, text):
        m = re.match(r"^(Q\d+)\s*[:\-]\s*(.+)$", text, re.S | re.I)
        if m:
            q = self.db.one("SELECT * FROM questions WHERE handle=? AND status='open'", m.group(1).upper())
            if q:
                post(self.db, q["topic_id"], "reply", answer(self.db, q["id"], m.group(2).strip()))
                return True
        open_q = self.db.q("SELECT * FROM questions WHERE status='open'")
        if len(open_q) == 1 and text.lower() in [o.lower() for o in open_q[0]["options"] or []]:
            post(self.db, open_q[0]["topic_id"], "reply",
                 f"Taking \"{text}\" as the answer to {open_q[0]['handle']}. " + answer(self.db, open_q[0]["id"], text))
            return True
        return False

    def correct(self, slug):
        """`→ slug` re-routes the last exchange and re-parents its cards (§6.4)."""
        db = self.db
        last_in = db.one("SELECT * FROM messages WHERE binding=? AND direction='in' ORDER BY created_at DESC LIMIT 1",
                         self.binding)
        if not last_in:
            return
        target = self.new_topic(self._title(last_in["text"])) if slug == "new" else \
            db.one("SELECT * FROM topics WHERE slug=?", slug)
        if not target:
            post(db, None, "reply", f"No topic '{slug}'. Try /topics.")
            return
        old = last_in["topic_id"]
        db.x("UPDATE messages SET topic_id=?, provisional=0, routed_by='correction' WHERE topic_id=? AND created_at>=?",
             target["id"], old, last_in["created_at"])
        db.x("UPDATE cards SET origin_topic_id=? WHERE origin_topic_id=? AND created_at>=?",
             target["id"], old, last_in["created_at"])
        db.x("UPDATE outbox SET topic_id=? WHERE topic_id=? AND created_at>=?", target["id"], old, last_in["created_at"])
        post(db, target["id"], "reply", "Moved here.")

    # ----- front desk (§6.5) -----
    def frontdesk(self, topic, text, provisional=False):
        with trace.session(self.db, "frontdesk", topic_id=topic["id"], model=self.gw.model, text=text[:500]) as s:
            self._frontdesk(topic, text, provisional, s)

    def _frontdesk(self, topic, text, provisional, s):
        db = self.db
        cards = db.q("SELECT * FROM cards WHERE origin_topic_id=? AND depth=0 AND kind='task' "
                     "ORDER BY created_at DESC LIMIT 8", topic["id"])
        qs = db.q("SELECT * FROM questions WHERE topic_id=? AND status='open'", topic["id"])
        cat_ids = memory.retrieve(db, topic["title"], text, k=4)
        history = [f"{'owner' if m['direction'] == 'in' else 'assistant'}: {m['text'][:300]}"
                   for m in reversed(db.q("SELECT * FROM messages WHERE topic_id=? ORDER BY created_at DESC LIMIT ?",
                                          topic["id"], HISTORY + 1))][:-1]
        ctx = {"owner": self.owner, "now": datetime.datetime.now().strftime("%A %Y-%m-%d %H:%M"),
               "profile": memory.profile_text(db), "topic_title": topic["title"], "topic_summary": topic["summary"],
               "history": history,
               "cards": [self.card_view(c, qs) for c in cards],
               "questions": [{"id": q["id"], "text": f"{q['handle']} (card {q['card_id']}): {q['text']}"} for q in qs],
               "memory": memory.memory_pack(db, cat_ids, max_notes=4), "text": text}
        steps, acted = [], []
        for k in range(1, FRONTDESK_STEPS + 1):
            try:
                a = calls.frontdesk_step(self.gw, ctx, steps, k, FRONTDESK_STEPS)
            except LLMFailure as e:
                self.log(f"  front desk failed: {e}")
                post(db, topic["id"], "reply", "Sorry, I could not process that. Please try again.")
                return
            t = a["action"]
            s["turns"] = k
            if t in ("reply", "no_reply"):
                s["outcome"] = t + (f": {a['text'][:200]}" if t == "reply" else "")
            if t == "reply":
                body = a["text"]
                if provisional:
                    body = f"(topic?) {body}"
                    if self.hints_shown < HINT_TIMES:
                        body += "\nWrong topic? Reply `→ other-slug` or `→ new`."
                        self.hints_shown += 1
                post(db, topic["id"], "reply", body)
                return
            if t == "no_reply":
                acks = [ack for ack in (_ack(x) for x in acted) if ack]
                if acks:  # the owner must always hear that something was done
                    post(db, topic["id"], "ack", " ".join(acks))
                return
            r = self.frontdesk_tool(topic, a)
            trace.tool_call(db, s["id"], k, a, r)
            if t == "answer_question":
                ctx["questions"] = []  # one answer per turn; the desk otherwise "answers" the others too
            acted.append((a, r))
            steps.append((_desc(a), r))
        # unreachable: last step only allows reply/no_reply

    @staticmethod
    def card_view(c, qs):
        """What the desk sees of a card: state, result summary when done, open question when blocked."""
        v = {"id": c["id"], "title": c["title"], "state": c["state"]}
        if c["state"] == "done" and c["result"]:
            v["result"] = c["result"].get("summary", "") + (
                f" Recommendation: {c['result']['recommendation']}" if c["result"].get("recommendation") else "")
        q = next((q for q in qs if q["card_id"] == c["id"]), None)
        if q:
            v["question"] = f"{q['handle']}: {q['text']}"
        return v

    def frontdesk_tool(self, topic, a):
        db, t = self.db, a["action"]
        if t == "create_card":
            c = board.create_card(db, a["title"], a["goal"], role=a["role"], done_when=a["done_when"],
                                  origin_topic_id=topic["id"], created_by="frontdesk")
            return f"Created card {c['id']}. Handle anything else the message asks for, then reply to {self.owner}."
        if t == "add_to_card":
            board.comment(db, a["card_id"], "owner", a["text"])
            return (f"Added to card {a['card_id']}; its next session will see it. Handle anything else the message "
                    f"asks for, then reply to {self.owner}.")
        if t == "remind":
            when = parse_when(a["when"])
            if not when:
                return f"Error: could not understand the time '{a['when']}'. Ask {self.owner} for a clearer time."
            c = board.create_card(db, a["text"][:80], a["text"], kind="reminder", role="none",
                                  origin_topic_id=topic["id"], created_by="frontdesk", state="ready",
                                  due_at=when.isoformat(timespec="minutes"))
            return f"Reminder {c['id']} set for {when.strftime('%a %d %b %Y, %H:%M')}."
        if t == "answer_question":
            return answer(db, a["question_id"], a["answer"])
        return "Error: unknown tool."

    def after_turn(self, topic, text):
        with trace.session(self.db, "summarize", topic_id=topic["id"], model=self.gw.model) as s:
            self._after_turn(topic, text)
            s["outcome"] = "summary and owner facts updated"

    def _after_turn(self, topic, text):
        """Rolling summary + owner facts (§6.5, §7.4). Batched per turn in the prototype."""
        db = self.db
        topic = db.get("topics", topic["id"])
        n = db.one("SELECT COUNT(*) n FROM messages WHERE topic_id=?", topic["id"])["n"]
        rebuild = n - (topic["summary_msg_count"] or 0) >= SUMMARY_REBUILD_EVERY or not topic["summary"]
        take = SUMMARY_REBUILD_EVERY if rebuild else max(2, n - (topic["summary_msg_count"] or 0))
        msgs = self.topic_lines(topic["id"], take)
        try:
            s = calls.summarize_topic(self.gw, topic["title"], "" if rebuild else topic["summary"], msgs)["summary"]
            db.update("topics", topic["id"], summary=s, summary_msg_count=n)
        except LLMFailure:
            pass
        try:
            facts = calls.extract_owner_facts(self.gw, [f"owner: {text}"])["facts"]
            memory.enqueue_facts(db, [dict(f, source="owner") for f in facts], source_type="owner")
        except LLMFailure:
            pass

    # ----- commands (§6.10, no LLM) -----
    def command(self, text):
        db = self.db
        m = re.fullmatch(r"/new\s+([^\n]+)", text)
        if m:  # a bare /new only opens the topic; the next messages stick to it
            tp = self.new_topic(m.group(1).strip())
            post(db, tp["id"], "reply", f"New topic: {tp['title']}")
            db.insert("messages", id=new_id("msg"), topic_id=tp["id"], binding=self.binding, direction="in",
                      text=text, routed_by="command", route_confidence="high", created_at=now())
            return True
        if text == "/topics":
            post(db, None, "reply", "\n".join(f"{t['slug']} — {t['title']} ({t['status']})"
                                              for t in db.q("SELECT * FROM topics ORDER BY last_activity_at DESC"))
                 or "No topics.")
            return True
        m = re.match(r"^/status(?:\s+([\w-]+))?$", text)
        if m:
            t = db.one("SELECT * FROM topics WHERE slug=?", m.group(1)) if m.group(1) else None
            rows = db.q("SELECT * FROM cards WHERE origin_topic_id=? AND kind!='reminder'", t["id"]) if t else \
                db.q("SELECT * FROM cards WHERE state NOT IN ('done','cancelled','failed') AND kind!='reminder'")
            post(db, t["id"] if t else None, "reply",
                 "\n".join(f"{'  ' * c['depth']}{c['id']} {c['state']:9} {c['role']:10} {c['title']}" for c in rows)
                 or "No cards.")
            return True
        m = re.match(r"^/cancel\s+(crd_\w+)$", text)
        if m:
            board.cancel_tree(db, m.group(1))
            post(db, None, "reply", f"Cancelled {m.group(1)}.")
            return True
        m = re.match(r"^/forget\s+((?:not|clm)_\w+)$", text)
        if m:
            memory.forget(db, m.group(1))
            post(db, None, "reply", f"Forgot {m.group(1)}.")
            return True
        return False


def _ack(x):
    a, r = x
    if a["action"] == "create_card" and r.startswith("Created"):
        return f"On it: \"{a['title']}\"."
    if a["action"] == "add_to_card":
        return "Noted, I passed that on."
    if a["action"] in ("remind", "answer_question") and not r.startswith("Error"):
        return r
    return None


def _desc(a):
    return f"{a['action']}(" + ", ".join(f"{k}={v!r}" for k, v in a.items() if k != "action") + ")"


def fire_reminders(db):
    for c in db.q("SELECT * FROM cards WHERE kind='reminder' AND state='ready' AND due_at <= ?",
                  datetime.datetime.now().isoformat(timespec="minutes")):
        post(db, c["origin_topic_id"], "reminder", f"Reminder: {c['goal']}", dedupe_key=f"reminder:{c['id']}")
        board.transition(db, c["id"], "fire")
