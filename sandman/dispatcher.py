"""Dispatcher (design §5.4, §5.10): picks runnable cards, runs the right
session type and applies the resulting transition. All control flow is here."""
import os

from . import board, calls, memory, recipes, verifier
from .board import MAX_ATTEMPTS, MAX_CONTINUATIONS, MAX_DEPTH, TERMINAL, transition
from .conversation import ask, fire_reminders, post
from .llm import LLMFailure
from .tools import ArtifactStore, ToolEnv
from .worker import MAX_TURNS, ROLE_TOOLS, result_text, run_worker

LIBRARIAN_ROLES = {"research", "write", "synthesize"}
INPUT_CHARS = 1500


class Dispatcher:
    def __init__(self, db, gw, web, workspace, gw_large=None, log=print):
        self.db, self.gw, self.web, self.workspace = db, gw, web, workspace
        self.gw_large, self.log = gw_large, log

    # ----- loop -----
    def tick(self):
        db = self.db
        board.reclaim_expired(db)
        fire_reminders(db)
        self.join_waiting()
        c = board.next_runnable(db)
        if not c:
            return False
        self.log(f"· {c['id']} [{c['state']}/{c['kind']}/{c['role']}] {c['title']}")
        if c["state"] == "new":
            self.plan(c) if c["kind"] == "plan" else self.preflight(c)
        elif c["state"] == "ready":
            self.work(c)
        elif c["state"] == "verifying":
            self.verify(c)
        return True

    def run_until_idle(self, max_ticks=200, after_tick=None):
        for _ in range(max_ticks):
            did = self.tick()
            if after_tick:
                after_tick()
            if not did:
                return

    def gw_for(self, card):
        return self.gw_large if card.get("model_profile") == "large" and self.gw_large else self.gw

    # ----- new → librarian → triage (§5.4) -----
    def preflight(self, c):
        db, gw = self.db, self.gw
        if c["role"] in LIBRARIAN_ROLES and c["phase"] == "execute":
            d, ids = memory.librarian_preflight(gw, db, c)
            self.log(f"  librarian: {d['decision']} (notes: {ids})")
            if d["decision"] == "answered":
                try:
                    notes = [memory.note_view(db, i) for i in d["answer_note_ids"]]
                    s = calls.render_answer(gw, c, notes)["summary"]
                    transition(db, c["id"], "librarian_answered",
                               result={"summary": s, "source": "memory", "notes": d["answer_note_ids"]})
                    return
                except LLMFailure:
                    pass
            if d["decision"] == "narrow" and d["narrowed_goal"]:
                db.update("cards", c["id"], goal=d["narrowed_goal"], original_goal=c["goal"])
                board.comment(db, c["id"], "librarian", f"Narrowed using {', '.join(ids)}")
                c = db.get("cards", c["id"])
        tools = [(t, calls.TOOL_DOCS[t].split(": ", 1)[1]) for t in ROLE_TOOLS.get(c["role"], [])]
        rcps = recipes.shortlist(c["goal"]) if c["depth"] < MAX_DEPTH - 1 else []
        try:
            # planner-made cards were sized by the planner: triage may not split them again
            allow_no = c["depth"] < MAX_DEPTH and c["created_by"] != "planner"
            t = calls.triage(gw, c, tools, rcps if allow_no else [], MAX_TURNS.get(c["role"], 8), allow_no=allow_no)
        except LLMFailure:
            t = {"fits_one_session": "unsure", "recipe_id": "none", "missing_info": None}
        self.log(f"  triage: {t}")
        if t["missing_info"] and c["created_by"] != "planner":
            ask(db, c, t["missing_info"])
            transition(db, c["id"], "missing_info")
        elif calls.triage_split(t) and c["depth"] < MAX_DEPTH:
            board.create_card(db, f"Plan: {c['title']}", c["goal"], kind="plan", role="plan", parent=c,
                              done_when=c["done_when"], created_by="triage",
                              recipe_id=None if t["recipe_id"] == "none" else t["recipe_id"])
            transition(db, c["id"], "triage_split")
        else:
            transition(db, c["id"], "triage_single")

    # ----- planner (§5.6) -----
    def plan(self, p):
        db = self.db
        parent = db.get("cards", p["parent_id"])
        try:
            if p["recipe_id"] in recipes.RECIPES:
                r = recipes.RECIPES[p["recipe_id"]]
                params = calls.plan_fill(self.gw, parent, recipes.for_prompt(r))
                self.log(f"  plan_fill {r['id']}: {params}")
                self.instantiate(parent, r, params)
            else:
                subs = calls.plan_generate(self.gw, parent)["subtasks"]
                self.log(f"  plan_generate: {[s['title'] for s in subs]}")
                ids = []
                for s in subs:
                    ch = board.create_card(db, s["title"], s["goal"], role=s["role"], done_when=s["done_when"],
                                           parent=parent, depends_on=[ids[i] for i in s["depends_on"]],
                                           created_by="planner")
                    ids.append(ch["id"])
            transition(db, p["id"], "planned")
        except (LLMFailure, board.BoardError) as e:
            self.log(f"  planner failed: {e}")
            transition(db, p["id"], "plan_failed", payload={"error": str(e)[:300]})
            if db.get("cards", parent["id"])["state"] == "waiting":
                transition(db, parent["id"], "plan_fallback")

    def instantiate(self, parent, r, params):
        db, keys = self.db, {}
        for s in r["steps"]:
            if s.get("fanout"):
                keys[s["key"]] = keys[s["fanout"]]   # dependents wait on the source until fan-out
                continue
            ch = board.create_card(db, recipes.fill(s["title"], params), recipes.fill(s["goal"], params),
                                   role=s["role"], done_when=[recipes.fill(x, params) for x in s["done_when"]],
                                   parent=parent, depends_on=[keys[d] for d in s.get("depends_on", [])],
                                   created_by="planner", recipe_id=r["id"], recipe_step=s["key"],
                                   recipe_params=params)
            keys[s["key"]] = ch["id"]

    def fanout(self, c):
        """When a recipe step finishes, create the per-item cards that fan out from it."""
        db = self.db
        r = recipes.RECIPES.get(c["recipe_id"] or "")
        if not r:
            return
        for s in r["steps"]:
            if s.get("fanout") != c["recipe_step"]:
                continue
            params = c["recipe_params"] or {}
            items = []
            for f in (c["result"] or {}).get("facts", []):
                if f["subject"] not in items:
                    items.append(f["subject"])
            items = items[:int(params.get("max_options", 3))]
            parent = db.get("cards", c["parent_id"])
            dependents = [x for x in r["steps"] if s["key"] in x.get("depends_on", [])]
            new_ids = []
            for it in items:
                p = dict(params, item=it)
                ch = board.create_card(db, recipes.fill(s["title"], p), recipes.fill(s["goal"], p), role=s["role"],
                                       done_when=[recipes.fill(x, p) for x in s["done_when"]], parent=parent,
                                       depends_on=[c["id"]], created_by="planner", recipe_id=r["id"],
                                       recipe_step=s["key"], recipe_params=p)
                new_ids.append(ch["id"])
            for d in dependents:
                for sib in db.q("SELECT id FROM cards WHERE parent_id=? AND recipe_step=?", parent["id"], d["key"]):
                    for nid in new_ids:
                        db.insert("card_deps", card_id=sib["id"], depends_on=nid)
            self.log(f"  fan-out: {items}")

    # ----- worker (§5.7) -----
    def inputs(self, c):
        db, out = self.db, list(c["inputs"] or [])
        srcs = [db.get("cards", d["depends_on"]) for d in db.q("SELECT * FROM card_deps WHERE card_id=?", c["id"])]
        if c["phase"] == "synthesize":
            srcs = [x for x in board.children(db, c["id"]) if x["kind"] != "plan"]
        for s in srcs:
            if s["state"] == "done":
                body = result_text(s["result"] or {})
            else:
                body = f"({s['state']}) " + "; ".join(board.comments(db, s["id"])[-1:])
            out.append(f"Result of \"{s['title']}\" [{s['state']}]:\n{body[:INPUT_CHARS]}")
        return out

    def work(self, c):
        db = self.db
        c = board.claim(db, c)
        arts = ArtifactStore(os.path.join(self.workspace, c["root_id"]))
        workdir = next((i[8:] for i in c["inputs"] or [] if isinstance(i, str) and i.startswith("workdir:")), None)
        if c["role"] == "code" and not workdir:
            workdir = os.path.join(self.workspace, c["root_id"], c["id"])
            os.makedirs(workdir, exist_ok=True)
        cat_ids = memory.retrieve(db, c["title"], c["goal"])
        env = ToolEnv(web=self.web, artifacts=arts, workdir=workdir, card_id=c["id"])
        allowed = list(calls.TERMINALS)
        if c["depth"] >= MAX_DEPTH or c["phase"] == "synthesize":
            allowed.remove("split")
        role = c["role"] if c["role"] in ROLE_TOOLS else "research"
        ctx = {"role": role, "title": c["title"], "goal": c["goal"], "constraints": c["constraints"],
               "done_when": c["done_when"], "profile": memory.profile_text(db), "memory": memory.memory_pack(db, cat_ids),
               "inputs": [i for i in self.inputs(c) if not i.startswith("workdir:")],
               "comments": board.comments(db, c["id"])}

        def on_step(k, a, r):
            board.renew(db, c["id"])
            self.log(f"    {k}. {a['action']} {str({x: y for x, y in a.items() if x != 'action'})[:100]}")

        a, steps = run_worker(self.gw_for(c), ctx, env, allowed_terminals=allowed, on_step=on_step)
        self.log(f"  → {a['action']}: {str(a)[:200]}")
        self.apply(c, a, env)

    def apply(self, c, a, env):
        db, t = self.db, a["action"]
        if t == "finish":
            result = {k: v for k, v in a.items() if k != "action"}
            result["artifacts"] = env.written
            transition(db, c["id"], "finish", result=result)
        elif t == "split":
            for s in a["subtasks"]:
                board.create_card(db, s["title"], s["goal"], role=s["role"], done_when=s["done_when"], parent=c,
                                  created_by="worker")
            transition(db, c["id"], "split", payload={"reason": a["reason"]})
        elif t == "block":
            ask(db, c, a["question"], a["options"])
            transition(db, c["id"], "block", payload={"why": a["why"]})
        elif t == "checkpoint":
            memory.enqueue_facts(db, a["facts"], c["id"])
            if c["continuation_n"] >= MAX_CONTINUATIONS:
                ask(db, c, f"This card checkpointed {c['continuation_n']} times, it seems mis-scoped. "
                           f"Progress: {a['progress'][:300]}. Continue / cancel?", ["continue", "cancel"])
                transition(db, c["id"], "fail_block")
                return
            transition(db, c["id"], "checkpoint", result={"kind": "checkpoint", "summary": a["progress"]})
            parent = db.get("cards", c["parent_id"]) if c["parent_id"] else None
            cont = board.create_card(db, c["title"], c["goal"], role=c["role"], done_when=c["done_when"],
                                     constraints=c["constraints"],
                                     inputs=(c["inputs"] or []) + [f"Progress so far: {a['progress']}\n"
                                                                   f"Next step: {a['next_step']}"],
                                     parent=parent, created_by="system", continuation_n=c["continuation_n"] + 1,
                                     origin_topic_id=c["origin_topic_id"], recipe_id=c["recipe_id"],
                                     recipe_step=c["recipe_step"], recipe_params=c["recipe_params"],
                                     phase=c["phase"])
            db.x("UPDATE card_deps SET depends_on=? WHERE depends_on=?", cont["id"], c["id"])
            db.x("INSERT INTO card_deps SELECT ?, depends_on FROM card_deps WHERE card_id=?", cont["id"], c["id"])
            if c["depth"] == 0:  # root continuation keeps reporting to the topic
                db.update("cards", cont["id"], root_id=c["root_id"])
        elif t == "fail":
            self.escalate(c, "fail", f"{a['category']}: {a['reason']}", hard=a["category"] in ("out_of_scope", "unclear"))

    def escalate(self, c, kind, reason, hard=False):
        """§5.9: retry → large model once → ask the owner."""
        db = self.db
        board.comment(db, c["id"], "verifier" if kind == "verify" else "harness", reason)
        retry_ev, block_ev = ("verify_fail", "verify_fail_block") if kind == "verify" else ("fail_retry", "fail_block")
        if not hard and c["attempt"] < MAX_ATTEMPTS:
            transition(db, c["id"], retry_ev, attempt=c["attempt"] + 1, payload={"reason": reason})
        elif not hard and self.gw_large and c["model_profile"] != "large":
            transition(db, c["id"], retry_ev, attempt=c["attempt"] + 1, model_profile="large",
                       payload={"reason": reason, "escalate": "large"})
        else:
            ask(db, c, f"Card failed: {reason[:300]}. Retry, cancel, or give guidance?", ["retry", "cancel"])
            transition(db, c["id"], block_ev, payload={"reason": reason})

    # ----- verifier (§5.8) -----
    def verify(self, c):
        db = self.db
        res = c["result"] or {}
        workdir = next((i[8:] for i in c["inputs"] or [] if isinstance(i, str) and i.startswith("workdir:")), None)
        arts = ArtifactStore(os.path.join(self.workspace, c["root_id"]))
        text = result_text(res, arts)
        for a in res.get("artifacts", [])[:2]:
            text += f"\n\nFile {a}:\n" + arts.read(a, 0, 2000)
        ok, fb = verifier.verify(self.gw, c["title"], res, c["done_when"] or [], text, workdir)
        self.log(f"  verify: {'pass' if ok else fb}")
        if ok:
            transition(db, c["id"], "verify_pass")
            memory.enqueue_facts(db, res.get("facts"), c["id"])
            self.fanout(db.get("cards", c["id"]))
            self.report(db.get("cards", c["id"]))
        elif res.get("source") == "memory":
            # memory answer not good enough: proceed normally, memory result as input
            db.update("cards", c["id"], inputs=(c["inputs"] or []) + [f"From memory (incomplete): {res['summary']}"])
            board.comment(db, c["id"], "verifier", "; ".join(fb))
            transition(db, c["id"], "verify_fail", payload={"memory": True})
        else:
            self.escalate(c, "verify", "; ".join(fb))

    def report(self, c):
        """Root cards report to their topic (§5.12, template mode)."""
        if c["depth"] != 0 or c["kind"] != "task" or c["state"] not in TERMINAL:
            return
        arts = ArtifactStore(os.path.join(self.workspace, c["root_id"]))
        body = f"Done: {c['title']}\n{result_text(c['result'] or {}, arts)}" if c["state"] == "done" else \
            f"Card {c['title']} ended {c['state']}."
        post(self.db, c["origin_topic_id"], "result", body, card_id=c["id"], dedupe_key=f"result:{c['id']}")

    def join_waiting(self):
        db = self.db
        for c in db.q("SELECT * FROM cards WHERE state='waiting'"):
            kids = board.children(db, c["id"])
            if kids and all(k["state"] in TERMINAL for k in kids):
                transition(db, c["id"], "children_done", phase="synthesize", role="synthesize", attempt=1)
