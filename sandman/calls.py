"""Call catalog (design §10.3): one function per call type.

Each function takes plain Python inputs, renders the prompt from prompts.md,
builds the output schema (with dynamic restrictions) and calls the gateway.
Rules the schema can't express are enforced with a `check` function.
"""
import os
import re
import zlib

HERE = os.path.dirname(__file__)
_PROMPTS = None


def prompts():
    global _PROMPTS
    if _PROMPTS is None:
        text = open(os.path.join(HERE, "prompts.md")).read()
        parts = re.split(r"^## (\w+)\n", text, flags=re.M)
        _PROMPTS = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}
    return _PROMPTS


def render(name, **v):
    def sub(m):
        k = m.group(1)
        return str(v[k]) if k in v else m.group(0)
    t = prompts()[name]
    sys_t, _, user_t = t.partition("\n---\n")
    return re.sub(r"\{(\w+)\}", sub, sys_t), re.sub(r"\{(\w+)\}", sub, user_t)


# ---------- schema helpers ----------

def obj(**props):
    # {"const": x} written as a single-value enum: wider grammar-engine support
    props = {k: ({"type": "string", "enum": [v["const"]]} if isinstance(v, dict) and "const" in v else v)
             for k, v in props.items()}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def S(max_len=None):
    s = {"type": "string"}
    if max_len:
        s["maxLength"] = max_len
    return s


NS = {"type": ["string", "null"]}


def enum(values):
    return {"type": "string", "enum": list(values)}


def arr(items, max_items=None, min_items=None):
    a = {"type": "array", "items": items}
    if max_items is not None:
        a["maxItems"] = max_items
    if min_items is not None:
        a["minItems"] = min_items
    return a


BOOL = {"type": "boolean"}
VOLATILITY = ["evergreen", "slow", "volatile"]
FACT = obj(subject=S(80), claim=S(300), source=S(300), volatility=enum(VOLATILITY))
ROLES = ["research", "write", "synthesize", "code"]
ROLE_LINES = {
    "research": "research — find facts on the web, cite sources",
    "write": "write — write a text (email, article, summary) from given material",
    "synthesize": "synthesize — combine results of earlier subtasks into one answer or recommendation",
    "code": "code — change code in a folder and run tests",
}


def lines(items, empty="(none)"):
    items = [i for i in items if i]
    return "\n".join(items) if items else empty


def bullets(items, empty="(none)"):
    return lines([f"- {i}" for i in items], empty)


# ---------- router ----------

def route_sticky(gw, topic_title, last_lines, text):
    s, u = render("route_sticky", topic_title=topic_title, last_lines=lines(last_lines), text=text)
    return gw.call("route_sticky", s, u, obj(same=enum(["yes", "no", "unsure"])))


def route_shortlist(gw, topics, text):
    """topics: list of dicts {slug, title, last}"""
    tl = lines([f"- {t['slug']} — {t['title']} — last: \"{t.get('last', '')}\"" for t in topics])
    s, u = render("route_shortlist", topic_lines=tl, text=text)
    schema = obj(choice=enum([t["slug"] for t in topics] + ["new"]), confidence=enum(["high", "low"]))
    return gw.call("route_shortlist", s, u, schema)


def topic_title(gw, text):
    s, u = render("topic_title", text=text)

    def check(o):
        if len(o["title"].split()) > 6:
            raise ValueError("title longer than 6 words")
    return gw.call("topic_title", s, u, obj(title=S(60)), temperature=0.3, check=check)


# ---------- front desk ----------

def frontdesk_schema(catalog_ids, question_ids, final):
    acts = [obj(action={"const": "reply"}, text=S(1500)), obj(action={"const": "no_reply"})]
    if not final:
        acts += [
            obj(action={"const": "create_card"}, title=S(100), goal=S(600),
                done_when=arr(S(200), max_items=5, min_items=1), role=enum(ROLES)),
            obj(action={"const": "board_status"}, card_id=NS),
            obj(action={"const": "remind"}, when=S(60), text=S(300)),
        ]
        if catalog_ids:
            acts.append(obj(action={"const": "open_note"}, note_id=enum(catalog_ids)))
        if question_ids:
            acts.append(obj(action={"const": "answer_question"}, question_id=enum(question_ids), answer=S(500)))
    return {"anyOf": acts}


