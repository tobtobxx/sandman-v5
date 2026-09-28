"""LLM gateway: one entry point for every model call (design §10.1).

Every call has a call_type, a system prompt, a user prompt and a JSON schema.
Output is produced under the schema (constrained decoding via the provider),
validated locally, retried once, and logged.
"""
import json
import os
import time
import uuid

import jsonschema
import requests

from . import trace


JSON_HINT = "\n\nAnswer with compact JSON. Use \\n for line breaks inside strings."


class LLMFailure(Exception):
    pass


class Gateway:
    def __init__(self, model=None, base_url=None, api_key=None, reasoning=False,
                 log=None, timeout=90):
        self.model = model or os.environ.get("SANDMAN_MODEL", "qwen/qwen3.6-35b-a3b")
        self.base_url = base_url or os.environ.get("SANDMAN_BASE_URL", "https://openrouter.ai/api/v1")
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("SANDMAN_API_KEY", "")
        self.reasoning = reasoning
        self.log = log          # callable(dict) or None
        self.timeout = timeout
        self.cost = 0.0
        self.calls = 0

    def _post(self, messages, schema, temperature, max_tokens):
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "out", "strict": True, "schema": schema}}
        if "openrouter.ai" in self.base_url:
            body["provider"] = {"require_parameters": schema is not None}
            if os.environ.get("SANDMAN_PROVIDERS"):  # e.g. "Darkbloom,AkashML"
                body["provider"]["order"] = os.environ["SANDMAN_PROVIDERS"].split(",")
            body["reasoning"] = {"enabled": bool(self.reasoning)}
        last = None
        for attempt in range(4):  # transport retries only
            try:
                r = requests.post(f"{self.base_url}/chat/completions", json=body, timeout=(10, self.timeout),
                                  headers={"Authorization": f"Bearer {self.api_key}"})
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                d = r.json()
                if "choices" not in d:
                    last = str(d)[:300]
                    time.sleep(2 ** attempt)
                    continue
                return d
            except requests.RequestException as e:
                last = str(e)
                time.sleep(2 ** attempt)
        raise LLMFailure(f"transport: {last}")

    def call(self, call_type, system, user, schema, temperature=0.0, check=None, meta=None, max_tokens=1500):
        """Returns the parsed object. `check` is an optional extra validator
        (raises ValueError) for rules JSON schema cannot express.
        schema=None is a plain-text call (used for long content such as files,
        which small models write badly inside JSON strings); returns a str."""
        # Without this hint, several providers' grammar engines let Qwen-class
        # models emit whitespace forever after "{" (see docs/BENCH.md).
        hint = JSON_HINT if schema is not None else ""
        messages = [{"role": "system", "content": system + hint}, {"role": "user", "content": user}]
        err = None
        for attempt in range(2):  # design: retry once, lower temperature
            t0 = time.time()
            raw, parsed, usage, provider = None, None, {}, None
            try:
                d = self._post(messages, schema, temperature if attempt == 0 else 0.0, max_tokens)
                raw = d["choices"][0]["message"].get("content") or ""
                if d["choices"][0].get("finish_reason") == "length":
                    raise ValueError(f"output cut off at max_tokens={max_tokens}")
                usage = d.get("usage", {})
                provider = d.get("provider")
                self.cost += usage.get("cost", 0) or 0
                if schema is None:
                    parsed = _strip_fences(raw)
                else:
                    parsed = _truncate(json.loads(_strip_fences(raw)), schema)
                    jsonschema.validate(parsed, schema)
                if check:
                    check(parsed)
                err = None
            except (json.JSONDecodeError, jsonschema.ValidationError, ValueError) as e:
                err = f"{type(e).__name__}: {str(e)[:300]}"
            except LLMFailure as e:
                err = str(e)
            self.calls += 1
            call_id = "cal_" + uuid.uuid4().hex[:12]
            ctx = trace.current.get()
            if ctx is not None:
                ctx["last_call"] = call_id  # tool calls after this point were chosen by this call
            if self.log:
                self.log({"id": call_id, "call_type": call_type, "model": self.model,
                          "system": system, "user": user, "raw": raw, "parsed": parsed if not err else None,
                          "ok": err is None, "error": err, "attempt": attempt, "provider": provider,
                          "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
                          "cost": usage.get("cost"), "ms": int((time.time() - t0) * 1000),
                          "temperature": temperature if attempt == 0 else 0.0, "max_tokens": max_tokens,
                          "schema": schema, "system_hint": hint, **(trace.current.get() or {}),
                          "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **(meta or {})})
            if err is None:
                return parsed
        raise LLMFailure(f"{call_type}: {err}")


def _truncate(v, schema):
    """Providers don't enforce maxLength; cut over-long strings instead of
    failing the whole call over a verbose reason or summary."""
    if isinstance(v, str) and "maxLength" in schema:
        return v[:schema["maxLength"]]
    if isinstance(v, dict):
        for branch in schema.get("anyOf", [schema]):
            props = branch.get("properties", {})
            tag = props.get("action", {}).get("enum")
            if tag and v.get("action") not in tag:
                continue  # union branch for another action
            return {k: _truncate(x, props.get(k, {})) for k, x in v.items()}
        return v
    if isinstance(v, list) and "items" in schema:
        return [_truncate(x, schema["items"]) for x in v]
    return v


def _strip_fences(s):
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def jsonl_logger(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def log(rec):
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return log
