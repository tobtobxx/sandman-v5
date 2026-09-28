"""Memory layer (design §7): notes made of claims, a fact queue, an offline
consolidator, and a librarian that pushes memory into sessions."""
import datetime
import re

from . import calls, trace
from .db import new_id, now
from .llm import LLMFailure

MAX_AGE_DAYS = {"volatile": 7, "slow": 180, "evergreen": None}
SOURCE_RANK = {"owner": 3, "url": 2, "card": 1}


def _days_since(d):
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(d[:10])).days
    except (TypeError, ValueError):
        return 0


def is_stale(claim):
    age = MAX_AGE_DAYS.get(claim["volatility"])
    return age is not None and _days_since(claim["observed_at"]) > age


# ---------- notes ----------

def create_note(db, title, kind="entity", aliases=(), one_liner=""):
    nid = new_id("not")
    db.insert("notes", id=nid, kind=kind, title=title, aliases=list(aliases), one_liner=one_liner, dirty=1,
              updated_at=now())
    reindex(db, nid)
    return nid


def add_claim(db, note_id, text, source, observed_at=None, volatility="slow", status="active"):
    cid = new_id("clm")
    db.insert("claims", id=cid, note_id=note_id, text=text, source=source, observed_at=observed_at or now()[:10],
              volatility=volatility, status=status)
    db.update("notes", note_id, dirty=1, updated_at=now())
    return cid


def active_claims(db, note_id, limit=20):
    return db.q("SELECT * FROM claims WHERE note_id=? AND status IN ('active','disputed') ORDER BY observed_at DESC "
                "LIMIT ?", note_id, limit)


def note_view(db, note_id):
    n = db.get("notes", note_id)
    n["claims"] = [{"id": c["id"], "text": c["text"] + (" [disputed]" if c["status"] == "disputed" else ""),
                    "observed_at": c["observed_at"], "stale": is_stale(c),
                    "source": (c["source"] or {}).get("type", "?")} for c in active_claims(db, note_id)]
    return n


def note_text(db, note_id):
    n = note_view(db, note_id)
    return calls.format_notes([n])


def reindex(db, note_id):
    n = db.get("notes", note_id)
    db.x("DELETE FROM notes_fts WHERE note_id=?", note_id)
    if n["status"] == "active":
        cl = " ".join(c["text"] for c in active_claims(db, note_id))
        db.x("INSERT INTO notes_fts (note_id, title, aliases, one_liner, claims) VALUES (?,?,?,?,?)",
             note_id, n["title"], " ".join(n["aliases"] or []), n["one_liner"] or "", cl)


def profile_text(db):
    n = db.one("SELECT id FROM notes WHERE kind='profile' AND status='active'")
    if not n:
        return ""
    return "\n".join(f"- {c['text']}" for c in active_claims(db, n["id"]))


def profile_note_id(db):
    n = db.one("SELECT id FROM notes WHERE kind='profile'")
    return n["id"] if n else create_note(db, "Owner profile", kind="profile", one_liner="The owner's preferences")


# ---------- retrieval (no LLM) ----------

def exact_match(db, name):
    name = name.strip().lower()
    for n in db.q("SELECT * FROM notes WHERE status='active' AND kind != 'profile'"):
        if n["title"].lower() == name or name in [a.lower() for a in (n["aliases"] or [])]:
            return n["id"]
    return None


def search(db, text, k=6):
    """FTS5 BM25 over notes. Vector search omitted (docs/DEVIATIONS.md)."""
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2]
    if not words:
        return []
    qs = " OR ".join(f'"{w}"' for w in words[:30])
    rows = db.q("SELECT note_id FROM notes_fts WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts) LIMIT ?", qs, k)
    return [r["note_id"] for r in rows]


def retrieve(db, title, goal, entities=(), k=6):
    ids = []
    for e in entities:
        nid = exact_match(db, e)
        if nid and nid not in ids:
            ids.append(nid)
    for nid in search(db, f"{title} {goal} {' '.join(entities)}", k):
        if nid not in ids and len(ids) < k + len(entities):
            n = db.get("notes", nid)
            if n and n["kind"] != "profile":
                ids.append(nid)
    return ids


def catalog(db, ids):
    return [{"id": n["id"], "title": n["title"], "one_liner": n["one_liner"] or ""}
            for n in (db.get("notes", i) for i in ids) if n]


def memory_pack(db, ids, max_notes=6, max_claims=5):
    """Notes with their claims, pushed into the session (P9) instead of an open_note tool."""
    out = []
    for i in ids[:max_notes]:
        n = note_view(db, i)
        n["claims"] = n["claims"][:max_claims]
        out.append(n)
    return out


# ---------- librarian (pre-flight, §7.6) ----------

def librarian_preflight(gw, db, card):
    """Returns (decision dict, note_ids). Never raises on model failure."""
    try:
        ents = calls.extract_entities(gw, card["title"], card["goal"])["entities"]
    except LLMFailure:
        ents = []
    ids = retrieve(db, card["title"], card["goal"], ents)
    if not ids:
        return {"decision": "proceed", "answer_note_ids": [], "narrowed_goal": None, "stale_note_ids": []}, ids
    try:
        d = calls.librarian(gw, card, [note_view(db, i) for i in ids])
    except LLMFailure:
        d = {"decision": "proceed", "answer_note_ids": [], "narrowed_goal": None, "stale_note_ids": []}
    return d, ids


# ---------- writing (§7.4) ----------

