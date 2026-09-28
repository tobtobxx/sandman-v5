"""Check helpers. Each check is (output, ctx) -> (ok, detail).
Mechanical wherever possible; `judge` asks a stronger model."""
import json
import re

from sandman import calls


def get(out, path):
    cur = out
    for p in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            cur = cur[int(p)] if p.isdigit() and int(p) < len(cur) else None
        else:
            cur = cur.get(p)
    return cur


def text_of(v):
    return v.lower() if isinstance(v, str) else json.dumps(v, ensure_ascii=False).lower()


class Check:
    def __init__(self, desc, fn):
        self.desc, self.fn = desc, fn

    def __call__(self, out, ctx):
        try:
            ok = bool(self.fn(out, ctx))
        except Exception as e:  # a check crashing on odd output is a failure, not a bench bug
            return False, f"{self.desc} (error: {e})"
        return ok, self.desc


def eq(path, value):
    return Check(f"{path} == {value!r}", lambda o, c: get(o, path) == value)


def one_of(path, values):
    return Check(f"{path} in {values!r}", lambda o, c: get(o, path) in values)


def not_in(path, values):
    return Check(f"{path} not in {values!r}", lambda o, c: get(o, path) not in values)


def contains(path, *words):
    """Every word must appear (case-insensitive). A tuple means any of them."""
    def fn(o, c):
        t = text_of(get(o, path) if path else o)
        return all(any(x.lower() in t for x in (w if isinstance(w, tuple) else (w,))) for w in words)
    return Check(f"{path or 'output'} contains {words}", fn)


def lacks(path, *words):
    return Check(f"{path or 'output'} lacks {words}",
                 lambda o, c: not any(w.lower() in text_of(get(o, path) if path else o) for w in words))


def length(path, lo=0, hi=10 ** 9):
    return Check(f"len({path}) in [{lo},{hi}]", lambda o, c: lo <= len(get(o, path) or []) <= hi)


def words(path, lo=0, hi=10 ** 9):
    return Check(f"words({path}) in [{lo},{hi}]", lambda o, c: lo <= len((get(o, path) or "").split()) <= hi)


def custom(desc, fn):
    return Check(desc, fn)


def judge(criterion, path=None):
    """LLM judge (bench only, stronger model). Input = the case inputs."""
    def fn(o, c):
        out = get(o, path) if path else o
        v = calls.judge(c["judge_gw"], json.dumps(c["inputs"], ensure_ascii=False, default=str)[:6000],
                        out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)[:6000],
                        criterion)
        c.setdefault("judge_reasons", []).append(v["reason"])
        return v["pass"]
    return Check(f"judge: {criterion}", fn)
