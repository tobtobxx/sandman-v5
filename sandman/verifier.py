"""Verifier (design §5.8): deterministic checks first, then one judge call per
remaining criterion."""
import os
import re
import subprocess

from . import calls
from .llm import LLMFailure

NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def classify(c):
    """Free-text done_when → typed criterion where a pattern matches, else judge."""
    if isinstance(c, dict):
        return c
    m = re.search(r"at least (\d+|one|two|three|four|five|six) (sources?|facts?|files?)", c, re.I)
    if m:
        n = NUM.get(m.group(1).lower()) or int(m.group(1))
        word = m.group(2).lower()
        field = "sources" if word.startswith("source") else "facts" if word.startswith("fact") else "artifacts"
        return {"type": "min_items", "field": field, "n": n, "text": c}
    return {"type": "judge", "text": c}


def check_deterministic(c, result, workdir=None):
    t = c["type"]
    if t == "min_items":
        n = len(result.get(c["field"]) or [])
        return n >= c["n"], f"{c['field']} has {n} items, needs {c['n']}"
    if t == "field_present":
        return bool(result.get(c["field"])), f"{c['field']} is empty"
    if t == "file_exists":
        return os.path.exists(os.path.join(workdir or ".", c["path"])), f"file {c['path']} missing"
    if t == "command_succeeds":
        r = subprocess.run(c["cmd"], shell=True, cwd=workdir, capture_output=True, text=True, timeout=120)
        return r.returncode == 0, f"`{c['cmd']}` failed: {(r.stdout + r.stderr)[-300:]}"
    raise ValueError(t)


def verify(gw, title, result, done_when, result_text, workdir=None):
    """Returns (passed, feedback[])."""
    crits = [classify(c) for c in done_when]
    feedback = []
    for c in crits:
        if c["type"] != "judge":
            ok, why = check_deterministic(c, result, workdir)
            if not ok:
                feedback.append(f"Not met: {c.get('text') or c['type']}: {why}")
    if feedback:
        return False, feedback
    for c in crits:
        if c["type"] == "judge":
            try:
                v = calls.verify_criterion(gw, title, c["text"], result_text)
            except LLMFailure as e:
                v = {"pass": False, "reason": f"verifier failed: {e}"}
            if not v["pass"]:
                feedback.append(f"Not met: {c['text']}: {v['reason']}")
    return not feedback, feedback