FRONTDESK_TOOLS = {
    "reply": "reply(text): send your answer to {owner} and end the turn",
    "no_reply": "no_reply(): end the turn without a message (e.g. after a plain \"thanks\")",
    "create_card": "create_card(title, goal, done_when, role): hand real work to a worker",
    "board_status": "board_status(card_id): see the state and result of a card",
    "remind": "remind(when, text): remind {owner} at a time, e.g. when=\"friday 9am\"",
    "open_note": "open_note(note_id): read a note from memory",
    "answer_question": "answer_question(question_id, answer): pass {owner}'s answer to a card that asked a question",
}


def frontdesk_step(gw, ctx, steps, k, n):
    """ctx: dict with owner, now, profile, topic_title, topic_summary, history[], cards[],
    questions[{id,text}], catalog[{id,title,one_liner}], text. steps: list of (action, result) strings."""
    final = k >= n
    step_txt = ""
    if steps:
        step_txt = "\nYour steps so far:\n" + lines([f"{i+1}. {a}\n   → {r}" for i, (a, r) in enumerate(steps)]) + "\n"
    owner = ctx.get("owner", "the owner")
    tools = [t for t in FRONTDESK_TOOLS if not (t == "open_note" and not ctx.get("catalog"))
             and not (t == "answer_question" and not ctx.get("questions"))]
    tool_lines = bullets([FRONTDESK_TOOLS[t].replace("{owner}", owner) for t in tools])
    s, u = render("frontdesk_step", owner=owner, now=ctx.get("now", ""), tool_lines=tool_lines,
                  profile=ctx.get("profile") or "(none)", topic_title=ctx.get("topic_title", "(new topic)"),
                  topic_summary=ctx.get("topic_summary") or "(none)", history=lines(ctx.get("history", [])),
                  cards=bullets(ctx.get("cards", [])),
                  questions=bullets([f"{q['id']}: {q['text']}" for q in ctx.get("questions", [])]),
                  catalog=bullets([f"{c['id']} — {c['title']} — {c['one_liner']}" for c in ctx.get("catalog", [])]),
                  text=ctx["text"], steps=step_txt, k=k, n=n,
                  final="\nThis is your final step: reply or no_reply." if final else "")
    schema = frontdesk_schema([c["id"] for c in ctx.get("catalog", [])],
                              [q["id"] for q in ctx.get("questions", [])], final)
    return gw.call("frontdesk_step", s, u, schema, temperature=0.2)


def summarize_topic(gw, topic_title, old_summary, messages):
    s, u = render("summarize_topic", topic_title=topic_title, old_summary=old_summary or "(none)",
                  messages=lines(messages))

    def check(o):
        if len(o["summary"].split()) > 170:
            raise ValueError("summary over 150 words")
    return gw.call("summarize_topic", s, u, obj(summary=S(1400)), temperature=0.3, check=check)


def extract_owner_facts(gw, messages):
    s, u = render("extract_owner_facts", messages=lines(messages))
    schema = obj(facts=arr(obj(subject=S(80), claim=S(300), volatility=enum(VOLATILITY)), max_items=8))
    return gw.call("extract_owner_facts", s, u, schema)


# ---------- librarian ----------

def extract_entities(gw, title, goal):
    s, u = render("extract_entities", title=title, goal=goal)
    return gw.call("extract_entities", s, u, obj(entities=arr(S(60), max_items=6)))


def format_notes(notes):
    """notes: [{id, title, one_liner, claims:[{text, observed_at, stale}]}]"""
    out = []
    for n in notes:
        out.append(f"[{n['id']}] {n['title']} — {n.get('one_liner', '')}")
        for c in n.get("claims", []):
            old = ", old" if c.get("stale") else ""
            out.append(f"  - {c['text']} (as of {c.get('observed_at', '?')}{old})")
    return lines(out)


