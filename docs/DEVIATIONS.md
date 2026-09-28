# Deviations from the design document

This is a prototype built to test whether the design's model split works with
weak models. Deviations are deliberate shortcuts unless noted.

**Scope.** Milestones M0–M5 are present in reduced form (gateway, board,
dispatcher, triage/planner/worker/verifier, router + front desk + outbox,
memory). There is no web UI and no Matrix adapter; the only channel is a CLI
chat (`python -m sandman chat`). Debouncing, quiet hours, digests, rate
limits, nags and multi-binding courier rules are left out, because with a
single CLI binding they have nothing to decide. Router stage 2 (platform
threads/replies) is left out for the same reason.

**Constrained decoding via OpenRouter.** The gateway uses
`response_format: json_schema` (strict) with `provider.require_parameters`,
instead of a local llama.cpp grammar. Outputs are also validated locally with
`jsonschema`, and rules a schema cannot express (DAG order, "narrow needs a
goal", word limits) are checked in code. A failed check counts as a parse
failure and is retried once, as the design says.

**Flat action objects.** Tool calls and terminal actions are flat
(`{"action": "web_search", "query": ...}`, `{"action": "finish", "summary":
...}`) instead of `{"action": "tool", "name": ..., "args": {...}}`. One
discriminator with fewer nesting levels is easier for small models, and the
union schema (`anyOf`) still limits the options to exactly the allowed tools
and terminal actions.

**Retrieval without vectors.** Notes are found by exact title/alias match and
SQLite FTS5 (BM25). There is no embedding model and no vector search (see
design open question 3). Topic shortlists rank by recency and word overlap.

**Workers see the memory catalog but no librarian notes inline.** The catalog
(ids, titles, one-liners) is shown and `open_note` is available; the "inline
the top 2 notes" step is left out to keep prompts short.

**Recipe fan-out over fact subjects.** The `gather` step of
`rcp_research_compare_recommend` fans out over the distinct `subject`s of its
result's `facts` (the gather goal asks for one fact per candidate), because
the finish schema has no separate item list.

**No sandbox for `run`.** The code role's `run` executes in the card's work
folder with a timeout, but not in a container. Do not point code cards at
anything valuable.

**Artifacts are harness-tracked.** The finish schema has no `artifacts` field;
the harness attaches the ids written by `write_artifact` during the session.
The model cannot reference artifacts it did not write.

**IDs** are time-ordered hex with a type prefix, not ULIDs.

**Large-model escalation** is implemented (`--large-model`) as a
`model_profile=large` flag on the card for its next attempt.

**Owner facts** are extracted from each inbound owner message after every
front-desk turn (not batched with summary updates).

## Additions found necessary by the benchmark (see docs/BENCH.md)

**Gateway JSON hint.** The gateway appends one line to every system prompt:
"Answer with compact JSON. Use \n for line breaks inside strings." Without it,
most OpenRouter providers let the model emit whitespace forever after `{`, and
newlines inside strings get lost. This is a transport workaround, not a task
instruction. `max_tokens` is always set.

**Key order.** Some providers generate JSON keys in alphabetical order, not
schema order. Where "reason first, then decide" matters, keys are named so the
alphabetical order is the intended one (`reason` before `verdict`).

**Over-long strings are truncated**, not rejected, because providers don't
enforce `maxLength`.

**Repetition guard.** A worker step with a long, highly repetitive string is
treated as invalid output and retried once.

**Repeated tool calls are not re-executed.** The worker gets "You already did
exactly this in step k" instead.

**Triage rule in code.** Choosing a recipe means splitting, whatever
`fits_one_session` says. Cards created by the planner are never split again by
triage (the planner already sized them).

**Front desk template ack.** If the front desk acted (card, reminder, answer)
but ended with `no_reply`, the harness sends a short template acknowledgement.
