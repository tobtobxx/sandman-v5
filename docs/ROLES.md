# Roles: who the model is asked to be, with what prompt, tools and output

Sandman never runs "an agent" with a big prompt. It asks the model small
questions in specific roles. There are two kinds:

- **Decision roles**: one call, one question, an answer constrained to a
  schema (router, triage, librarian, verifier, consolidator, ...). The harness
  acts on the answer.
- **Worker roles** (research, write, synthesize, code) and the **front desk**:
  a short loop where each call picks exactly one action (a tool or a terminal
  action) until the session ends.

The prompts live in [`sandman/prompts.md`](../sandman/prompts.md) (one section
per call type, system prompt above `---`, user prompt below). Output schemas
are built in [`sandman/calls.py`](../sandman/calls.py) and enforced by
constrained decoding, so **prompts never describe the output format**. The
web UI (`/prompts`) shows the live prompts, toolsets and example schemas.

Scores are from the benchmark (qwen3.6-35b-a3b, thinking off, v6, 3 runs × 3
repeats pooled; see [BENCH.md](BENCH.md)).

## Overview

| Role / call type | Called by | When | Answers | Score |
|---|---|---|---|---|
| router: `route_sticky` | conversation | a message arrives shortly after the last one | same topic? yes/no/unsure | 100% |
| router: `route_shortlist` | conversation | not decided by commands/sticky | which of ≤5 topics, or new; confidence | 100% |
| router: `topic_title` | conversation | a new topic is opened | title ≤6 words | 100% |
| front desk: `frontdesk_step` | conversation | every routed owner message | one action (≤4 steps) | 96–100% |
| note taker: `summarize_topic` | after each desk turn | | summary ≤150 words | 100% |
| note taker: `extract_owner_facts` | after each desk turn | | facts the owner stated | 100% |
| librarian: `extract_entities` | dispatcher, new card | | ≤6 names | 83% |
| librarian: `librarian` | dispatcher, new card | notes were found | answered / narrow / proceed | 100% |
| librarian: `render_answer` | dispatcher | librarian said answered | result from notes only | 83% |
| planner: `triage` | dispatcher, new card | after the librarian | fits one session? + missing info | 90% |
| planner: `pick_recipe` | dispatcher | only after triage said "no" | recipe or none | (in triage) |
| planner: `plan_fill` | dispatcher, plan card | a recipe was picked | the recipe's parameters | 100% |
| planner: `plan_generate` | dispatcher, plan card | no recipe | 2–5 subtasks as a DAG | 100% |
| worker: `worker_step` | worker loop | each turn | one tool call or terminal action | 99% (single steps) |
| worker: `write_content` | worker loop | after `write_file`/`write_artifact` | the file content, plain text | (in episodes) |
| verifier: `verify_criterion` | dispatcher | card finished, per judge criterion | reason + pass/fail | 100% |
| consolidator: `match_subject` | consolidator | fact's subject has no exact note | which note, or none | 100% |
| consolidator: `relevance_rubric` | consolidator | non-owner facts | 4 booleans (keep rule in code) | 99% |
| consolidator: `consolidate_fact` | consolidator | fact lands in a note with claims | new/duplicate/update/contradicts/discard | 100% |
| consolidator: `render_note` | consolidator | a note changed | one-liner ≤20 words | 100% |

Worker episodes (whole sessions): research 93%, write 98%, synthesize 100%,
code 61%.

## Common rules for every prompt

- System prompt: "Your role is the X." plus 2–5 plain sentences. No generic
  "helpful assistant" text, no format instructions.
- The question comes last, right before generation.
- Options are listed with the same names the schema's enum uses.
- The gateway appends one line to every JSON call: *"Answer with compact JSON.
  Use \n for line breaks inside strings."* (a provider workaround, see
  DEVIATIONS.md).
- Where "think, then decide" matters, the reason field comes first. Some
  providers emit keys alphabetically, so the keys are named to sort that way
  (`reason` < `verdict`).

---

## Router

**Job:** put each owner message into the right topic. Most messages never
reach the model: `/t slug`, `#slug`, `/new Title`, the web topic selector and
`Q3: …` answers are handled in code. The model only sees the ambiguous rest.

**Cascade** (`Conversation.route`):
1. explicit command / selector → decisive
2. `route_sticky` if the last message on this binding is < 20 min old; `yes` → decisive
3. `route_shortlist` over ≤5 candidate topics (recency + word overlap)
4. low confidence, or sticky said unsure and shortlist disagrees → the reply
   is marked `(topic?)` with a hint "Wrong topic? Reply `→ other-slug`".

```
Your role is the router.

You sort chat messages into conversation topics.

Short replies like "ok", "yes", "thanks" belong to the current topic.
---
Current topic: "{topic_title}"
Last messages in this topic: …
New message: "{text}"

Is the new message about the current topic? Answer yes, no, or unsure.
```