def librarian(gw, card, notes):
    ids = [n["id"] for n in notes] or ["none"]
    s, u = render("librarian", title=card["title"], goal=card["goal"], done_when=bullets(card.get("done_when", [])),
                  notes=format_notes(notes))
    schema = obj(decision=enum(["answered", "narrow", "proceed"]), answer_note_ids=arr(enum(ids)),
                 narrowed_goal=NS, stale_note_ids=arr(enum(ids)))

    def check(o):
        if o["decision"] == "narrow" and not o["narrowed_goal"]:
            raise ValueError("narrow needs narrowed_goal")
        if o["decision"] == "answered" and not o["answer_note_ids"]:
            raise ValueError("answered needs answer_note_ids")
    return gw.call("librarian", s, u, schema, check=check)


def render_answer(gw, card, notes):
    s, u = render("render_answer", title=card["title"], goal=card["goal"],
                  done_when=bullets(card.get("done_when", [])), notes=format_notes(notes))
    return gw.call("render_answer", s, u, obj(summary=S(1000)), temperature=0.3)


# ---------- work ----------

def triage(gw, card, tools, recipes, max_turns, allow_no=True):
    """tools: [(name, description)], recipes: [{id, title}]"""
    s, u = render("triage", max_turns=max_turns, tool_lines=bullets([f"{n}: {d}" for n, d in tools]),
                  title=card["title"], goal=card["goal"], done_when=bullets(card.get("done_when", [])),
                  recipe_lines=bullets([f"{r['id']}: {r['title']}" for r in recipes]))
    fits = ["yes", "no", "unsure"] if allow_no else ["yes", "unsure"]
    schema = obj(fits_one_session=enum(fits), recipe_id=enum([r["id"] for r in recipes] + ["none"]),
                 missing_info=NS)
    return gw.call("triage", s, u, schema)


def triage_split(t):
    """Effective triage decision (code rule): choosing a multi-step plan means splitting."""
    return t["fits_one_session"] == "no" or t["recipe_id"] != "none"


def plan_fill(gw, card, recipe):
    """recipe: {id, title, params: {name: description}, steps: [str]}"""
    s, u = render("plan_fill", title=card["title"], goal=card["goal"], done_when=bullets(card.get("done_when", [])),
                  recipe_title=recipe["title"], recipe_steps=bullets(recipe["steps"]),
                  param_lines=bullets([f"{k}: {d}" for k, d in recipe["params"].items()]))
    props = {k: ({"type": "integer", "minimum": 1, "maximum": 5} if k.startswith("max_") else S(200))
             for k in recipe["params"]}
    return gw.call("plan_fill", s, u, obj(**props))


def plan_generate(gw, card, roles=ROLES):
    s, u = render("plan_generate", role_lines=bullets([ROLE_LINES[r] for r in roles]), title=card["title"],
                  goal=card["goal"], done_when=bullets(card.get("done_when", [])))
    sub = obj(title=S(100), goal=S(500), role=enum(roles), done_when=arr(S(200), max_items=4, min_items=1),
              depends_on=arr({"type": "integer", "minimum": 0}))
    schema = obj(subtasks=arr(sub, min_items=2, max_items=5))

    def check(o):
        for i, t in enumerate(o["subtasks"]):
            if any(d >= i for d in t["depends_on"]):
                raise ValueError(f"subtask {i} depends on a later or itself")
    return gw.call("plan_generate", s, u, schema, temperature=0.3, check=check)


TOOL_DOCS = {
    "web_search": "web_search(query): search the web, returns titles, urls and snippets",
    "web_fetch": "web_fetch(url): read a web page as text",
    "open_note": "open_note(note_id): read a note from memory",
    "read_artifact": "read_artifact(art_id, offset): read a saved file, 3000 characters from offset",
    "write_artifact": "write_artifact(name, content): save a file as a result",
    "read_file": "read_file(path): read a file in your work folder",
    "write_file": "write_file(path, content): write a whole file in your work folder",
    "run": "run(command): run a shell command in your work folder",
    "list_dir": "list_dir(path): list files",
}


