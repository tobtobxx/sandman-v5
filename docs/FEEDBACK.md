# Feedback on the v5 design, from the prototype

What building and benchmarking the prototype taught us about
[DESIGN.md](DESIGN.md). Meant as input for the next revision of the design.

**Basis.** A prototype of milestones M0–M5 in reduced form (no Matrix, no
projects, no embeddings), 173 benchmark tasks run through the real harness
(see [BENCH.md](BENCH.md)), and a handful of end-to-end chat runs. Model:
qwen3.6-35b-a3b with thinking off, via **OpenRouter**. Not yet on a local Q4
model; see "What we did not learn" at the end.

## Summary

1. **The core bet holds.** A mid-size model with thinking off makes the
   design's small decisions (routing, triage, librarian, verifier,
   consolidator) at 90–100% accuracy. Bigger models did not do better. Keep
   P1, P2 and P5 as they are.
2. **The harness matters more than the model.** Harness changes raised the
   harder tasks from 65% to 90% and cut cost by a quarter. Switching models
   moved scores by ±3 points. The design should treat context packing,
   toolset shape and tool-result wording as first-class, tested design
   surface. Today it treats them as implementation detail.
3. **Constrained decoding is necessary but not sufficient** (P3). Grammar
   engines differ: key order, whitespace, escapes, length limits. Long free
   text must not be generated inside JSON at all.
4. **Toolsets should be even smaller than §5.7/§6.5 say** (P4). Tools that only
   fetch information the harness already has should become context. Every
   intent the owner can express needs a tool, or the model will pretend.
5. **Never let the model report what the harness can measure.** Examples:
   "tests passed", "reminder sent", "I updated the card".
6. **The design has no failure model for the model itself looping or
   collapsing.** Several guards were needed and should be written into the
   design.

## Verdict per principle

| Principle | Verdict | Change |
|---|---|---|
| P1 harness is the brain | ✅ confirmed | none |
| P2 one decision per call | ✅ confirmed, with a caveat | a follow-up decision must only be asked after the gating answer (§ triage) |
| P3 constrained decoding | ⚠️ insufficient alone | add engine-quirk rules and "long text outside JSON" |
| P4 small role toolsets | ✅ confirmed, go further | push info instead of tools; no tempting tools; tools only when applicable |
| P5 board is the only bus | ✅ confirmed | add `add_to_card` (owner follow-ups) |
| P6 short fresh sessions | ✅ confirmed | none |
| P7 route by topic | ✅ router 100% in the bench | minor (bare `/new`) |
| P8 visible assumptions | ✅ `(topic?)` works | none |
| P9 memory pushed | ✅ confirmed, but pushing alone doesn't stop redundant search | librarian "answered" is the real saver |
| P10 offline consolidation | ✅ works | rubric smaller; subject hygiene |
| P11 log every call | ✅ essential | extend to sessions + tool calls + a UI; make it a milestone-0 requirement |

## Proposed changes to the design

Each item names the section, what we saw, and what to write instead.

### 1. P3: constrained decoding rules (§2, §10.1, §10.4)

**Seen.** On OpenRouter, every provider's grammar engine behaved differently:
- JSON keys came out **alphabetically**, not in schema order. A verifier with
  `{pass, reason}` decided before reasoning (86%). Renaming to
  `{reason, verdict}` gave 100%.
- Several engines let the model emit **whitespace forever** after `{`; calls
  hung until the timeout.
- `maxLength` was **never enforced**, and some engines dropped a required key.
- Inside JSON strings, all engines but one **lost `\n`**: code became one
  line, bullet lists one paragraph.

**Proposed text.**
- Every call sets `max_tokens`. The gateway always validates locally and
  truncates over-long strings instead of failing the call.
- The *generation order* of fields is part of the schema design. Where
  reasoning should come first, name keys so that any plausible engine order
  keeps it first, or verify the engine preserves schema order.
- **Long free text (file bodies, emails, reports) is never generated inside
  JSON.** The action names and describes the content, e.g.
  `write_file(path, what)`, and a second, plain-text call produces the body.
  This amends "free text only appears inside schema string fields".
