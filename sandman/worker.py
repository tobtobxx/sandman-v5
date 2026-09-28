"""Worker session (design §5.7): a loop of single-action model calls that must
end in a terminal action. Independent of the DB so the bench can run it."""
from . import calls
from .llm import LLMFailure

ROLE_TOOLS = {
    "research": ["web_search", "web_fetch", "read_artifact"],
    "write": ["read_artifact", "write_artifact"],
    "synthesize": ["read_artifact", "write_artifact"],
    "code": ["read_file", "write_file", "run", "list_dir"],
}
MAX_TURNS = {"research": 12, "write": 8, "synthesize": 6, "code": 15}
LAST_RESULT_CHARS = 3000
OLD_RESULT_CHARS = 1000


def describe(a):
    args = ", ".join(f"{k}={_short(v)}" for k, v in a.items() if k != "action")
    return f"{a['action']}({args})"


def _short(v, n=80):
    s = repr(v)
    return s if len(s) <= n else s[:n] + "...'"


def transcript_lines(steps):
    """steps: [(action_dict, result)]; the harness truncates, not the model."""
    out = []
    for i, (a, r) in enumerate(steps):
        limit = LAST_RESULT_CHARS if i == len(steps) - 1 else OLD_RESULT_CHARS
        if len(r) > limit:
            r = r[:limit] + f"\n[... {len(r) - limit} characters omitted]"
        out.append(f"{i + 1}. {describe(a)}\n→ {r}")
    return out


def run_worker(gw, ctx, env, max_turns=None, allowed_terminals=None, on_step=None):
    """ctx: see calls.worker_step. Returns (terminal_action, steps).
    A model failure (unparseable twice) becomes a fail action, never an exception."""
    role = ctx["role"]
    ctx = dict(ctx, tools=ctx.get("tools") or ROLE_TOOLS[role])
    n = max_turns or MAX_TURNS[role]
    allowed = allowed_terminals or calls.TERMINALS
    steps = []
    for k in range(1, n + 1):
        ctx["transcript"] = transcript_lines(steps)
        if env.artifacts is not None:
            ctx["art_ids"] = list(env.artifacts.index)
        try:
            a = calls.worker_step(gw, ctx, k, n, allowed)
        except LLMFailure as e:
            return {"action": "fail", "category": "tool_error", "reason": f"model output invalid: {e}"}, steps
        if a["action"] in calls.TERMINALS:
            return a, steps
        prev = next((i for i, (b, _) in enumerate(steps) if b == a), None)
        if prev is not None:  # weak models loop; don't re-run, point back instead
            r = f"You already did exactly this in step {prev + 1}. Use that result or choose another action."
        elif a["action"] in ("write_file", "write_artifact"):
            try:  # the content itself is a separate plain-text call
                a["content"] = calls.write_content(gw, ctx, k, n, a.get("path") or a.get("name"), a["what"])
                r = env.execute(a)
            except LLMFailure as e:
                r = f"Error: writing failed ({e}). Try again."
        else:
            r = env.execute(a)
        steps.append((a, r))
        if on_step:
            on_step(k, a, r)
    # unreachable: the last turn's schema only allows terminal actions
    return {"action": "fail", "category": "tool_error", "reason": "no terminal action"}, steps


def result_text(result, artifacts=None):
    """Render a finish result for verifiers, parents and the front desk."""
    out = [result.get("summary", "")]
    if result.get("recommendation"):
        out.append(f"Recommendation: {result['recommendation']}")
    if result.get("facts"):
        out.append("Facts:\n" + "\n".join(f"- {f['subject']}: {f['claim']} ({f['source']})" for f in result["facts"]))
    if result.get("sources"):
        out.append("Sources:\n" + "\n".join(f"- {s}" for s in result["sources"]))
    if result.get("artifacts") and artifacts:
        out.append("Files:\n" + "\n".join(artifacts.lines(result["artifacts"])))
    if result.get("open_questions"):
        out.append("Open questions:\n" + "\n".join(f"- {q}" for q in result["open_questions"]))
    if "tests_passed" in result:
        out.append(f"Tests passed: {result['tests_passed']}")
    return "\n\n".join(o for o in out if o)