def tool_schemas(tools, note_ids, art_ids):
    out = []
    for t in tools:
        if t == "web_search":
            out.append(obj(action={"const": t}, query=S(200)))
        elif t == "web_fetch":
            out.append(obj(action={"const": t}, url=S(500)))
        elif t == "open_note" and note_ids:
            out.append(obj(action={"const": t}, note_id=enum(note_ids)))
        elif t == "read_artifact" and art_ids:
            out.append(obj(action={"const": t}, art_id=enum(art_ids), offset={"type": "integer", "minimum": 0}))
        elif t == "write_artifact":
            out.append(obj(action={"const": t}, name=S(80), content=S(12000)))
        elif t == "read_file":
            out.append(obj(action={"const": t}, path=S(200)))
        elif t == "write_file":
            out.append(obj(action={"const": t}, path=S(200), content=S(12000)))
        elif t == "run":
            out.append(obj(action={"const": t}, command=S(300)))
        elif t == "list_dir":
            out.append(obj(action={"const": t}, path=S(200)))
    return out


def finish_schema(role):
    p = {"action": {"const": "finish"}, "summary": S(1000)}
    if role in ("research", "synthesize"):
        p["sources"] = arr(S(300), max_items=10)
    if role in ("research", "synthesize", "write"):
        p["facts"] = arr(FACT, max_items=8)
    if role == "synthesize":
        p["recommendation"] = NS
    if role == "code":
        p["tests_passed"] = BOOL
    p["open_questions"] = arr(S(200), max_items=5)
    return obj(**p)


TERMINALS = ["finish", "split", "block", "checkpoint", "fail"]


def worker_schema(role, tools, note_ids, art_ids, allowed_terminals, allow_tools=True):
    acts = tool_schemas(tools, note_ids, art_ids) if allow_tools else []
    if "finish" in allowed_terminals:
        acts.append(finish_schema(role))
    if "split" in allowed_terminals:
        acts.append(obj(action={"const": "split"}, reason=S(300),
                        subtasks=arr(obj(title=S(100), goal=S(500), role=enum(ROLES),
                                         done_when=arr(S(200), max_items=4, min_items=1)),
                                     min_items=2, max_items=5)))
    if "block" in allowed_terminals:
        acts.append(obj(action={"const": "block"}, question=S(300), options=arr(S(80), max_items=5), why=S(300)))
    if "checkpoint" in allowed_terminals:
        acts.append(obj(action={"const": "checkpoint"}, progress=S(1000), next_step=S(300),
                        facts=arr(FACT, max_items=8)))
    if "fail" in allowed_terminals:
        acts.append(obj(action={"const": "fail"},
                        category=enum(["tool_error", "impossible", "out_of_scope", "unclear"]), reason=S(300)))
    return {"anyOf": acts}


def worker_step(gw, ctx, k, n, allowed_terminals=TERMINALS):
    """ctx: role, tools[], title, goal, constraints[], done_when[], profile, catalog[{id,title,one_liner}],
    inputs[str], comments[str], transcript[str], art_ids[]"""
    role, tools = ctx["role"], ctx["tools"]
    final = k >= n
    catalog = ctx.get("catalog", [])
    note_ids = [c["id"] for c in catalog]
    art_ids = ctx.get("art_ids", [])
    shown_tools = [t for t in tools if not (t == "open_note" and not note_ids)
                   and not (t == "read_artifact" and not art_ids)]
    preamble = prompts()["role_" + role] + "\n\nYour tools:\n" + bullets([TOOL_DOCS[t] for t in shown_tools])
    s, u = render("worker_step", preamble=preamble, title=ctx["title"], goal=ctx["goal"],
                  constraints=bullets(ctx.get("constraints", [])), done_when=bullets(ctx.get("done_when", [])),
                  profile=ctx.get("profile") or "(none)",
                  catalog=bullets([f"{c['id']} — {c['title']} — {c['one_liner']}" for c in catalog]),
                  inputs=lines(ctx.get("inputs", [])), comments=bullets(ctx.get("comments", [])),
                  transcript=lines(ctx.get("transcript", []), "(none yet)"), k=k, n=n,
                  final="\nThis is your final step: you must choose finish, checkpoint, block or fail." if final else "")
    terminals = [t for t in allowed_terminals if not (final and t == "split")]
    schema = worker_schema(role, tools, note_ids, art_ids, terminals, allow_tools=not final)
    return gw.call("worker_step", s, u, schema, temperature=0.2, max_tokens=5000, check=not_repetitive)


