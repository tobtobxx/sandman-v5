# Sandman v5 — prototype

A pragmatic prototype of the Sandman v5 design (multi-agent harness for weak,
slow models), plus a benchmark that checks whether a given model can do each
job the design gives to the model.

The design's bet is: keep all control flow in code, and ask the model only
small, single-decision questions under a JSON schema. The benchmark tests
exactly those questions, one call type at a time, plus a few short episodes
(worker loops, front-desk turns) that run the real harness code.

## Layout

```
sandman/
  prompts.md      every prompt, one section per call type (system --- user)
  calls.py        call catalog: renders prompts, builds schemas (dynamic enums, unions)
  llm.py          gateway: OpenAI-compatible, json_schema output, validate, retry once, log
  board.py        cards, deps, events; the state machine (one transition table)
  dispatcher.py   new → librarian → triage → plan | work → verify → done/retry/ask
  worker.py       worker loop: one action per call, last turn terminal-only
  verifier.py     typed done_when: deterministic checks first, then one judge per criterion
  conversation.py router cascade, front desk, outbox/courier, questions, commands
  memory.py       notes/claims, fact queue, consolidator (rubric + merge), librarian retrieval
  recipes.py      two seed recipes
  tools.py        web (live or fake corpus), artifacts, files, run
  trace.py        sessions + tool calls; tags every LLM call with its session/card/topic
  web.py          read-only web UI over the whole runtime (python -m sandman web)
bench/
  cases/*.py      173 tasks: core suite (isolated calls) + harness suite (harness.py:
                  long pages, files, memory, follow-ups, full dispatcher pipelines)
  corpus.py       fake web pages for worker episodes (incl. a prompt injection, a long page)
  run.py          runner + report with costs (bench/results/*.md|json)
  cases/quick.py  which cases the default (quick) run includes
  compare.py      per-group and per-case diff of two runs
tests/            offline tests of the harness with a scripted fake model
docs/            DESIGN.md (the design), ARCHITECTURE.md (how it is built), ROLES.md (every
                  role: prompt, tools, output, harness rules), BENCH.md, DEVIATIONS.md
```

## Run

### NixOS / nix (recommended)

`flake.nix` has a dev shell with Python and all dependencies from nixpkgs:

```bash
nix develop                      # then run the commands below as they are
# or one-off, without entering the shell:
nix develop -c python -m sandman --fake-web bench chat
```

(`nix run nixpkgs#python3 -- …` alone does not work: `requests` and
`jsonschema` are not in the plain interpreter.)

### uv (any Linux/macOS)

`pyproject.toml` + `uv.lock` pin the dependencies; prefix every command with
`uv run`:

```bash
uv run python -m sandman --fake-web bench chat
nix run nixpkgs#uv -- run python -m sandman --fake-web bench chat   # uv via nix
```

On NixOS, uv's own downloaded Python and the prebuilt native wheel of
`rpds-py` (a jsonschema dependency) may need `programs.nix-ld.enable = true`.
Using the flake avoids that.

### Local model (llama.cpp, vLLM, Ollama, …)

Any OpenAI-compatible server works; pass the endpoint and the model name:

```bash
python -m sandman --base-url http://localhost:8080/v1 --model qwen3.6-35b-a3b chat
python -m bench.run --base-url http://localhost:8080/v1 --model qwen3.6-35b-a3b --workers 1
# or once: export SANDMAN_BASE_URL=http://localhost:8080/v1 SANDMAN_MODEL=qwen3.6-35b-a3b
```

For non-OpenRouter endpoints the gateway:
- streams the response and only gives up when the server sends nothing for
  `--idle-timeout` seconds (default 600); slow generation is never cut off,
  and a timed-out request is not resent;
- sends `chat_template_kwargs: {"enable_thinking": false}` (thinking off;
  `--thinking` turns it on) and llama.cpp's `return_progress`, so prompt
  processing counts as activity;
- sends the schema as `response_format: {"type": "json_schema", …}`, which
  llama.cpp turns into a grammar;
- sends no API key unless `--api-key` / `$SANDMAN_API_KEY` is set (never the
  OpenRouter key).

The bench's judge stays on OpenRouter (`$OPENROUTER_API_KEY`), so local runs
still need that key for the 13 judged cases. `--workers 1` avoids queueing
parallel requests on a server with one slot (llama.cpp `-np`).

### Commands

```bash
export OPENROUTER_API_KEY=...            # or SANDMAN_BASE_URL for a local llama.cpp/vLLM server

python -m pytest tests                   # harness logic, no model needed
python -m bench.run                      # quick set (61 cases: harness suite + sensitive core cases)
python -m bench.run --full               # all 173 cases; default model qwen/qwen3.6-35b-a3b, thinking off
python -m bench.run --repeat 3 --only router,triage   # or --only harness
python -m bench.compare bench/results/A.json bench/results/B.json
python -m bench.run --model qwen/qwen3.8-flash
SANDMAN_PROVIDERS=Darkbloom python -m bench.run    # prefer an OpenRouter provider

python -m sandman --fake-web bench chat   # or live web without --fake-web
python -m sandman card "Gardena price" "Find the price of the GARDENA Micro-Drip starter set" \
    --done-when "states the price in CHF"
python -m sandman board
python -m sandman web --port 8080          # inspect everything in the browser (read-only)
```

State lives in `.sandman/` in the current directory (`--home` to change).

`bench/results/` holds the reports of the runs discussed in `docs/BENCH.md`.
