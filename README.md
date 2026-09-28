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
docs/DEVIATIONS.md
```

## Run

```bash
pip install requests jsonschema pytest   # dateparser optional
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

`bench/results/` holds the reports of the runs discussed in `docs/BENCH.md`.
