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


class LLMTimeout(LLMFailure):
    """The server stopped sending. Not retried: resending won't unstick it."""


class _Retry(Exception):
    """A transient server answer (429/5xx) worth resending."""


class Gateway:
    """base_url: OpenRouter (default) or any OpenAI-compatible server, e.g.
    llama.cpp at http://localhost:8080/v1. Local servers are streamed: a call
    only times out when the server sends nothing for idle_timeout seconds, so
    slow generation is never cut off, and a timeout is not resent."""

    def __init__(self, model=None, base_url=None, api_key=None, reasoning=False, log=None, idle_timeout=None):
        self.model = model or os.environ.get("SANDMAN_MODEL", "qwen/qwen3.6-35b-a3b")
        self.base_url = (base_url or os.environ.get("SANDMAN_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")
        self.openrouter = "openrouter.ai" in self.base_url
        # never send the OpenRouter key to another server
        self.api_key = api_key or os.environ.get("SANDMAN_API_KEY") or \
            (os.environ.get("OPENROUTER_API_KEY", "") if self.openrouter else "")
        self.reasoning = reasoning
        self.log = log          # callable(dict) or None
        self.idle_timeout = float(idle_timeout or os.environ.get("SANDMAN_IDLE_TIMEOUT", 600))
        self.cost = 0.0
        self.calls = 0

    def _body(self, messages, schema, temperature, max_tokens):
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "out", "strict": True, "schema": schema}}
        if self.openrouter:
            body["provider"] = {"require_parameters": schema is not None}
            if os.environ.get("SANDMAN_PROVIDERS"):  # e.g. "Darkbloom,AkashML"
                body["provider"]["order"] = os.environ["SANDMAN_PROVIDERS"].split(",")
            body["reasoning"] = {"enabled": bool(self.reasoning)}
        else:
            # llama.cpp / vLLM: thinking on/off goes through the chat template
            body["chat_template_kwargs"] = {"enable_thinking": bool(self.reasoning)}
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            body["return_progress"] = True  # llama.cpp: progress chunks during prompt processing
        return body

    def _post(self, messages, schema, temperature, max_tokens):
        body = self._body(messages, schema, temperature, max_tokens)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = f"{self.base_url}/chat/completions"
        last = None
        for attempt in range(4):  # transport retries: connection errors and 429/5xx only
            try:
                if body.get("stream"):
                    return self._stream(url, body, headers)
                r = requests.post(url, json=body, headers=headers, timeout=(10, 90))
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
            except _Retry as e:
                last = str(e)
                time.sleep(2 ** attempt)
            except requests.exceptions.ReadTimeout as e:
                if body.get("stream"):  # the server went quiet: resending won't help
                    raise LLMTimeout(f"no data from the server for {self.idle_timeout:.0f} s") from e
                last = str(e)
                time.sleep(2 ** attempt)
            except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
                last = str(e)
                time.sleep(2 ** attempt)
            except requests.RequestException as e:
                raise LLMFailure(f"transport: {e}") from e
        raise LLMFailure(f"transport: {last}")

    def _stream(self, url, body, headers):
        """Server-sent events → the same dict shape as a non-streamed response.
        requests' read timeout applies between received bytes, i.e. it is an idle timeout."""
        with requests.post(url, json=body, headers=headers, stream=True, timeout=(10, self.idle_timeout)) as r:
            if r.status_code in (429, 500, 502, 503, 504):
                raise _Retry(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400:
                raise LLMFailure(f"HTTP {r.status_code}: {r.text[:300]}")
            content, finish, usage = [], None, {}
            try:
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue  # keep-alives, comments
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise LLMFailure(f"server error: {str(chunk['error'])[:300]}")
                    usage = chunk.get("usage") or usage
                    for ch in chunk.get("choices") or []:
                        content.append((ch.get("delta") or {}).get("content") or "")
                        finish = ch.get("finish_reason") or finish
            except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
                # mid-stream silence surfaces as ConnectionError(ReadTimeoutError); never resend
                raise LLMTimeout(f"no data from the server for {self.idle_timeout:.0f} s "
                                 f"(after {sum(map(len, content))} chars)") from e
        return {"choices": [{"message": {"content": "".join(content)}, "finish_reason": finish}], "usage": usage}

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
            raw, parsed, usage, provider, timed_out = None, None, {}, None, False
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
                timed_out = isinstance(e, LLMTimeout)
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
            if timed_out:
                raise LLMTimeout(f"{call_type}: {err}")
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
