# Benchmark: does the harness + model combination work?

The benchmark tests the Sandman harness as much as the model: every case goes
through the real prompts, schemas, toolsets and (for episodes) the real
worker loop, front desk, router or dispatcher. Model: `qwen/qwen3.6-35b-a3b`,
thinking off, via OpenRouter.

- **core suite**: 153 small isolated tasks, one call type each, plus short
  worker/front-desk episodes.
- **harness suite**: 20 longer tasks where the harness decides the outcome:
  long pages, saved files the worker must read, memory, follow-ups on
  existing cards, several messages through router + front desk, and 4 full
  dispatcher pipelines (librarian → triage → planner → workers → verifier).

Checks are mechanical except 13 cases that use an LLM judge
(`google/gemini-3.8-flash`, with reasoning).

## Result of the harness iteration

Baseline = the first prototype (1 run × 3 repeats). v6 = after the changes
below (3 runs × 3 repeats, pooled).

| group | baseline | v6 | Δ |
|---|---|---|---|
| **core suite** | 95% | **97%** | +2 pp |
| **harness suite** | 65% | **90%** | +25 pp |
| router | 100% | 100% | |
| front desk | 90% | 99% | +9 pp |
| librarian | 96% | 92% | −4 pp |
| planner (triage, plans) | 98% | 94% | −4 pp |
| verifier | 100% | 100% | |
| consolidator | 90% | 100% | +9 pp |
| worker: research | 86% | 93% | +7 pp |
| worker: write | 72% | 98% | +26 pp |
| worker: synthesize | 100% | 100% | |
| worker: code | 50% | 61% | +11 pp |
| pipelines (whole card trees) | 75% | 100% | +25 pp |
| **overall** | 91.3% | **96.3%** | +5 pp |

## Cost

Summed from `usage.cost` of every OpenRouter response (retries included),
cross-checked against the key's own usage counter (they agree within ~5%;
the key counter also bills a few requests that never returned).

| | baseline | v6 |
|---|---|---|
| full run (173 cases × 3), model | $0.139 | **$0.107** |
| full run, judge | $0.050 | $0.049 |
| per case run, average | $0.00027 | $0.00021 |
| per core case run | $0.00013 | $0.00012 |
| per harness case run | $0.00130 (7.8 calls) | $0.00089 (6.5 calls) |
| one pipeline: simple lookup card | | $0.0005 (5 calls) |
| one pipeline: compare-3-kits card tree | | $0.0039 (20 calls) |

The changes made the harness both better and cheaper (fewer wasted steps).

## What changed, and why

Each change came from a failing case; most are **harness** changes, and most
of them *removed* things.

| Change | Failure it fixed | Effect |
|---|---|---|
| File content is written in a **separate plain-text call**: `write_file(path, what)` / `write_artifact(name, what)`, then "write the full content of …" without JSON | On all providers but one, the model loses `\n` in long JSON strings: code became one line, bullet lists one paragraph | write 72→98%, code 50→61% |
| **Long pages are paged**: `web_fetch` saves pages over 2500 chars and shows `[characters 0-2500 of 6106, read on with read_artifact(...)]` first | Facts past the first 3000 chars were invisible (house rules §14) | h_research_1 0→100% |
| **Memory pushed inline** (notes with claims in the context), `open_note` removed | One tool fewer; matches design P9 | see "remaining" below |
| Front desk: **card results and open questions shown on the card line**, `board_status` removed | The desk answered "what did you find out?" from nothing or wasted a step | fd_step_11/13 fixed |
| Front desk: new **`add_to_card(card_id, text)`** | "…and it must work without a tap" had no tool; the desk replied "I've updated the card" without doing anything | h_fd_4, h_conv_1 0→100% |
| Tool results **say what to do next**: "Created card X. Handle anything else the message asks for, then reply to Alex." | Desk ended turns without replying; a bare "reply now" made it skip the reminder in "research X and remind me Friday" | fd_ep_5 fixed |
| **One `answer_question` per turn** (tool disappears after use) | With two open questions it answered the right one, then "answered" the other with "Not specified" | h_fd_3 44→89% (a regression from the new card view; baseline 100%) |
| Open questions moved **next to the new message** ("the new message may answer one"), plus one rule line | "go with the cheaper one" created a new card instead of answering | fd_ep_3 56→100% (regression from the tool changes; baseline 100%) |
| `create_card` doc explains the **roles** in one line | Email tasks went to `research`, analyses to `synthesize` | fd_step_1/7 22–44%→100% (regression from the tool changes) |
| **research toolset without `write_artifact`** | The researcher saved a file instead of finishing | worker_step_3 67→100% |
| **Triage as two questions**: fits-in-one-session (known plans shown as a hint) → only if "no", which recipe | The combined call said "fits: yes" but picked a recipe; the code rule "recipe ⇒ split" over-split simple lookups | pipelines 75→100%, planner −4 pp |
| **Rubric without `durable`** (volatility is known already) | "Swica's 2027 premium" judged not durable → discarded | consolidator 90→100% |
| `match_subject` prompt: "a different thing of the same kind is none" | Porto filed under Lisbon, Helsana under Swica | 32/35 → 34/35 |

### Findings about the method

- **Run-to-run noise is ±3 pp** on the harness suite, much larger than the
  repeats within one run suggest: repeats hit the same provider at low
  temperature and come out nearly identical. Decisions were made on 3 runs × 3
  repeats pooled (9 samples per case). A single run can't show a 3 pp change.
- **OpenRouter providers differ** in their constrained decoding: key order
  (alphabetical), whitespace runaway, dropped keys, lost escapes. A local
  llama.cpp server will differ again; rerun the bench there before trusting
  the numbers.
- **What didn't work**: asking `pick_recipe` on its own for every card (the
  model nearly always picks *some* recipe, even for a lookup: 33/60); a
  single-call triage with the plan chosen first (45/55).

## Remaining weak spots (v6, pooled)

| case | pass | what goes wrong | idea |
|---|---|---|---|
| h_code_2, worker_ep_8 | 0%, 11% | multi-file change / slug edge cases; the coder claims "tests pass" without running them | harness: run the done_when command itself after every write and show the result; or `large` model for code |
| worker_ep_3 | 22% | "compare three kits" in one session: stops after one search | this is what triage + recipes are for; pipeline h_pipe_3 does it at 100% |
| triage_4/7/10 | 56–78% | compare/research-then-write tasks judged "fits one session" | defensible for 12 steps; could add a code rule on the number of named items |
| h_research_4 | 67% | searches the web although memory has a fresh answer | the librarian catches this in the pipeline (h_pipe_2: 0 worker steps) |
| entities_5 | 0% | "EU" not extracted from "EU or Swiss funding programs" | minor |

## Earlier: model comparison (before the harness iteration)

Same bench (core suite only), thinking off: qwen3.6-35b-a3b 96%,
qwen3.8-flash 97%, deepseek-v4.1-flash 94%. The model is not the bottleneck
except for code; harness changes moved the scores far more than switching
models did.

Raw reports: `bench/results/*.md` (per-case failures with outputs), `.json`
for the baseline and the three v6 runs. Compare two runs with
`python -m bench.compare A.json B.json`.