def not_repetitive(o):
    """Small models sometimes collapse into repeating one phrase inside a long
    string. Such output is rejected like a parse failure (retried once)."""
    for v in o.values():
        if isinstance(v, str) and len(v) > 400 and len(zlib.compress(v.encode())) / len(v) < 0.12:
            raise ValueError("repetitive output")


def verify_criterion(gw, title, criterion, result_text):
    s, u = render("verify_criterion", title=title, criterion=criterion, result=result_text)
    # Some providers emit keys alphabetically: "reason" < "verdict" keeps reason-first.
    o = gw.call("verify_criterion", s, u, obj(reason=S(250), verdict=enum(["pass", "fail"])))
    return {"pass": o["verdict"] == "pass", "reason": o["reason"]}


# ---------- consolidator ----------

def match_subject(gw, subject, claim, notes):
    """notes: [{id, title, one_liner}]"""
    s, u = render("match_subject", subject=subject, claim=claim,
                  note_lines=bullets([f"{n['id']} — {n['title']} — {n['one_liner']}" for n in notes]))
    return gw.call("match_subject", s, u, obj(note_id=enum([n["id"] for n in notes] + ["none"])))


RUBRIC_KEYS = ["reusable", "costly", "durable", "task_mechanics", "trivial"]


def relevance_rubric(gw, subject, claim, source, card_title):
    s, u = render("relevance_rubric", subject=subject, claim=claim, source=source, card_title=card_title)
    return gw.call("relevance_rubric", s, u, obj(**{k: BOOL for k in RUBRIC_KEYS}))


def keep_fact(r, volatility="slow"):
    """The keep-rule is code (design §7.5); only the booleans come from the model."""
    if r["task_mechanics"] or r["trivial"]:
        return False
    if not (r["reusable"] or r["costly"]):
        return False
    if not r["durable"] and not (volatility == "volatile" and r["costly"]):
        return False
    return True


def consolidate_fact(gw, note_title, claims, claim, source, observed_at):
    """claims: [{id, text, source, observed_at}]"""
    ids = [c["id"] for c in claims] or ["none"]
    cl = bullets([f"[{c['id']}] {c['text']} ({c.get('source', '?')}, {c.get('observed_at', '?')})" for c in claims])
    s, u = render("consolidate_fact", title=note_title, claim_lines=cl, claim=claim, source=source,
                  observed_at=observed_at)
    schema = obj(decision=enum(["new", "duplicate", "update", "contradicts", "discard"]),
                 target_claim_id={"anyOf": [enum(ids), {"type": "null"}]})

    def check(o):
        if o["decision"] in ("duplicate", "update", "contradicts") and not o["target_claim_id"]:
            raise ValueError("decision needs target_claim_id")
    return gw.call("consolidate_fact", s, u, schema, check=check)


def render_note(gw, title, claims):
    s, u = render("render_note", title=title, claim_lines=bullets([c["text"] for c in claims]))
    return gw.call("render_note", s, u, obj(one_liner=S(200)), temperature=0.3)


# ---------- bench only ----------

def judge(gw, input_text, output_text, criterion):
    s, u = render("judge", input=input_text, output=output_text, criterion=criterion)
    o = gw.call("judge", s, u, obj(reason=S(300), verdict=enum(["pass", "fail"])))
    return {"pass": o["verdict"] == "pass", "reason": o["reason"]}
