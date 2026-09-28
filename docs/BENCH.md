# Benchmark: can a small model do the jobs the design gives it?

153 small isolated tasks, one call type each (plus 13 short episodes that run
the real worker loop / front desk), each run 3 times. Checks are mechanical
except 11 cases that use an LLM judge (`google/gemini-3.8-flash`, with
reasoning). All models ran with thinking **off**, through OpenRouter.

## Result

| group (cases) | qwen3.6-35b-a3b | qwen3.8-flash | deepseek-v4.1-flash |
|---|---|---|---|
| router (29) | **100%** | 100% | 100% |
| frontdesk (28) | **96%** | 94% | 92% |
| librarian (16) | **98%** | 100% | 88% |
| planner: triage, plan (18) | **96%** | 100% | 98% |
| verifier (12) | **100%** | 97% | 100% |
| consolidator (28) | **92%** | 96% | 96% |
| worker: research (12) | **94%** | 94% | 97% |
| worker: write (4) | **92%** | 75% | 75% |
| worker: synthesize (2) | **100%** | 67% | 83% |
| worker: code (4) | **67%** | 100% | 58% |
| **overall** | **96%** | 97% | 94% |
| cost of the full run ×3 | $0.07 | $0.03 | $0.08 |

Median latency per call is 1–2 s for classification, 2–6 s for worker steps
(API latency, not a local Q4 model).

**Verdict: the model split works with qwen3.6-35b-a3b.** Every
classification-style call (routing, triage, librarian, verifier, rubric) is at
or near 100%, and stronger models do not do meaningfully better. The only role
where a stronger model clearly helps is **code** (67% → 100% with
qwen3.8-flash), which is exactly what the design's `large` escalation profile
is for. Recommendation: qwen3.6 as the `small` profile for everything, a
stronger model as `large` for code cards and retries.

## What mattered more than the model

The first run was 90%. Most of the gain to 96% came from harness fixes the
bench exposed, not from prompts or models:

| Problem found | Effect | Fix |
|---|---|---|
| Providers emit JSON keys **alphabetically**, so `pass` came before `reason` | verifier passed things its own reason said fail (86%) | keys named so alphabetical = intended order (`reason` < `verdict`) → 100% |
| Most providers' grammar engines let Qwen emit whitespace forever after `{` | calls hung until timeout | `max_tokens` + one line in the gateway: "Answer with compact JSON" |
| Newlines dropped inside JSON strings | written files were one long line | same gateway line mentions `\n` |
| `maxLength` not enforced by providers | correct but long reasons rejected | truncate in the gateway instead of failing |
| A provider sometimes drops a required key | invalid output | the design's single retry catches it |
| Front desk prompt did not describe its tools | missed `answer_question`, `remind`, invented results | one-line tool list |
| Long strings collapse into repetition (`email._qprint, email._qprint, …`) | code episodes failed | reject highly compressible strings → retry |
| Worker repeats the identical search 5× | wasted turns | harness answers "you already did this in step k" |
| Triage says "fits: yes" but picks a multi-step recipe | recipe ignored | code rule: picking a recipe means split |

Two more were found only in the end-to-end chat run (not visible in isolated
tasks): planner-made subcards were re-split by triage (over-decomposition),
and the synthesize role had no `sources` field while the recipe required
sources. Both fixed.

## Remaining consistent misses (qwen3.6)

- `match_subject` confuses things of the same kind (Helsana → Swica note, Porto → Lisbon note).
- `relevance_rubric` calls "Swica's 2027 premium" not durable → fact discarded.
- `triage` does not ask for missing info on "Find a hotel for my trip" (asks sometimes).
- Front desk calls `board_status` after `create_card` instead of replying (harmless: the harness sends a template ack).
- Worker does `web_search` when the answer is in a memory note shown in its catalog.
- Code episodes: repetition collapse and wrong assumptions about paths.

## End-to-end

`hi! I have 40 m2 of raised beds. Compare the drip irrigation kits you can buy
in Switzerland and recommend one.` on the fake web runs: router → front desk
creates card → librarian → triage picks `rcp_research_compare_recommend` →
plan_fill → gather → fan-out into 3 detail cards → compare → parent
synthesis → verifier → result reported to the topic. About 100 model calls, ~$0.02 in total.
One detail card blocked because plan_fill chose a criterion ("durability")
the web had no data on; the verifier correctly refused, the owner was asked,
and after the answer the tree completed. That's the design working as
intended, but it shows plan_fill criteria need to stay close to what the owner
asked for.

## Caveats

- The bench is near ceiling for classification calls. It shows the model *can*
  do these jobs, but it no longer separates models; harder and more ambiguous
  cases are the next step.
- Cases and prompts were written by the same author; some overfitting is likely.
- The fake web is small and friendly. Real pages are longer and noisier.
- OpenRouter providers behave differently (grammar engine, key order,
  strictness). A local llama.cpp server with GBNF will behave differently
  again; rerun the bench there (`SANDMAN_BASE_URL`).

Raw reports: `bench/results/*.md` (per-case failures with outputs) and `.json`.