- Each engine/profile has a known-quirks list (e.g. "needs a compact-JSON
  hint", "keys alphabetical") and the benchmark is rerun per engine.

### 2. P4: toolsets (§5.7 roles table, §6.5 front desk tools)

**Seen.**
- `board_status` and `open_note` only fetched information the harness
  already had. Showing card results and pending questions on the card line,
  and notes *with their claims* in the context, removed two tools. It also
  fixed "what did you find out?" answered from nothing.
- The researcher's `write_artifact` was a **temptation**: it saved files
  instead of finishing (67% → 100% after removal).
- The owner said "…and it must work without a tap" about a running card.
  There was no tool for it, so the desk replied "I've updated the card" and
  did nothing. Added `add_to_card`.
- With two open questions, the desk answered the right one, then "answered"
  the other with "Not specified". Fixed by allowing `answer_question` only
  once per turn.
- The desk's tool list first had no descriptions, and "role" meant nothing to
  it: emails went to `research`. One line per tool fixed it.

**Proposed text.**
- A tool that only *reads* state the harness can put in the context is not a
  tool. It's context.
- Offer a tool only when it can apply: `answer_question` only with open
  questions, `add_to_card` only with open cards, `read_artifact` only with
  artifacts, at most one `answer_question` per turn.
- **Every owner intent the desk can meet needs a tool.** Missing tools
  produce confident lies. Front desk tools: `reply`, `no_reply`,
  `create_card`, `add_to_card`, `remind`, `answer_question`
  (drop `board_status`, `open_note`).
- Roles: research = `web_search`, `web_fetch`, `read_artifact`;
  write/synthesize = `read_artifact`, `write_artifact`; code as designed.
- Every tool and every role value gets a one-line description in the prompt.
  The schema enum alone is not enough.

### 3. Tool results are instructions (§5.7, §6.5)

**Seen.** After `create_card`, the desk often ended without replying, or
replied before handling the second half of "research X and remind me Friday".
A tool result "Created card X. Handle anything else the message asks for,
then reply to Alex." fixed both. A blunter "Now reply" made it skip the
reminder, so the exact wording matters and must be benchmarked.

**Proposed text.** Tool results are part of the prompt design: they state
what happened and what to do next. They are versioned and evaluated like
prompts. If the desk acted but ends with `no_reply`, the harness sends a
template acknowledgement.

### 4. Never let the model report measurable facts (§5.7, §5.8)

**Seen.** The coder returned `tests_passed: true` for code that failed its
tests. A worker on a mis-created card "sent" a reminder that never existed.
The desk said it "updated the card" when it had no tool to do so.

**Proposed text.** Any fact the harness can observe is recorded by the
harness, not reported by the model: whether tests pass (the harness runs the
`done_when` command), which artifacts were written, which cards or reminders
exist. Remove such fields from result schemas (`tests_passed`, `artifacts`).
Code cards always get a `command_succeeds` criterion.

### 5. Guards against the model's own failure modes (new section, §13)

**Seen.** The design's failure table covers state failures (crash, card
explosion) but not model behaviour:
- **Loops:** identical searches repeated 5×.
- **Repetition collapse:** a 12,000-character `import re, …, email._qprint,
  email._qprint, …`.
- **Invisible results:** facts past the first 3000 characters of a page.

**Proposed text** (add to §5.7 and §13):
- An identical tool call is not re-executed; the result says "you already did
  this in step k".
- Long, highly repetitive output counts as invalid and is retried once.
- Tool results longer than the window are saved and paged, with the position
  header *first* so truncation can't remove it
  (`[characters 0-2500 of 6106, read on with read_artifact(...)]`).
- On a local server: stream responses; time out only on silence
  (idle timeout, not total time); never resend a timed-out request.

### 6. Triage (§5.5)

**Seen.**
- The single combined call (`fits_one_session`, `recipe_id`, `missing_info`)
  often said "fits: yes" *and* picked a multi-step recipe.
- The code rule "a recipe means split" over-split simple lookups.
- Asking "which recipe?" on its own for every card made the model pick *some*
  recipe for almost everything, even simple lookups (triage score 33/60).
- What works: first ask "fits in one session?", with the recipes shown as a
  hint; only after "no" ask which recipe. Result: 90%.
- The remaining misses are compare tasks the model thinks fit in 12 steps.
  That's arguably true, but it disagrees with the design's intent.
- Cards created by the planner were triaged again and split again.

**Proposed text.**
- Triage = `fits_one_session` + `missing_info`, with the known plans visible
  as context. `pick_recipe` is asked only after "no".
- Cards created by the planner are never split by triage.
- Open question for the design: should "compare N things" always go to a
  recipe (a code rule on the number of named items), or trust the model?

### 7. Planner and recipes (§5.6)

**Seen.**
- `plan_fill` invented a criterion the owner never asked for
  ("durability"). No page had data on it, so the verifier correctly failed
  the card and the owner got a question. That's correct behaviour, but it
  came from a planning error.
- Fan-out needs an item list; the finish schema has none. We fanned out over
  the fact subjects of the gather step.