Outputs: `{same: yes|no|unsure}` · `{choice: <slug>|new, confidence: high|low}`
· `{title}` (≤6 words checked in code). The slug is derived in code.

## Front desk

**Job:** the only role that talks to the owner. One short session per message,
at most 4 steps; the last step allows only `reply`/`no_reply`.

**Context it gets** (built by the harness, `Conversation._frontdesk`): owner
profile, topic title + rolling summary, the last 8 messages, the topic's cards
*with their result summary when done and their pending question when
blocked*, up to 4 memory notes with claims, open questions (placed right
before the new message), the new message, and its own steps so far.

**Tools** (only offered when they apply):

| Tool | Effect |
|---|---|
| `reply(text)` | send the answer; ends the turn |
| `no_reply()` | end silently (e.g. after "thanks") |
| `create_card(title, goal, done_when, role)` | new root card in this topic. role: research / write / synthesize / code |
| `add_to_card(card_id, text)` | only if the topic has open cards: adds the owner's new wish as a comment the card's next session sees |
| `remind(when, text)` | reminder card; `when` is parsed by the harness, the parsed time is returned |
| `answer_question(question_id, answer)` | only if questions are open; unblocks the card. At most once per turn |

There is no `board_status` and no `open_note`: what they returned is already
in the context.