def enqueue_facts(db, facts, card_id=None, source_type="card"):
    for f in facts or []:
        src = f.get("source") or ""
        stype = "owner" if source_type == "owner" else ("url" if src.startswith("http") else "card")
        db.insert("facts", id=new_id("fct"), card_id=card_id, subject=f["subject"], text=f["claim"],
                  source={"type": stype, "ref": src, "card_id": card_id}, volatility=f.get("volatility", "slow"),
                  created_at=now())


# ---------- consolidator (§7.5) ----------

def consolidate(gw, db, limit=50, log=print):
    with trace.session(db, "consolidator", model=getattr(gw, "model", None)) as s:
        n = _consolidate(gw, db, limit, log)
        s["outcome"], s["turns"] = f"{n} facts processed", n


def _consolidate(gw, db, limit, log):
    count = 0
    for f in db.q("SELECT * FROM facts WHERE status='pending' ORDER BY created_at LIMIT ?", limit):
        try:
            decision, reason = _consolidate_one(gw, db, f)
        except LLMFailure as e:
            decision, reason = "error", str(e)[:200]
            db.update("facts", f["id"], decision=decision, decision_reason=reason)
            continue
        count += 1
        status = {"discard": "discarded", "contradicts": "review"}.get(decision, "merged")
        db.update("facts", f["id"], status=status, decision=decision, decision_reason=reason)
        log(f"  fact {f['id']} [{f['subject']}] {f['text'][:60]!r} → {decision} ({reason})")
    for n in db.q("SELECT * FROM notes WHERE dirty=1"):
        cl = active_claims(db, n["id"])
        if cl and n["kind"] != "profile":
            try:
                db.update("notes", n["id"], one_liner=calls.render_note(gw, n["title"], cl)["one_liner"])
            except LLMFailure:
                pass
        db.update("notes", n["id"], dirty=0)
        reindex(db, n["id"])
    return count


def _retracted(db, f):
    ref = (f["source"] or {}).get("ref")
    return ref and db.one("SELECT 1 FROM claims WHERE status='retracted' AND json_extract(source,'$.ref')=? "
                          "AND text=?", ref, f["text"])


def _consolidate_one(gw, db, f):
    src = f["source"] or {}
    if _retracted(db, f):
        return "discard", "retracted before"
    # 1. resolve subject
    if src.get("type") == "owner":
        nid = profile_note_id(db)
    else:
        nid = exact_match(db, f["subject"])
        if not nid:
            cands = [i for i in search(db, f"{f['subject']} {f['text']}", 3)
                     if db.get("notes", i)["kind"] != "profile"]
            if cands:
                m = calls.match_subject(gw, f["subject"], f["text"], catalog(db, cands))["note_id"]
                nid = None if m == "none" else m
    # 2. relevance rubric (owner facts are always relevant)
    if src.get("type") != "owner":
        card = db.get("cards", f["card_id"]) if f["card_id"] else None
        r = calls.relevance_rubric(gw, f["subject"], f["text"], src.get("type", "card"),
                                   card["title"] if card else "(conversation)")
        if not calls.keep_fact(r, f["volatility"]):
            return "discard", "rubric " + ",".join(k for k, v in r.items() if v)
    if not nid:
        nid = create_note(db, f["subject"].strip()[:80])
        add_claim(db, nid, f["text"], src, volatility=f["volatility"])
        return "new", "new note"
    # 3. merge decision
    claims = active_claims(db, nid, 5)
    if not claims:
        add_claim(db, nid, f["text"], src, volatility=f["volatility"])
        return "new", "empty note"
    view = [{"id": c["id"], "text": c["text"], "source": (c["source"] or {}).get("type"),
             "observed_at": c["observed_at"]} for c in claims]
    d = calls.consolidate_fact(gw, db.get("notes", nid)["title"], view, f["text"], src.get("type"), now()[:10])
    dec, tid = d["decision"], d["target_claim_id"]
    if dec == "new":
        add_claim(db, nid, f["text"], src, volatility=f["volatility"])
    elif dec == "duplicate":
        db.x("UPDATE claims SET corroborations=corroborations+1 WHERE id=?", tid)
    elif dec in ("update", "contradicts"):
        old = db.get("claims", tid)
        same_or_better = SOURCE_RANK.get(src.get("type"), 0) >= SOURCE_RANK.get((old["source"] or {}).get("type"), 0)
        owner_wins = src.get("type") == "owner"
        if owner_wins or (f["volatility"] != "evergreen" and same_or_better and dec == "update"):
            new = add_claim(db, nid, f["text"], src, volatility=f["volatility"])
            db.update("claims", tid, status="superseded", superseded_by=new)
            dec = "update"
        else:
            new = add_claim(db, nid, f["text"], src, volatility=f["volatility"], status="disputed")
            db.update("claims", tid, status="disputed")
            db.insert("review_items", id=new_id("rev"), kind="conflict", status="open",
                      payload={"note_id": nid, "a": tid, "b": new})
            dec = "contradicts"
    return dec, f"note {nid}"


def forget(db, ref):
    """Retract a note or claim (§7.10)."""
    if ref.startswith("clm_"):
        db.update("claims", ref, status="retracted")
        c = db.get("claims", ref)
        reindex(db, c["note_id"])
    elif ref.startswith("not_"):
        db.update("notes", ref, status="retracted")
        db.x("UPDATE claims SET status='retracted' WHERE note_id=?", ref)
        reindex(db, ref)