**Proposed text.** `plan_fill` criteria must come from the owner's request
(quote them, don't invent them). Recipe steps that fan out declare their item
field explicitly (e.g. `result.items[]` in the gather step's schema).

### 8. Verifier (§5.8)

**Seen.** Per-criterion judges work: 100% once reason comes before verdict.
Typed deterministic checks ("at least N sources") catch most failures
cheaply. Verifier feedback as a comment fixes most second attempts. An
impossible criterion ("covers durability") ends in an owner question.

**Proposed text.** Keep as designed. Consider a criterion form "covers X, or
says X is not available" for research criteria, so honest "not found" answers
can pass.

### 9. Memory (§7)

**Seen.**
- Pushing notes into the context works for answering. But the worker still
  searched the web when memory had a fresh answer (the prompt line "finish
  without searching" is ignored ~⅓ of the time). An explicit `open_note` tool
  made it *use* memory more, but cost a step.
- **The librarian's `answered` path is what actually saves work:** zero
  worker steps for a known fact.
- **Rubric:** "durable: still true in a week?" was misread (a 2027 premium or
  opening hours judged "not durable"). Dropping it helped (consolidator
  90% → 100%). Volatility already comes from the worker.
- `match_subject` confused things of the same kind (Porto → Lisbon note).
  The phrase "a different thing of the same kind is none" fixed it.
- Workers produced facts with property subjects ("Price", "Setup"), creating
  junk notes. One prompt line fixed most.
- Retries and continuations re-emit the same facts. The consolidator marks
  them duplicates, but it costs calls.

**Proposed text.**
- The rubric has 4 questions: reusable, costly, task_mechanics, trivial.
  Keep rule: not task_mechanics and not trivial and (reusable or costly).
- The fact schema defines `subject` as "the thing, not a property".
- The fact queue de-duplicates exact repeats from the same card tree before
  consolidation.
- State explicitly that the librarian, not the worker, is the mechanism for
  "don't redo work". Don't rely on worker prompts for it.

### 10. Front desk intent (§6.5, open question)

**Seen.** In a live run, "remind me on Friday to file the tax extension"
became a *write card*, and its worker reported "Reminder sent successfully".
The desk makes two decisions in one call: *what kind* of action, and *its
arguments*.

**Proposed.** Test splitting the desk's first step, following P2: first
classify the intent (reply / new work / reminder / answer / follow-up /
nothing), then ask for the arguments of that one action. This costs one call
per message, but it turns the most consequential free choice into a
classification.

### 11. Observability is part of the design (P11, §14)

**Seen.** Most of the bugs above were found by reading traces, not by
thinking. The design logs LLM calls, but had no link between a tool call and
the call that chose it, and no view of a whole session.

**Proposed text.**
- Milestone 0 includes `sessions` and `tool_calls` tables. Every call is
  tagged with session/card/topic and records schema, provider, cost,
  attempt and latency.
- A read-only inspection UI (session timeline, card detail, call detail)
  comes before the chat UI.

### 12. Evaluation method (§14.2)

**Seen.**
- Isolated call tests saturate fast (95%+) and stop telling models or
  prompts apart. The failures that mattered only showed up in **harness
  tasks**: long pages, saved files, follow-ups, whole card trees.
- **Run-to-run noise is ±3 points.** Repeats within one run hit the same
  provider and come out nearly identical, so they understate it.
- LLM judges are literal: "the *saved* email" failed an email shown as text.
- Cases written together with the prompts overfit.

**Proposed text.**
- Golden sets per call type stay. In addition: a harness suite of short
  episodes and whole card trees on a fixed offline corpus.
- Compare prompt versions on ≥3 independent runs pooled.
- Keep a list of "sensitive" cases (ever failed) for cheap runs.
- Judge criteria describe only what the judge sees.
- Grow cases from real traces (P11), not only hand-written seeds.

### 13. Budgets and latency (§5.10, §16 `context_budgets`)

**Seen.**
- Every call is small: 100–800 input tokens, 10–100 output tokens (worker
  steps ~540 in / ~90 out; plan_generate ~380 out).
- A "compare three kits" card tree is ~20 calls, ~21k tokens in, ~2.3k out.
  On a local model at an assumed 200 tok/s prompt processing and 15 tok/s
  generation, that's roughly 4–5 minutes.
- The design's 8–32k context and its per-slot budgets are far from binding.
  **Output tokens and the number of calls are the real cost.**

**Proposed.**
- Budget in calls per card and output tokens, not context.
- Order every prompt static-first (role preamble, tools, task) so a local
  server's prompt cache can reuse the prefix across the steps of a session.
  This is not measured yet.

### 14. Small items

- A bare `/new Title` must only open the topic, not be treated as a request.
- Answering a question with free text ("cancel it, …") should use the
  option-matching the design describes for quick replies, not exact
  equality.
- The design's `open_note` / catalog wording in §5.7 slot 5 and §6.5 should
  follow item 2.
- Fact volatility, the verifier's reason field and the rubric are all
  "classification after reasoning". Say once, in §10.4, that reasoning fields
  come first.

## What we did not learn

- **A local quantized model.** All numbers are from qwen3.6-35b-a3b on
  OpenRouter at full or near-full precision. Q3/Q4 on llama.cpp may be
  noticeably worse, and its grammar engine behaves differently. Rerun the
  benchmark before trusting any number here. The gateway now supports local
  servers (streaming, idle timeout, thinking off).
- **Latency under one slot**, the interactive reservation, and priorities
  (§5.10). The prototype runs single-threaded between chat messages.
- **Multi-channel conversation** (Matrix, threads, courier policies, digests,
  quiet hours, debouncing), **projects and briefs**, **embeddings** (open
  question 3), the **code sandbox**, and **crash recovery** under real
  crashes. All were out of scope.
- **Long-term memory at scale.** Memory held tens of notes, not thousands.
  Retrieval quality and consolidation drift over months are untested.