**Harness rules:** tool results say what to do next ("Created card X. Handle
anything else the message asks for, then reply to Alex."). If the desk acted
but ends with `no_reply`, a template acknowledgement is sent. Provisional
routing prefixes the reply with `(topic?)`.

**Known weakness:** it sometimes turns "remind me on Friday to …" into a
write card instead of calling `remind` (seen in a live run, not in the bench).

## Note taker (after each front-desk turn)

`summarize_topic`: old summary + new messages → ≤150 words. Once 20 messages
have come in since the last summary, it is rebuilt from the messages instead
of from the old summary (against drift).
`extract_owner_facts`: facts the owner stated about themselves ("The owner
lives in Basel"), each with a volatility class; they go to the fact queue as
owner-sourced and end up in the profile note.

## Librarian (before any work)

Runs for every new research/write/synthesize card (`memory.librarian_preflight`):

1. `extract_entities` → ≤6 names (products, places, organisations, …)
2. exact title/alias lookup + FTS5 search over notes (code)
3. if notes were found: `librarian` sees the goal, done_when and the notes
   with their claims (stale ones marked "old") and decides:
   - **answered** → `render_answer` writes the result from the notes only;
     the card goes straight to the verifier. No worker session at all.
   - **narrow** → the goal is replaced by a shorter one (the original is kept)
   - **proceed**

Pipeline h_pipe_2 in the bench: a library-hours question is answered from
memory with zero worker steps.

## Planner

**`triage`**: "can this be done in ONE session?" with the role's tools, the
turn budget and the known recipes shown *as a hint*. Also returns
`missing_info`, a question for the owner if the task cannot start (the card
then blocks before any work). Examples in the prompt: library hours → yes;
compare 4 insurers → no.

**`pick_recipe`**: asked **only after "no"**. Asked on its own, the model
picks some recipe for almost everything, including simple lookups.

Code rules around triage: at max depth, "no" is not offered by the schema;
cards made by the planner are never split again.

**`plan_fill`** (recipe mode, preferred): fills the recipe's parameters, e.g.
for `rcp_research_compare_recommend`: subject, criteria, max_options (1–5). The
harness creates the step cards and fans out one detail card per candidate
when the gather step finishes.

**`plan_generate`** (free mode): 2–5 subtasks, each with title, goal, role,
done_when and `depends_on` (indices of earlier subtasks; order checked in code).

Recipes (`sandman/recipes.py`): `rcp_research_compare_recommend`
(gather → detail per item → compare), `rcp_research_write` (research → write).

## Workers

One loop for all roles (`worker.run_worker`): up to N turns; each turn one
`worker_step` call returning exactly one action. The union schema contains
only this role's tools plus the allowed terminal actions:

| Terminal action | Fields | What the harness does |
|---|---|---|
| `finish` | role result (below) | → verifier |
| `split` | reason, 2–5 subtasks | children created, card waits, then a synthesize session |
| `block` | question, options, why | question to the owner, card blocked |
| `checkpoint` | progress, next_step, facts | card done; a continuation card continues |
| `fail` | category (tool_error/impossible/out_of_scope/unclear), reason | retry, larger model, or ask the owner |

Restrictions in the schema, not the prompt: no `split` at max depth or in the
synthesize phase; on the last turn no tools at all ("This is your final step").

**Context pack** (in this order): task, goal, constraints, done_when, owner
profile, what memory knows (notes with claims), inputs (results of
dependencies or children, ≤1500 chars each), comments (verifier feedback,
owner answers, `add_to_card` wishes), the transcript so far (last tool result
up to 3000 chars, older ones 1000), then the footer:

```
Step {k} of {n}. Choose exactly one action.
- Use a tool if you need more information or need to save work.
- finish if every "done when" item is satisfied.
- split only if the task clearly needs 2-5 separate pieces of work.
- block if you cannot continue without the owner's input.
- checkpoint if you are making progress but will run out of steps.
- fail if the task is impossible or out of scope.
```

**Harness guards:** a repeated identical tool call is not executed again ("You
already did exactly this in step k"); a long, highly repetitive string is
rejected and retried; long web pages are paged; files are written in a
separate plain-text call (below).

### research (12 turns)

```
Your role is the researcher.

You investigate a task to find reliable and factual information.
Use the web_search and web_fetch tools for this. If what memory knows
already answers the task and is recent, finish without searching.

Because others will only see your result, you need to cite your sources.
Put facts worth remembering for later into facts. A fact's subject is the
thing it is about ("GARDENA Micro-Drip starter set"), not a property ("Price").
```

Tools: `web_search(query)`, `web_fetch(url)` (pages over 2500 chars are saved;
the result starts with `[characters 0-2500 of N, read on with
read_artifact(...)]`), `read_artifact(art_id, offset)`.
No `write_artifact`: its results go into `finish` (the tool made it save files
instead of finishing).
Finish: `summary` (≤1000 chars), `sources[]`, `facts[]` (subject, claim,
source, volatility), `open_questions[]`.

### write (8 turns)

```
Your role is the writer.

You write the text the task asks for and save it with write_artifact.
Base it on your inputs and notes. Do not make up facts.
```

Tools: `read_artifact`, `write_artifact(name, what)`. Finish: `summary`,
`facts[]`, `open_questions[]`; the harness attaches the ids of written files.

### synthesize (6 turns)

```
Your role is the synthesizer.

Others worked on parts of this task. Their results are your inputs.
You combine them into one result for the task. Be honest about parts that
failed or are missing.
```

Runs when a split card's children are all finished; inputs are the
children's results (failed ones with their reason). Tools: `read_artifact`,
`write_artifact`. Finish: `summary`, `sources[]`, `facts[]`,
`recommendation`, `open_questions[]`.

### code (15 turns)

```
Your role is the coder.

You change the code in your work folder to complete the task.
All tools work inside that folder: use relative paths like "main.py".
Use run to check your work, for example by running the tests.
```

Tools: `read_file(path)`, `write_file(path, what)`, `run(command)` (cwd =
work folder, 60 s timeout, no container), `list_dir(path)`. Finish: `summary`,
`tests_passed`, `open_questions[]`. The weakest role (61%): it sometimes claims
`tests_passed` without running them; multi-file changes fail.

### Writing files: `write_content`

`write_file(path, what)` / `write_artifact(name, what)` only name and describe
the file. The harness then makes a second, **plain-text** call with the same
context and

```
Now write the full content of "{name}": {what}
Output only the content itself, nothing before or after it.
```

and saves the answer. Inside JSON strings the model lost newlines on all but
one provider.

## Verifier

Typed `done_when` criteria (`verifier.py`): "at least N sources/facts/files"
becomes a deterministic `min_items` check; `field_present`, `file_exists`,
`command_succeeds` are also code. Everything else is a **judge** criterion, one
call each, only after all deterministic checks pass:

```
Your role is the verifier.

You check one requirement of a finished task. Be strict: pass only if the
result clearly meets it.
---
Task: {title}
Requirement: {criterion}
Result: {result}

Does the result meet the requirement?
```

Output `{reason, verdict: pass|fail}`. Failures become a comment for the next
attempt (2 attempts, then a larger model if configured, then a question to the
owner).

## Consolidator (offline memory writing)

Per pending fact (`memory.consolidate`):

1. **subject → note**: exact title/alias match in code; else
   `match_subject` over the FTS top 3 ("a different thing of the same kind is
   none"); else a new note. Owner facts go to the profile note.
2. **`relevance_rubric`** (not for owner facts): reusable, costly,
   task_mechanics, trivial. **Keep rule in code:** not task_mechanics and not
   trivial and (reusable or costly).
3. **`consolidate_fact`** against the note's newest 5 claims: new / duplicate
   (corroboration +1) / update / contradicts / discard, with a target claim.
   Code policy: updates to evergreen claims or from weaker sources become
   *disputed* plus a review item for the owner; owner facts always win.
4. **`render_note`**: a one-liner for every changed note (used in catalogs and
   search).
