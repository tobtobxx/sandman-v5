"""Runtime tracing: every LLM call and tool call is attributed to a session
(one librarian/triage/planner/worker/verifier/router/front-desk/... step), and
through it to a card or topic. The web UI reads these rows (design P11, G6)."""
import contextlib
import contextvars

from .db import new_id, now

current = contextvars.ContextVar("sandman_trace", default=None)


@contextlib.contextmanager
def session(db, type, card_id=None, topic_id=None, model=None, **info):
    """Opens a session row; LLM calls made inside are tagged with it.
    Set s["outcome"] (and optionally s["turns"]) before leaving."""
    sid = new_id("ses")
    db.insert("sessions", id=sid, card_id=card_id, topic_id=topic_id, type=type, model=model,
              info=info or None, started_at=now())
    s = {"id": sid, "outcome": None, "turns": 0}
    token = current.set({"session_id": sid, "card_id": card_id, "topic_id": topic_id})
    try:
        yield s
    except Exception as e:
        s["outcome"] = f"error: {type_name(e)}: {e}"[:300]
        raise
    finally:
        current.reset(token)
        db.update("sessions", sid, outcome=s["outcome"], turns=s["turns"], ended_at=now())


def type_name(e):
    return e.__class__.__name__


def tool_call(db, session_id, turn, action, result, ms=None):
    db.insert("tool_calls", id=new_id("tcl"), session_id=session_id, turn=turn, tool=action.get("action"),
              args={k: v for k, v in action.items() if k != "action"}, result=result,
              ok=int(not str(result).startswith("Error")), ms=ms, at=now(),
              after_call=(current.get() or {}).get("last_call"))
