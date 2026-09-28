# Sandman v5 — Design Document

**A multi-agent harness for weak, slow local models**

| | |
|---|---|
| Status | Draft 1 of the v5 design — for implementation |
| Date | 2026-09-28 |
| Project | Sandman, iteration 5. Earlier iterations (v1–v4) were prototypes; the pain points they surfaced — cross-agent communication, multi-channel conversation, long-term memory — are the focus of this version. |
| Inspiration | Hermes Agent (Nous Research), in particular its Kanban multi-agent board |
| Target models | Heavily quantized small open models (e.g. a Qwen-class model at Q3/Q4), running locally via an OpenAI-compatible server (llama.cpp, vLLM, Ollama). Assume: small context (8–32k), slow generation (a few tokens/s to ~30 tokens/s), unreliable at long-horizon planning, unreliable at free-form JSON, decent at short classification and summarization. |

---

## 0. How to use this document (for the implementing agent)

- This document is **normative** where it says MUST / MUST NOT, and **advisory** where it says SHOULD / MAY. Where it says "suggested", treat it as a default you may replace with an equivalent.
- Every major decision is followed by a **Why** block. If you find a better implementation, keep the *reason* intact; the reason is the requirement, the mechanism is negotiable.
- Section 12 ("Rejected alternatives") lists designs that look attractive and MUST NOT be reintroduced without an explicit decision by the human.
- Build in the order of Section 15 (milestones). Each milestone has acceptance tests. Do not start memory (M5) before the board (M1–M2) is solid.
- When the document is ambiguous, prefer the option that (a) keeps control flow in code rather than in the model, and (b) asks the model a smaller question.
- Record every deviation from this document in `docs/DEVIATIONS.md` with a one-paragraph justification.

---

## 1. Problem statement

We want a personal, always-on agent system that:

1. Accepts requests from a human over several channels (Matrix, a web UI, later others), never confusing them, and can **initiate** messages (reminders, results, questions).
2. Breaks work into small, bounded units executed by independent agent sessions, coordinated through a **Kanban board**.
3. Accumulates **long-term memory** from everything the swarm learns, so future work benefits from past research without the model having to know what to ask for.
4. Works acceptably with **weak, slow models**. This is the dominant constraint and shapes every decision below.

Three problems have historically been hard in prototypes and are the focus of this design:

- **Cross-agent communication**: when/how an agent spins up subtasks, how results flow back.
- **Human communication**: multiple channels, multiple interleaved subjects in one channel, proactive outreach.
- **Long-term memory**: committing the right facts, and retrieving knowledge the agent doesn't know it needs.

### 1.1 Goals

- G1. A single human (the "owner") can hold several independent conversations ("topics") across any channel, and the system keeps them apart.
- G2. Any work item larger than one short session is decomposed into cards; each card is executed by a fresh session with a narrow scope, a small toolset, and a defined end state.
- G3. Results, questions and reminders are delivered to the right topic on the right channel, without the model choosing channels.
- G4. Knowledge produced by any card is available to future cards automatically, with provenance and freshness.
- G5. The system degrades gracefully: a wrong model output costs one retry, not a corrupted state.
- G6. Everything is inspectable: board, topics, memory and every LLM call are visible and editable by the human.

### 1.2 Non-goals (v1)

- Multi-user / multi-tenant operation (one owner; the data model allows more later).
- Distributed execution across machines. Single host, single SQLite database.
- Autonomous self-modification of the harness code.
- Voice, images, rich media (text only in v1; attachments stored as files and referenced).
- Competing with frontier-model agent quality. The goal is *reliable* over *clever*.

---

## 2. Design principles

These principles are the core of the design. Every later section is an application of them.

### P1. The harness is the brain; the model is a function

All control flow — what runs next, when a task is done, where a message goes, what gets remembered — is decided by deterministic code. The model is invoked as a **pure-ish function** with a narrow input and a constrained output.

> **Why:** Weak models are bad at remembering to do things across turns, and bad at tracking state. Anything the model must *remember to do* will eventually be skipped. Anything the model is *asked* at the right moment, as a narrow question, it usually gets right. Moving state and sequencing into code turns long-horizon reliability problems into many short, testable decisions.

### P2. One decision per call; prefer choosing over generating

Each LLM call answers exactly one question. Where possible the answer is a choice among enumerated options (yes/no, pick one of ≤6), not free-form generation.

> **Why:** Small quantized models are far more consistent at classification than at open generation, and consistency is testable. A call that asks "classify the topic AND draft a reply" fails in combinatorially more ways than two calls. Choosing from a list also solves the "unknown unknowns" problem: the model can *recognize* the relevant item even when it could not have *named* it.

### P3. Constrained decoding everywhere

Every LLM output that the harness parses MUST be produced under a grammar / JSON schema (llama.cpp GBNF, `response_format: json_schema`, or equivalent). Free text only appears inside schema string fields.

> **Why:** Malformed JSON and invented tool names are the #1 failure mode of quantized models. Grammar-constrained decoding eliminates the entire class at near-zero cost. It also lets the harness *restrict* the option set dynamically (e.g. remove `split` when depth is exhausted), which is a much stronger control than a prompt instruction.

### P4. Small, role-scoped toolsets chosen by the harness

A session never sees more than ~3–7 tools. The toolset is chosen by the harness from the card's `role`, never by the model. There is no "tool search" tool.

> **Why:** Tool selection accuracy collapses as the tool count grows, and weak models collapse earliest. A tool-search meta-tool is a symptom that scope wasn't defined. If a task needs tools from two roles, it should be two cards.

### P5. The board is the only bus between agents

Agents never message each other. All inter-agent communication is card creation, card results, card comments and dependency edges, persisted in the database.

> **Why:** Direct agent chat requires both parties to hold context, wait, and interpret each other's free text — all weak-model weaknesses. A durable board makes every handoff explicit, inspectable, resumable after a crash, and editable by the human.

### P6. Sessions are short, fresh and disposable

No session waits. A parent that needs children's results ends its session; a *new* session is started later with the children's results as inputs. A session that runs out of budget checkpoints and a continuation card is queued.

> **Why:** Small contexts and slow generation make long sessions expensive and fragile (context overflow, drift, crash loss). Fresh sessions with curated inputs are cheaper, restartable and debuggable.

### P7. Route by topic, deliver by channel

Every human-facing message belongs to a **topic** (a subject). Channels are only delivery routes. The model never chooses a channel.

> **Why:** "Channel" and "subject" are orthogonal. One Matrix room carries many subjects; one subject may move from web to Matrix. Binding sessions to channels is what causes cross-talk.

### P8. Make assumptions visible and corrections cheap

Where the system guesses (topic routing, memory merges), the guess is shown to the human in a compact form and can be corrected with a one-word command.

> **Why:** A perfect classifier is not achievable with a weak model. A visible, cheaply corrected guess is nearly as good as a correct one; an invisible wrong guess silently corrupts state.

### P9. Memory is pushed, not pulled

The harness assembles relevant memory *before* a session starts. The model is never responsible for realizing it lacks knowledge and formulating a query. Inside a session, the model at most opens items from a short catalog it has been shown.

> **Why:** A model cannot ask about what it doesn't know exists. The harness can: it knows the card's goal, its project, its entities, and what's in the library.

### P10. Memory is written offline, by a dedicated process, with provenance

Workers *propose* candidate facts as part of their structured result. A separate consolidator decides what enters long-term memory, merges duplicates, resolves or flags contradictions, and records provenance and volatility.

> **Why:** Asking a worker to also curate memory mid-task splits its attention (bad for weak models) and produces duplicates and junk. Batch consolidation is cheaper (latency doesn't matter), more consistent (same prompt, same rubric), and auditable.

### P11. Log every LLM call for replay and evaluation

Every call is logged with call type, model profile, full input, raw output, parsed output, latency and outcome. Each call type has a golden test set.

> **Why:** With weak models, prompt and schema tuning is the main lever on quality. That requires replaying real inputs against changed prompts or models.

---

## 3. System overview

```
             ┌──────────────────── Conversation layer ────────────────────┐
 Matrix ─┐   │                                                            │
 Web UI ─┼─► │ Adapters ─► Inbox ─► Debouncer ─► Topic Router ─► Front Desk│
 Email* ─┘   │    ▲                                              │   ▲   │
             │    └──────────── Courier ◄──── Outbox ◄───────────┘   │   │
             └──────────────────────────────▲────────────────────────┼───┘
                                            │ results, questions,     │ create_card,
                                            │ reminders               │ board_status
             ┌──────────────────── Work layer ─────────────────────────┼───┐
             │  Scheduler ─► Board (cards, deps, events) ◄─────────────┘   │
             │                 │    ▲                                      │
             │           Dispatcher │ terminal actions                     │
             │                 ▼    │                                      │
             │  Triage · Librarian · Planner · Worker · Verifier sessions  │
             └───────────────────────────┬───────────────▲─────────────────┘
                              candidate  │               │ context packs
                              facts      ▼               │
             ┌──────────────────── Memory layer ──────────────────────────┐
             │  Fact queue ─► Consolidator ─► Notes/Claims ─► Retriever    │
             │  Episodic log (all messages, results, tool calls; FTS)      │
             └─────────────────────────────────────────────────────────────┘
             Cross-cutting: LLM Gateway (schemas, model profiles, call log),
                            Workspace/files, Web UI (board, topics, memory, calls)
```

### 3.1 Components (summary)

| Component | Layer | LLM? | Responsibility |
|---|---|---|---|
| Channel adapter | Conversation | No | Normalize platform events ↔ internal messages. One per platform. |
| Inbox + Debouncer | Conversation | No | Persist inbound messages; coalesce bursts. |
| Topic Router | Conversation | Partly | Assign each inbound message to a topic (cascade: deterministic first, LLM last). |
| Front Desk | Conversation | Yes | The only agent that converses with the human. One short session per topic turn. |
| Outbox | Conversation | No | Durable queue of outbound messages, addressed to topics. |
| Courier | Conversation | No | Resolve topic → channel binding, apply quiet hours/digests/dedupe, send. |
| Board | Work | No | Cards, dependencies, events, leases. The single source of truth for work. |
| Scheduler | Work | No | Turns schedules and due dates into cards. |
| Dispatcher | Work | No | State machine driver: picks runnable cards, runs the right session type, applies terminal actions. |
| Triage | Work | Yes | Decides whether a card fits one session, or needs planning. |
| Librarian | Work/Memory | Yes | Pre-flight: does memory already answer / narrow this card? |
| Planner | Work | Yes | Decomposes a card into subcards (recipe-first). |
| Worker | Work | Yes | Executes a card with a role toolset; must end with a terminal action. |
| Verifier | Work | Partly | Checks `done_when` criteria (deterministic checks first, LLM judge second). |
| Fact queue | Memory | No | Candidate facts from cards and conversations. |
| Consolidator | Memory | Yes | Offline merge of candidates into notes/claims. |
| Retriever | Memory | No* | Entity match, hybrid search, context pack assembly (*embedding model only). |
| Episodic log | Memory | No | Full-text searchable record of everything. |
| LLM Gateway | Cross-cutting | — | Single entry point for all model calls; schemas, profiles, logging, concurrency. |
| Web UI | Cross-cutting | No | Board, topics, memory browser, call log, chat. |

### 3.2 Suggested technology (replaceable)

- **Language:** Python 3.12, asyncio.
- **Storage:** SQLite in WAL mode (single file), FTS5 for full-text, `sqlite-vec` for vectors. All state in one DB; files in a workspace directory.
- **LLM access:** OpenAI-compatible HTTP API to a local server. llama.cpp server is preferred because it supports JSON-schema / GBNF constrained decoding reliably.
- **Embeddings:** a small local embedding model (e.g. a bge-small / nomic-embed class model) behind the same gateway.
- **Matrix:** `matrix-nio` (supports threads via `m.relates_to` / `rel_type: m.thread`, replies via `m.in_reply_to`).
- **Web:** FastAPI + server-sent events or WebSocket; a minimal front-end (HTMX or a small SPA).
- **Scheduling:** in-process scheduler (e.g. APScheduler) persisting to the DB, or a simple polling loop over a `schedules` table.

---

## 4. Core concepts and vocabulary

| Term | Definition |
|---|---|
| **Owner** | The human user. v1 has exactly one. |
| **Identity** | A platform account that maps to the owner (Matrix user ID, web session user). |
| **Binding** | A concrete delivery route: `(platform, room/chat id)`. A DM room, a web chat, etc. |
| **Topic** | A subject of conversation ("Garden irrigation", "Tax return 2026"). Has a short slug used as a label (`garden`). Owns messages, cards, and a rolling summary. |
| **Project** | An optional long-lived grouping of topics and cards that shares a memory brief. v1: a topic MAY be linked to one project; a project MAY have many topics. |
| **Card** | A unit of work on the board. |
| **Session** | One execution of one LLM-driven step for one card or topic turn. Sessions are ephemeral; their inputs and outputs are persisted. |
| **Role** | A named worker profile that defines a toolset, prompt preamble, and output schema (e.g. `research`, `write`, `code`, `synthesize`, `ops`). |
| **Terminal action** | The structured action that ends a worker session: `finish`, `split`, `block`, `checkpoint`, `fail`. |
| **Recipe** | A reusable decomposition template ("research → compare → recommend"). |
| **Note** | A long-term memory document about one entity, project, procedure, or the owner's profile. Contains claims. |
| **Claim** | One atomic statement inside a note, with provenance, date and volatility. |
| **Candidate fact** | A proposed claim emitted by a worker or the front desk, awaiting consolidation. |
| **Context pack** | The harness-assembled bundle of memory and inputs injected into a session. |
| **Call type** | A named kind of LLM call with a fixed prompt template and output schema (see §10). |

### 4.1 Identifier conventions

All IDs are ULIDs with a type prefix: `crd_` card, `top_` topic, `prj_` project, `msg_` message, `out_` outbox item, `qst_` question, `not_` note, `clm_` claim, `fct_` candidate fact, `ses_` session, `cal_` LLM call, `bnd_` binding, `sch_` schedule, `art_` artifact.

> **Why:** Prefixed IDs make logs and prompts self-describing and prevent a model from confusing a card ID with a note ID when it has to reference one.

---

## 5. Work layer: board, cards and cross-agent communication

### 5.1 The card

A card is a **contract**. It contains everything a session needs, and a session receives nothing that is not derivable from its card.

```yaml
id:            crd_01J...
title:         "Find EU grant programs for small solar co-ops"
kind:          task            # task | plan | reminder | scheduled | refresh | system
role:          research        # selects toolset, preamble, output schema
goal:          "Identify currently open EU or Swiss funding programs ..."
done_when:                     # acceptance criteria, each typed (see §5.8)
  - {type: min_items, field: artifacts.programs, n: 3}
  - {type: judge, text: "Each program lists deadline and eligibility"}
  - {type: links_resolve, field: artifacts.programs[*].url}
inputs:                        # references, resolved by the harness into the context pack
  - note:not_01J...            # project brief
  - card:crd_01H....result     # a sibling's output
constraints:   ["Only programs open to co-ops with <50 members"]
budget:        {turns: 12, tokens_out: 6000, wall_minutes: 60}
depth:         1               # 0 = root card
parent_id:     crd_01H...
depends_on:    [crd_01H...]    # must be done before this card is runnable
origin_topic:  top_01J...      # where questions/results for the human go
project_id:    prj_01J...
state:         ready
phase:         execute         # execute | synthesize (after children complete)
attempt:       1
priority:      normal          # interactive | high | normal | background
created_by:    frontdesk       # frontdesk | planner | worker(split) | scheduler | human | system
```

> **Why a contract:** A weak model cannot recover missing context by "looking around". If the card is complete, any session — including a retry, a different model, or the human — can pick it up. It also makes cards the natural unit for evaluation.

### 5.2 Card kinds

| Kind | Executed by | Purpose |
|---|---|---|
| `task` | Worker session (after triage/librarian) | Normal work. |
| `plan` | Planner session | Decompose a parent that triage judged too big. |
| `reminder` | Harness only (no LLM) | At `due_at`, post a message to `origin_topic`. |
| `scheduled` | Harness creates a `task` from a template at each firing | Recurring work ("check X every Monday"). |
| `refresh` | Worker (role `research`) | Re-verify a stale memory note. Created by the librarian. |
| `system` | Harness jobs (consolidation, summaries) | Background maintenance; lowest priority. |

### 5.3 Card state machine

States: `new`, `ready`, `running`, `waiting`, `blocked`, `verifying`, `done`, `failed`, `cancelled`.

| From | Event | To | Notes |
|---|---|---|---|
| `new` | triage → `single` | `ready` | Librarian runs first (§7.6); may short-circuit to `done` or rewrite the goal. |
| `new` | triage → `split` | `ready` (as `kind=plan` child) | Harness creates a plan card as the only child; parent → `waiting`. |
| `ready` | dispatcher claims | `running` | Lease acquired (§5.10). |
| `running` | `finish` | `verifying` | |
| `running` | `split` | `waiting` | Children created in `new`. |
| `running` | `block` | `blocked` | Question sent via outbox (§6.8). |
| `running` | `checkpoint` | `done`* | *Card marked `done` with `result.kind=checkpoint`; a continuation card is created with the same goal, the progress note as input, `attempt` reset. |
| `running` | `fail` | `failed` or `ready` | Escalation policy (§5.9). |
| `running` | lease expired | `ready` | Crash recovery; `attempt` += 1. |
| `verifying` | all criteria pass | `done` | Candidate facts enqueued; parent notified. |
| `verifying` | a criterion fails | `ready` | Verifier feedback appended as comment; `attempt` += 1; escalation policy applies. |
| `waiting` | all children terminal | `ready` | `phase=synthesize`, role → `synthesize`, inputs = children results. |
| `blocked` | answer received | `ready` | Answer appended as comment; next session sees it. |
| any non-terminal | human cancels | `cancelled` | Children cancelled recursively. |

Terminal states: `done`, `failed`, `cancelled`. Every transition writes a row to `card_events`.

> **Why `checkpoint` ends the card instead of pausing it:** it keeps the invariant "one card = one bounded effort" and gives a natural place to snapshot progress into the board. Chains of continuation cards are visible on the board and capped (§5.11).

### 5.4 Pipeline for a new card

```
new ─► [Librarian] ─► answered? ──yes──► done (result from memory)
             │ narrowed goal / proceed
             ▼
        [Triage] ─► single ─► ready ─► [Worker] ─► terminal action ─► ...
             │
             └──► split ─► plan child ─► [Planner] ─► subcards (new) ─► ...
```

Librarian before triage, because a narrowed goal often turns a "too big" card into a single-session card.

### 5.5 Triage

**Call type `triage`.** Input: card title, goal, done_when, role toolset *names and one-line descriptions*, budget, and up to 6 candidate recipes (retrieved by keyword/vector match on the goal). Output schema:

```json
{ "fits_one_session": "yes | no | unsure",
  "recipe_id": "<one of the offered ids> | none",
  "missing_info": "<short question for the human> | null" }
```

Rules:
- `yes` → `ready`.
- `unsure` → `ready` (the worker can still `split` or `checkpoint`).
- `no` with a recipe → planner runs in *recipe mode*.
- `no` without recipe → planner runs in *free mode*.
- `missing_info` non-null → card goes to `blocked` with that question **before** any work starts.
- If `depth >= max_depth`, the harness MUST NOT offer `no`; the grammar only allows `yes|unsure`.

> **Why a separate triage call:** "Should I decompose?" is the decision weak models make worst when it's buried inside an execution loop — they either split everything or never split. As a standalone, three-way classification with a budget and a toolset in view, it's tractable and testable. Asking for missing information up-front avoids burning a 20-minute session on an under-specified task.

### 5.6 Planner

**Recipe mode** (preferred). A recipe is a parameterized template:

```yaml
id: rcp_research_compare_recommend
title: "Research options, compare, recommend"
params: [subject, criteria, max_options]
steps:
  - {key: gather,   role: research,   title: "Find candidate {subject}",       done_when: [...]}
  - {key: detail,   role: research,   title: "Detail each candidate",          depends_on: [gather], fanout: "per_item(gather.artifacts.items, max={max_options})"}
  - {key: compare,  role: synthesize, title: "Compare against {criteria}",      depends_on: [detail]}
```

The model only fills `params` (call type `plan_fill`, schema derived from the recipe's params). Fan-out over a previous step's output is performed by the harness when that step finishes.

**Free mode.** Call type `plan_generate`. Output: 2–5 subtasks, each `{title, goal, role (enum of roles), done_when[] (strings), depends_on[] (indices)}`. Constraints enforced by schema: max 5 items, role enum, indices < own index (DAG).

After the planner, the parent card enters `waiting` with `depends_on` = the new children. When all children reach a terminal state, the parent becomes `ready` with `phase=synthesize` (§5.7).

> **Why recipes first:** Choosing a proven template and filling slots is a classification + extraction problem; inventing a plan is a long-horizon generation problem. The former is what weak models are good at. Recipes are also where accumulated *procedural* knowledge lives (§7.9): successful free-mode plans can be promoted to recipes by the human.

### 5.7 Worker sessions

A worker session is a loop of up to `budget.turns` model calls. Each call (call type `worker_step`) returns **exactly one** of:

- a tool call from the role's toolset, or
- a terminal action.

The union schema is generated per session from the role's tools plus the *allowed* terminal actions.

```json
{ "oneOf": [
  {"action": "tool", "name": "<enum of role tools>", "args": { ... per-tool schema ... }},
  {"action": "finish",     "result": { ... role output schema ... }},
  {"action": "split",      "reason": "...", "subtasks": [ ... max 5 ... ]},
  {"action": "block",      "question": "...", "options": ["..."], "why": "..."},
  {"action": "checkpoint", "progress": "...", "next_step": "...", "facts": [ ... ]},
  {"action": "fail",       "category": "tool_error|impossible|out_of_scope|unclear", "reason": "..."}
]}
```

Dynamic restrictions (enforced in the grammar, not the prompt):
- `split` removed when `depth >= max_depth` or `kind=plan` children already exist for this attempt, or `phase=synthesize`.
- On the **last allowed turn**, `tool` is removed: the model MUST pick a terminal action. On the last turn the prompt also says "This is your final step."
- `block` removed for `priority=background` cards when quiet hours are active (they `checkpoint` instead).

**Session context (the "context pack"), in this order, each with a token budget:**

1. Role preamble (fixed, short; ≤300 tokens).
2. Card contract: title, goal, constraints, done_when (≤400).
3. Owner profile note (≤300).
4. Project brief (≤600).
5. Memory catalog: top-k note titles + one-line summaries (≤400), with `open_note` available.
6. Inputs: parent/sibling results (summaries only; artifacts by reference) (≤1500).
7. Comments: verifier feedback, human answers (≤500).
8. Tool-call transcript of this session so far (remaining budget; older tool results truncated to their first N tokens with a `[truncated, use read_artifact]` marker).
9. Turn footer: "Step k of N. Choose one action."

The harness, not the model, truncates. Exact budgets are config values scaled to the model's context size.

> **Why a single-action-per-call loop:** It gives the harness a checkpoint after every step (can stop, log, enforce budget, restrict actions). It also keeps each generation short, which on a slow model means faster feedback and less wasted compute when something goes wrong.

**Roles and toolsets (initial set; each ≤7 tools):**

| Role | Tools | Output schema highlights |
|---|---|---|
| `research` | `web_search`, `web_fetch`, `open_note`, `read_artifact`, `write_artifact` | `summary`, `artifacts[]`, `facts[]`, `open_questions[]`, `sources[]` |
| `write` | `read_artifact`, `write_artifact`, `open_note` | `summary`, `artifacts[]` |
| `synthesize` | `read_artifact`, `open_note`, `write_artifact` | `summary`, `artifacts[]`, `facts[]`, `recommendation?` |
| `code` | `read_file`, `write_file`, `run` (sandboxed), `list_dir` | `summary`, `artifacts[]`, `tests_passed` |
| `ops` | per-integration tools, all side-effecting tools require approval (§11) | `summary`, `actions_taken[]` |

Every role output schema MUST include `summary` (≤150 words, enforced by `maxLength`) and SHOULD include `facts[]`:

```json
"facts": [{ "subject": "<entity name>", "claim": "<one sentence>",
            "source": "<url | card | owner>", "volatility": "evergreen|slow|volatile" }]
```

> **Why every result has a bounded `summary`:** results are consumed by parents, the front desk and the consolidator — all with small contexts. A hard length cap on the one field everyone reads keeps the whole graph within budget. Full detail lives in artifacts, fetched only on demand.

### 5.8 Verification

`done_when` criteria are typed:

- **Deterministic:** `min_items(field, n)`, `field_present(field)`, `file_exists(path)`, `links_resolve(field)`, `command_succeeds(cmd)`, `schema_valid(artifact, schema)`.
- **Judge:** `{type: judge, text: "..."}`. One call per criterion (call type `verify_criterion`), output `{"pass": bool, "reason": "<≤30 words>"}`, given only the criterion, the result summary, and (if referenced) the relevant artifact excerpt.

Free-text `done_when` strings produced by the front desk or the planner are classified into types by the harness where a pattern matches (e.g. "at least 3 X" → `min_items`); otherwise they become `judge`.

Deterministic checks run first; judges run only if all deterministic checks pass.

> **Why one judge call per criterion:** "Does this satisfy all of these criteria?" invites a lazy global yes. Per-criterion yes/no is much more reliable and its failures are specific enough to feed back to the next attempt.

### 5.9 Retry and escalation

On `fail`, verification failure, or lease expiry:

1. `attempt < max_attempts` (default 2): back to `ready` with feedback comment.
2. If a `large` model profile is configured: one attempt with the large profile.
3. Otherwise: `block` with an auto-generated question to the owner ("Card *X* failed twice: <reason>. Retry / cancel / give guidance?"), options rendered as quick replies where the platform supports it.

`fail(category=out_of_scope|unclear)` skips step 1 and goes straight to blocking, since retrying won't help.

### 5.10 Dispatcher, leases and concurrency

- The dispatcher loops: pick the highest-priority runnable card (`ready`, dependencies satisfied), acquire a lease (`lease_owner`, `lease_expires_at`), run the appropriate session type, apply the resulting transition in **one DB transaction**.
- Leases are renewed after each worker step. Expired leases are reclaimed on startup and periodically.
- **Model slots.** The LLM gateway exposes N concurrent slots per model profile (config; with a slow local model typically 1–2). Priorities: `interactive` (front desk, router) > `high` (unblocked cards, synthesize) > `normal` > `background` (consolidator, refresh).
- **Interactive reservation.** One slot SHOULD be reserved for interactive calls, or a separate small/fast model profile SHOULD serve router + front desk. A human message MUST NOT wait behind a 20-minute worker session.

> **Why:** With a slow model, perceived responsiveness is dominated by queueing, not generation speed. The owner tolerates a research card taking an hour; they don't tolerate a "got it" taking an hour.

### 5.11 Hard limits (config defaults)

| Limit | Default | Rationale |
|---|---|---|
| `max_depth` | 3 | Deeper trees lose the plot; synthesis quality degrades per level. |
| `max_children_per_split` | 5 | Keeps synthesis input within context. |
| `max_attempts` | 2 | Then escalate; blind retries waste slow compute. |
| `max_continuations` | 4 | Checkpoint chains longer than this usually mean the card is mis-scoped → block to human. |
| `max_open_cards_per_root` | 25 | Guards against card explosion. |
| `max_turns` per role | research 12, write 8, synthesize 6, code 15 | Tune from logs. |

### 5.12 How results flow back

- Child `done` → its result is stored on the card; the parent's `depends_on` check re-evaluates.
- Parent's synthesize session receives each child's `{title, state, summary, artifact refs, open_questions}` — never child transcripts.
- Failed/cancelled children are included with their reason, so synthesis can report partial results honestly.
- A **root** card (`depth=0`, created by the front desk) reports to its `origin_topic` via the outbox when it reaches a terminal state (§6.9).

### 5.13 Comments

Any actor (human, verifier, front desk, harness) can append a comment to a card. Comments are included in the next session's context pack (slot 7). This is how guidance, answers to `block` questions, and verifier feedback reach a card without any agent-to-agent messaging.

---

## 6. Conversation layer: talking to the human

### 6.1 Three separate identifiers

| Concept | Example | Owned by |
|---|---|---|
| Owner (person) | "the owner" | Identity table maps platform accounts → owner |
| Binding (route) | Matrix DM room `!abc:server`, web chat session | Adapter |
| Topic (subject) | `garden` — "Garden irrigation" | Router / front desk |

A binding carries many topics; a topic may be active on several bindings over time. Every message row has both `binding_id` and `topic_id`.

> **Why:** Conflating route and subject is the root cause of channel confusion. Separating them turns "don't confuse channels" into a solved delivery problem (courier) and "keep subjects apart" into a classification problem (router) — two different problems with different tools.

### 6.2 Channel adapter interface

```python
class Adapter(Protocol):
    platform: str
    capabilities: Capabilities   # threads, replies, reactions, edits, quick_replies, markdown

    async def events(self) -> AsyncIterator[InboundEvent]: ...
    async def send(self, binding: Binding, body: str,
                   thread_ref: str | None = None,
                   reply_to: str | None = None,
                   quick_replies: list[str] | None = None) -> str: ...   # returns platform_msg_id
```

`InboundEvent` fields: `platform`, `platform_user_id`, `binding_ref`, `platform_msg_id`, `text`, `thread_ref` (platform thread root, if any), `reply_to_ref` (platform message replied to, if any), `reaction` (if the event is a reaction), `timestamp`, `attachments[]`.

Adapters MUST NOT contain business logic. They translate. Unknown senders are dropped (single-owner system) and logged.

**Matrix specifics.** Use `m.thread` relations: the courier SHOULD open a Matrix thread per topic in a room (thread root message = `[slug] Topic title`), and the router maps `(binding, thread_ref) → topic` deterministically. Replies (`m.in_reply_to`) map to the replied message's topic/question. Reactions MAY be used for quick answers (✅/❌) on questions.

**Web UI specifics.** The web chat shows a topic sidebar; the composer has a topic selector defaulting to the last active topic plus "New topic". Messages sent with an explicit topic skip routing.

### 6.3 Inbound pipeline

```
Adapter event → persist to inbox (raw) → identity check → debounce → route → front desk
```

**Debounce.** Messages from the same binding are coalesced while new ones keep arriving within `debounce_seconds` (default 30, max window 120). The coalesced group is routed and handled as one unit, unless the messages carry *different* explicit topic signals (then they are split by signal).

> **Why:** People type in bursts ("hey" / "about the garden" / "can you check drip vs sprinkler"). Handling each fragment separately with a slow model wastes minutes and produces three half-answers.

### 6.4 Topic router (cascade)

The router tries each stage in order and stops at the first decisive result. Every routed message records `routed_by` (stage) and `route_confidence`.

1. **Explicit command.** `/t <slug>`, `#<slug>` prefix, `/new <title>`. Decisive.
2. **Platform structure.** `thread_ref` mapped to a topic; `reply_to_ref` pointing at a message with a topic. Decisive.
3. **Question answer.** `reply_to_ref` pointing at an outbound *question* → route to the question's topic and attach as answer (§6.8). Also: if exactly one question is pending on this binding and the message is short and matches one of its `options`, treat as its answer (confidence: medium; the front desk confirms in its reply).
4. **Web UI selector.** Topic chosen in the composer. Decisive.
5. **Sticky check** (LLM, call type `route_sticky`). If the binding's last message (either direction) was < `sticky_minutes` (default 20) ago, ask: "Recent topic: *<title>* (last two lines). New message: <text>. Is the new message about the same topic?" → `{"same": "yes|no|unsure"}`. `yes` → decisive.
6. **Shortlist** (LLM, call type `route_shortlist`). Candidates: up to 5 topics ranked by recency and lexical/vector similarity to the message, plus `new`. Each shown as `slug — title — last line`. Output `{"choice": "<slug>|new", "confidence": "high|low"}` (choice constrained to the enum).
7. **Low confidence handling.** If `confidence=low` or stage 5 returned `unsure` and stage 6 disagrees with the sticky topic: route to the best guess **and mark it provisional**. The front desk reply is prefixed `[slug?]` and ends with a one-line correction hint the first few times ("Wrong topic? reply `→ other-slug` or `→ new`").

For `new`, the router calls `topic_title` (short generation, ≤6 words) and derives a unique slug.

**Corrections.** `→ <slug>` / `→ new` replying to (or directly following) a message moves the message and the resulting front desk turn to the right topic; any card created from that turn is re-parented (`origin_topic` updated). Corrections are stored as training examples for `route_*` golden sets.

> **Why a cascade:** most messages can be routed without the model at all (threads, replies, commands, selectors). The LLM only handles the genuinely ambiguous residue, and even then it answers a yes/no or picks from ≤6 options. Visible provisional labels (P8) cap the cost of errors.

**Topic lifecycle.** `active` → `dormant` after `dormant_days` (default 14) without activity → `archived` on request. Dormant topics are still candidates in stage 6 but ranked lower. A topic with open cards never goes dormant.

### 6.5 Front desk

The front desk is the **only** agent that converses with the owner. It runs one short session per routed inbound unit (and for certain outbound events, §6.9). It is stateless between turns; continuity comes from the topic record.

**Context pack:**
1. Front desk preamble (tone, rules, owner name) (≤300 tokens).
2. Owner profile note (≤300).
3. Topic: title, rolling summary (≤400), project brief if linked (≤400).
4. Last N messages in this topic (default 8, truncated) (≤1200).
5. Open cards for this topic: `id — title — state — one-line status` (≤400).
6. Pending questions for this topic (≤200).
7. Memory catalog for the message (titles + one-liners) (≤300).
8. The new message(s).

**Tools (≤7):**

| Tool | Effect |
|---|---|
| `reply(text)` | Queue an outbound message to this topic. **Terminal.** |
| `no_reply()` | End without replying (e.g. owner said "thanks"). **Terminal.** |
| `create_card(title, goal, done_when[], role?, priority?)` | Create a root card with `origin_topic` = this topic. Returns card id. |
| `board_status(card_id?)` | Status of this topic's cards (or one card). |
| `open_note(note_id)` | Read a note from the catalog. |
| `remind(when, text)` | Create a `reminder` card. `when` is parsed by the harness (natural language → datetime), the parsed time is echoed in the reply. |
| `answer_question(question_id, answer)` | Deliver the owner's answer to a blocked card (used when routing stage 3 was not decisive). |

Max 4 steps per front desk session; the last step allows only `reply`/`no_reply`.

**Acknowledge fast.** When `create_card` is called, the harness MAY immediately send a template acknowledgement ("[garden] On it — card *Find drip irrigation options* created.") without waiting for the model's `reply`, if the front desk session is expected to exceed `ack_after_seconds` (default 20).

**Rolling topic summary.** After each front desk turn (batched when the model is busy), a `summarize_topic` call updates the summary from `{old summary, new messages}` into ≤150 words. Every `summary_rebuild_every` (default 20) messages the summary is rebuilt from the full recent history rather than incrementally.

> **Why a single front desk:** workers with different roles and partial context would produce inconsistent voices and could leak internal details; the owner would have to manage many correspondents. One stateless desk per topic with a strong summary gives consistency without a long-running session.
>
> **Why incremental-then-rebuild summaries:** incremental summarization drifts (summaries of summaries); periodic rebuilds from source reset the drift.

### 6.6 Outbox

All outbound messages — replies, results, questions, reminders, alerts — are rows in the outbox, addressed to a **topic**, never to a channel.

```yaml
id: out_...
topic_id: top_...
kind: reply | result | question | reminder | digest | alert | ack
priority: urgent | normal | low
body: "..."
question_id: qst_...          # when kind=question
card_id: crd_...              # provenance
dedupe_key: "reminder:crd_...:2026-09-29"
not_before: 2026-09-29T07:00  # quiet hours / scheduling
status: pending | sent | failed | merged_into_digest
sent_binding_id, sent_platform_msg_id, sent_at
```

### 6.7 Courier: choosing the channel

Deterministic rules, in order:

1. If the outbox item is a reply to a specific inbound message → that message's binding (and thread).
2. Else the topic's `last_inbound_binding` (where the owner last spoke about this topic), if active within `binding_affinity_days` (default 7).
3. Else the owner's `default_binding_for[priority]` (config, e.g. urgent → Matrix DM, low → web UI inbox/digest).

Then:
- **Quiet hours** (config): `normal`/`low` items get `not_before` = end of quiet hours; `urgent` bypasses.
- **Digest:** `low` items accumulate and are sent as one digest message at configured times, grouped by topic.
- **Dedupe:** same `dedupe_key` within the window → dropped.
- **Rate limit:** max M messages per binding per 10 minutes; excess merged into a digest.
- **Labeling:** body is prefixed with `[slug]` unless the message goes into a platform thread that is already bound 1:1 to the topic.
- **Threads:** on thread-capable platforms, post into the topic's thread; create it if missing and store the mapping.

> **Why deterministic:** channel choice is policy, not judgment. Making it code means it is predictable, configurable, and never confused by a model.

### 6.8 Questions from cards (human-in-the-loop)

When a worker calls `block(question, options)`:

1. Harness creates `qst_` with `card_id`, `topic_id = card.origin_topic` (or nearest ancestor's), `text`, `options`, `status=open`.
2. Outbox item `kind=question`, body rendered as: `[slug] Card "<title>" needs input: <question>` + options (as quick replies where supported).
3. The owner's answer arrives via router stage 3 (reply-to) or via the front desk tool `answer_question`.
4. Harness appends the answer as a card comment, sets `qst_.status=answered`, moves the card `blocked → ready`.
5. Unanswered questions are re-surfaced in the next digest after `question_nag_hours` (default 24), max 2 times; then the card stays `blocked` and is listed in the web UI.

Multiple open questions on the same binding MUST each carry a short handle (`Q3`) so the owner can answer "Q3: yes" unambiguously on platforms without reply support.

### 6.9 Proactive messages

The system initiates contact only through the outbox. Sources:

| Source | Mechanism |
|---|---|
| Reminder | `reminder` card fires at `due_at` → outbox `kind=reminder` (template, no LLM). |
| Scheduled work | `schedules` row fires → creates a `task` card from a template with `origin_topic`; its result reports like any root card. |
| Root card finished/failed | Outbox `kind=result`. `report_mode` config: `template` (default: title + summary + artifact links) or `frontdesk` (a front desk session phrases it in topic context — nicer, but costs a model call). |
| Card blocked | Outbox `kind=question`. |
| Memory review | Consolidator contradictions needing a human decision → `low` priority digest items (§7.5). |
| System alerts | Model server down, repeated failures → `urgent` alert to default binding. |

The owner can create reminders and schedules via the front desk ("remind me Friday to…", "every Monday check…"); the front desk tool parses them into `reminder` cards or `schedules` rows.

### 6.10 Commands (all platforms)

| Command | Effect |
|---|---|
| `/t <slug>` or `#slug …` | Route this message to topic. |
| `/new <title>` | Create topic and route there. |
| `→ <slug>` / `→ new` | Re-route the previous exchange. |
| `/topics` | List active topics with slugs. |
| `/status [slug]` | Board summary for topic (no LLM). |
| `/cancel <card>` | Cancel a card tree. |
| `/quiet <duration>` | Suppress non-urgent outbound. |
| `/forget <note or claim>` | Mark memory as retracted (§7.10). |

Commands are handled by the harness without an LLM call.

---

## 7. Memory layer

### 7.1 The two hard questions, reframed

**"How does an LLM know to ask for what it doesn't know?"** — It doesn't, and it shouldn't have to. The harness knows the card's goal, role, project, and the entities it mentions; it also knows what the library contains. So the harness retrieves *before* the session (push), and inside the session the model only chooses among catalog entries it is shown (recognition, not recall).

**"How are memories committed, and which facts are relevant?"** — Workers only *propose* candidate facts in a structured field they fill anyway. A dedicated, offline consolidator decides, using a fixed rubric of binary questions, with the existing notes in view. Everything carries provenance and a volatility class so relevance can be re-evaluated later.

### 7.2 Memory tiers

| Tier | Content | Written by | Read by | Injected by default? |
|---|---|---|---|---|
| **Working** | Card contracts, results, comments, artifacts | Harness/workers | Dispatcher, sessions | Yes, per card inputs |
| **Episodic log** | Every message, card result, tool call, LLM call (FTS indexed) | Harness | Consolidator, human, `search_log` (rare) | No |
| **Semantic notes** | Entity notes, project briefs, topic notes, negative results | Consolidator (+ human edits) | Retriever | Catalog + selected notes |
| **Profile** | Owner preferences, constraints, standing decisions | Consolidator from owner-sourced facts only (+ human edits) | Every front desk and worker session | Yes, always (≤300 tokens) |
| **Procedural** | Recipes; role preambles | Human (promotion from successful plans) | Triage/planner | Recipe shortlist |

### 7.3 Notes and claims

A **note** is about one thing. Kinds:

- `entity` — a thing in the world: a product, organization, person (public), place, concept, API. Has `aliases[]`.
- `project_brief` — the living README of a project: goal, status, decisions, open questions, key entity links.
- `topic` — for topics without a project: condensed history and decisions.
- `negative` — "searched for X on date D using approach A, found nothing useful / approach failed because …".
- `profile` — the owner's profile (exactly one in v1).

Notes are **rendered** from claims; claims are the unit of storage and merging.

```yaml
note:
  id: not_...
  kind: entity
  title: "Drip irrigation kits"
  aliases: ["drip kit", "micro irrigation"]
  one_liner: "Consumer drip kits compared for a 40 m² raised-bed garden."   # ≤20 words, used in catalogs
  project_id: prj_...        # optional scoping
  tags: [garden, irrigation]
  updated_at: ...

claim:
  id: clm_...
  note_id: not_...
  text: "Gardena Micro-Drip starter set covers ~15 m² per kit."
  source: {type: url, ref: "https://...", card_id: crd_...}   # or {type: owner, msg_id} / {type: card}
  observed_at: 2026-09-20
  volatility: slow            # evergreen | slow | volatile
  status: active              # active | superseded | disputed | retracted
  superseded_by: clm_...      # when status=superseded
  confidence: medium          # low | medium | high (from source type + corroboration)
```

The rendered note body lists active claims grouped by a small set of headings per kind, plus a "History" section listing superseded claims (collapsed in UI, excluded from context packs by default).

> **Why entity-shaped notes instead of a pool of text chunks:** chunk-level vector retrieval returns fragments without context, duplicates the same fact many times, and has no place for "this is outdated". Entity notes give (a) a natural merge target for new facts, (b) exact-name lookup, which is far more reliable than similarity search for a weak model's queries, and (c) a readable, editable artifact for the human.
>
> **Why claims under notes:** merging, superseding and provenance all need an atomic unit smaller than a document and larger than a token span.

### 7.4 Writing: candidate facts

Sources of candidate facts (`fct_` rows, status `pending`):

1. Worker results: the `facts[]` field (§5.7).
2. Checkpoints: `facts[]` in the checkpoint action.
3. Front desk / topic summarization: a `extract_owner_facts` call runs with each summary update, extracting **only statements made by the owner** about preferences, decisions, constraints and personal context. These are tagged `source.type=owner`.
4. Negative results: when a card finishes with empty or failed research (`fail(impossible)` or a `summary` flagged `nothing_found: true`), the harness auto-creates a candidate `negative` fact from the card goal and reason.
5. Human edits in the web UI (bypass consolidation; applied directly, logged).

### 7.5 The consolidator

Runs as `system` cards at `background` priority: nightly by default, and opportunistically when the model is idle and the queue exceeds `consolidate_batch_size`.

For each pending candidate:

```
1. resolve subject:
     exact/alias match on note titles  → target note
     else vector top-3 notes            → call `match_subject` (choose one or "none")
     else                                → target = new entity note (title from subject)
2. relevance rubric (skip for source=owner: always relevant):
     call `relevance_rubric` → 5 booleans (below)
     discard if rubric says so; record reason
3. merge decision:
     retrieve the target note's active claims most similar to the candidate (top 5)
     call `consolidate_fact` → {decision, target_claim_id?}
        new        → insert claim
        duplicate  → add source to existing claim (corroboration; confidence may rise)
        update     → insert claim, mark target superseded (only if candidate is newer)
        contradicts→ both kept, both status=disputed, create review item
        discard    → drop (logged)
4. if a project brief's linked entities changed → mark brief dirty
5. regenerate dirty briefs and notes' one_liners (call `render_brief`, from claims — never from the previous brief text)
```

**Relevance rubric** (call type `relevance_rubric`), all yes/no:

| Q | Keep if |
|---|---|
| `reusable`: Could a *different* future task plausibly need this? | yes |
| `costly`: Would re-deriving it take real effort (research, owner input)? | yes |
| `durable`: Will it likely still be true in a week? | yes, unless `volatility=volatile` and `costly` |
| `task_mechanics`: Is it only about how this particular task was executed (e.g. "I used the search tool")? | no |
| `trivial`: Is it common knowledge any model knows? | no |

Rule: keep if `not task_mechanics and not trivial and (reusable or costly)`. The rule is code; only the booleans come from the model.

**Contradictions.** Policy by volatility:
- `volatile` or `slow` with a newer `observed_at` and a source of equal or better type → `update` (newer wins, old superseded) automatically.
- `evergreen`, or sources of different types (e.g. owner vs web) → `disputed`, and a review item goes into the owner's low-priority digest: "Memory conflict on *Drip irrigation kits*: A (card X, Sept 20) vs B (card Y, Sept 27). Keep A / keep B / keep both?".
- Owner-sourced claims always win over web-sourced claims about the owner's own preferences.

> **Why offline and batched:** latency doesn't matter for consolidation, so the slow model is fine here, and batching lets the consolidator see several candidates about the same subject together. It is also the single best place to spend a stronger model if one is ever available intermittently (config: `consolidator_profile`).
>
> **Why binary rubric questions:** "rate importance 1–10" produces noise from small models; five yes/no questions produce stable, auditable decisions, and the keep-rule can be tuned in code without touching prompts.

### 7.6 Reading: the librarian (pre-flight)

Runs for every new `task` card before triage (skippable per role via config, e.g. `code` cards may skip).

1. **Entity extraction** (call type `extract_entities`): up to 6 entity names from title + goal. Constrained output: list of short strings.
2. **Exact/alias lookup** of each entity → entity notes (deterministic).
3. **Project brief** of `card.project_id` (deterministic).
4. **Hybrid search**: FTS5 (BM25) + vector similarity over notes' `title + aliases + one_liner`, restricted to the project first, then global; merged with reciprocal rank fusion. Top `k` (default 6) not already found.
5. **Negative notes** matching the goal are always included if they score above threshold.
6. **Decision** (call type `librarian`): given the card goal, done_when, and the retrieved notes' *one-liners plus active claims* (budget-truncated), output:

```json
{ "decision": "answered | narrow | proceed",
  "answer_note_ids": ["not_..."],          // when answered
  "narrowed_goal": "…",                     // when narrow; must be shorter than or equal to the goal
  "stale_note_ids": ["not_..."] }           // notes the card depends on that the model flags as possibly outdated
```

- `answered` → the harness builds a result from the referenced notes (summary via `render_answer`), marks the card `done` with `result.source=memory`, and the card still passes through the verifier. If the verifier fails, the card proceeds normally with the memory result as an input.
- `narrow` → goal replaced; original goal kept in `card.original_goal`; a comment records "Narrowed by librarian using not_x, not_y".
- `proceed` → normal.
- **Staleness:** claims past their volatility age (`volatile` 7 d, `slow` 180 d, `evergreen` ∞; config) are shown with an `[as of <date>]` marker. If a note the card depends on is stale, the librarian creates a `refresh` card as a dependency (at most one refresh per note per `refresh_cooldown`).

The retrieved set becomes slot 5 (catalog) of the worker's context pack; the notes judged most relevant (`answer_note_ids`, or top 2) are inlined in full within budget.

> **Why a librarian step at all:** with a slow model, the most valuable optimization is not doing work. A research card that would take 20 minutes becomes a lookup, or shrinks to "only check what changed since Sept 20". It also means the swarm gets visibly smarter over time, which is the point of memory.
>
> **Why entity extraction + exact match first:** names are the most reliable retrieval key. Similarity search is a fallback for things the goal describes without naming.

### 7.7 In-session memory access

Workers and the front desk get exactly one memory tool: `open_note(note_id)` — restricted to IDs present in their catalog. Roles MAY additionally get `search_notes(query)` (returns ≤5 catalog lines) if evaluation shows it helps; it is off by default.

Workers never write memory directly; they put facts into `facts[]`.

### 7.8 Project briefs

Each project has one `project_brief` note regenerated (never incrementally edited) from: the project's entity notes' one-liners, owner-sourced decisions, root card results (summaries), and open questions. Sections: *Goal*, *Current status*, *Decisions (with dates)*, *Key facts*, *Open questions*, *Things that didn't work*. ≤600 tokens.

Every card with a `project_id` gets the brief in its context pack. This alone solves most "unknown unknowns" inside a project.

### 7.9 Procedural memory: recipes

- Recipes live in a `recipes` table (YAML body) and are human-curated in v1.
- When a free-mode plan's root card finishes `done` with all children `done`, the harness offers (low-priority digest) to promote it: "The plan for *X* worked. Save as recipe?" On acceptance, a `generalize_recipe` call proposes params and titles; the human edits and saves in the web UI.
- Triage retrieves recipes by FTS/vector match on the goal (§5.5).

### 7.10 Forgetting and correction

- `/forget` or the web UI marks a claim or note `retracted`. Retracted claims are never injected and never re-created from the same source (the consolidator checks `retracted` sources).
- Owner corrections in conversation ("no, I moved to Basel") become owner-sourced facts that supersede older claims.
- Nothing is hard-deleted by the system; the human can purge from the UI.

### 7.11 Episodic log

Every message, card event, result, tool call and LLM call is stored and FTS-indexed. It is the ground truth from which notes can be rebuilt (a `rebuild_memory` maintenance command replays candidate facts). Workers do not see it by default; a `search_log` tool MAY be granted to `research`/`synthesize` roles later if evaluation shows value.

---

## 8. Workspace and artifacts

- Each root card tree gets a workspace directory `workspace/<root_card_id>/`; each card writes to `workspace/<root>/<card_id>/`.
- Artifacts are registered in the `artifacts` table (`art_`, path, mime, size, card_id, summary ≤30 words). Results reference artifacts by ID; the context pack shows `art_id — filename — summary`, and `read_artifact(art_id, offset?, length?)` fetches content in bounded windows.
- The `code` role's `run` tool executes in a sandbox (container or bubblewrap/firejail) with the card's directory mounted, no network by default, CPU/time limits.

> **Why artifacts by reference:** passing full documents between cards blows small context windows; a 30-word summary plus an on-demand reader lets the consumer decide what it needs.

---

## 9. Data model (SQLite)

Suggested schema. Types are SQLite affinities; JSON columns hold validated JSON. All tables have `created_at`, and mutable ones `updated_at` (omitted below for brevity). Every state change on cards goes through a single function that writes `cards` and `card_events` in one transaction.

```sql
-- Conversation
CREATE TABLE identities (id TEXT PRIMARY KEY, platform TEXT, platform_user_id TEXT, owner_id TEXT,
                         UNIQUE(platform, platform_user_id));
CREATE TABLE bindings   (id TEXT PRIMARY KEY, platform TEXT, ref TEXT, kind TEXT /*dm|group|web*/,
                         owner_id TEXT, active INTEGER, UNIQUE(platform, ref));
CREATE TABLE projects   (id TEXT PRIMARY KEY, title TEXT, status TEXT, brief_note_id TEXT);
CREATE TABLE topics     (id TEXT PRIMARY KEY, slug TEXT UNIQUE, title TEXT, status TEXT /*active|dormant|archived*/,
                         project_id TEXT, summary TEXT, summary_msg_count INTEGER,
                         last_activity_at TEXT, last_inbound_binding_id TEXT);
CREATE TABLE topic_threads (binding_id TEXT, thread_ref TEXT, topic_id TEXT, PRIMARY KEY(binding_id, thread_ref));
CREATE TABLE messages   (id TEXT PRIMARY KEY, topic_id TEXT, binding_id TEXT, direction TEXT /*in|out*/,
                         platform_msg_id TEXT, thread_ref TEXT, reply_to_msg_id TEXT, text TEXT,
                         routed_by TEXT, route_confidence TEXT, provisional INTEGER DEFAULT 0,
                         debounce_group TEXT, outbox_id TEXT);
CREATE TABLE questions  (id TEXT PRIMARY KEY, handle TEXT /*Q3*/, card_id TEXT, topic_id TEXT, text TEXT,
                         options JSON, status TEXT /*open|answered|expired*/, answer TEXT,
                         nag_count INTEGER DEFAULT 0, answered_at TEXT);
CREATE TABLE outbox     (id TEXT PRIMARY KEY, topic_id TEXT, kind TEXT, priority TEXT, body TEXT,
                         question_id TEXT, card_id TEXT, reply_to_msg_id TEXT, dedupe_key TEXT,
                         not_before TEXT, status TEXT, sent_binding_id TEXT, sent_platform_msg_id TEXT,
                         sent_at TEXT, attempts INTEGER DEFAULT 0);

-- Work
CREATE TABLE cards      (id TEXT PRIMARY KEY, kind TEXT, role TEXT, title TEXT, goal TEXT, original_goal TEXT,
                         done_when JSON, constraints JSON, inputs JSON, budget JSON,
                         depth INTEGER, parent_id TEXT, root_id TEXT, origin_topic_id TEXT, project_id TEXT,
                         state TEXT, phase TEXT, attempt INTEGER, continuation_of TEXT, continuation_n INTEGER,
                         priority TEXT, created_by TEXT, recipe_id TEXT, recipe_step TEXT,
                         due_at TEXT, lease_owner TEXT, lease_expires_at TEXT,
                         result JSON, model_profile TEXT);
CREATE TABLE card_deps  (card_id TEXT, depends_on TEXT, PRIMARY KEY(card_id, depends_on));
CREATE TABLE card_events(id INTEGER PRIMARY KEY, card_id TEXT, from_state TEXT, to_state TEXT,
                         event TEXT, actor TEXT, payload JSON, at TEXT);
CREATE TABLE comments   (id TEXT PRIMARY KEY, card_id TEXT, author TEXT /*owner|verifier|frontdesk|harness|librarian*/,
                         body TEXT);
CREATE TABLE sessions   (id TEXT PRIMARY KEY, card_id TEXT, topic_id TEXT, type TEXT, model_profile TEXT,
                         turns INTEGER, outcome TEXT, started_at TEXT, ended_at TEXT);
CREATE TABLE tool_calls (id TEXT PRIMARY KEY, session_id TEXT, turn INTEGER, tool TEXT, args JSON,
                         result_excerpt TEXT, result_artifact_id TEXT, ok INTEGER, ms INTEGER);
CREATE TABLE artifacts  (id TEXT PRIMARY KEY, card_id TEXT, path TEXT, mime TEXT, bytes INTEGER, summary TEXT);
CREATE TABLE recipes    (id TEXT PRIMARY KEY, title TEXT, body_yaml TEXT, uses INTEGER, successes INTEGER);
CREATE TABLE schedules  (id TEXT PRIMARY KEY, cron TEXT, card_template JSON, origin_topic_id TEXT,
                         next_fire_at TEXT, active INTEGER);

-- Memory
CREATE TABLE notes      (id TEXT PRIMARY KEY, kind TEXT, title TEXT, aliases JSON, one_liner TEXT,
                         body_rendered TEXT, project_id TEXT, tags JSON, dirty INTEGER DEFAULT 0,
                         status TEXT /*active|retracted*/);
CREATE TABLE claims     (id TEXT PRIMARY KEY, note_id TEXT, text TEXT, source JSON, observed_at TEXT,
                         volatility TEXT, status TEXT, superseded_by TEXT, confidence TEXT,
                         corroborations INTEGER DEFAULT 0);
CREATE TABLE facts      (id TEXT PRIMARY KEY, card_id TEXT, msg_id TEXT, subject TEXT, text TEXT,
                         source JSON, volatility TEXT, status TEXT /*pending|merged|discarded|review*/,
                         decision TEXT, decision_reason TEXT, target_claim_id TEXT, decided_at TEXT);
CREATE TABLE review_items (id TEXT PRIMARY KEY, kind TEXT /*conflict|recipe_promotion*/, payload JSON,
                         status TEXT, outbox_id TEXT);

-- LLM calls
CREATE TABLE llm_calls  (id TEXT PRIMARY KEY, call_type TEXT, prompt_version TEXT, model_profile TEXT,
                         session_id TEXT, card_id TEXT, topic_id TEXT, input JSON, raw_output TEXT,
                         parsed JSON, ok INTEGER, error TEXT, tokens_in INTEGER, tokens_out INTEGER,
                         ms INTEGER, at TEXT, eval_label JSON);

-- Search
CREATE VIRTUAL TABLE notes_fts    USING fts5(title, aliases, one_liner, body_rendered, content='notes');
CREATE VIRTUAL TABLE episodic_fts USING fts5(kind, ref_id, text);
-- sqlite-vec: note_vectors(note_id, embedding), topic_vectors(topic_id, embedding), recipe_vectors(...)
```

---

## 10. LLM gateway and call catalog

### 10.1 Gateway responsibilities

- Single function `call(call_type, inputs, *, profile=None, allowed=None) -> Parsed`.
- Loads the prompt template and JSON schema for `call_type` (versioned files under `prompts/<call_type>/v<N>.*`).
- Builds the grammar/schema, applying dynamic restrictions (`allowed` enums, removed actions).
- Selects the model profile (call-type default, overridable per card), enforces slot concurrency and priority.
- Retries **once** on parse/transport failure with the same input and a lower temperature; then raises `LLMFailure`, which the caller maps to a state transition (never an unhandled exception).
- Logs everything to `llm_calls`.

### 10.2 Model profiles (config)

```yaml
profiles:
  small:  {base_url: http://localhost:8080/v1, model: qwen-q4, ctx: 16384, slots: 2, reserve_interactive: 1}
  embed:  {base_url: http://localhost:8081/v1, model: bge-small}
  large:  null        # optional: a stronger model for escalation / consolidation
```

### 10.3 Call catalog

Each call type has one job. Temperature defaults to 0–0.2 for classification, 0.3–0.6 for generation.

| Call type | Used by | Output (constrained) | Notes |
|---|---|---|---|
| `route_sticky` | Router | `{same: yes\|no\|unsure}` | Input: sticky topic title + last 2 lines + new text |
| `route_shortlist` | Router | `{choice: <slug enum>\|new, confidence}` | ≤5 candidates |
| `topic_title` | Router | `{title ≤6 words}` | Slug derived in code |
| `frontdesk_step` | Front desk | tool call or `reply`/`no_reply` | ≤4 steps |
| `summarize_topic` | Front desk | `{summary ≤150 words}` | Incremental; periodic rebuild |
| `extract_owner_facts` | Front desk | `{facts[]}` | Owner statements only |
| `extract_entities` | Librarian | `{entities: [≤6 strings]}` | |
| `librarian` | Librarian | see §7.6 | |
| `triage` | Dispatcher | see §5.5 | |
| `plan_fill` | Planner | recipe params | Schema from recipe |
| `plan_generate` | Planner | ≤5 subtasks DAG | |
| `worker_step` | Worker | tool call or terminal action | Union schema per session |
| `verify_criterion` | Verifier | `{pass, reason ≤30 words}` | One per judge criterion |
| `match_subject` | Consolidator | `{note_id enum \| none}` | |
| `relevance_rubric` | Consolidator | 5 booleans | Keep-rule in code |
| `consolidate_fact` | Consolidator | `{decision, target_claim_id?}` | |
| `render_brief` / `render_note` | Consolidator | markdown ≤ N tokens | From claims only |
| `render_answer` | Librarian | role result schema | Memory-sourced result |
| `report_result` | Front desk (optional) | `{text}` | Only if `report_mode=frontdesk` |
| `generalize_recipe` | Recipes | recipe YAML draft | Human reviews |

### 10.4 Prompt construction rules

- System prompt short and role-specific; no generic "you are a helpful assistant" boilerplate.
- Put the **question last**, directly before generation.
- Present options as a numbered or slugged list; the schema enum matches exactly.
- 1–3 short in-prompt examples for every classification call type (few-shot examples are cheap relative to the reliability gain for small models).
- Never include instructions the grammar already enforces; enforce, don't ask.
- Dates: always inject current date/time and the owner's timezone into any call that deals with time.
- Truncation marks are explicit: `[... 1,240 tokens omitted — use read_artifact(art_x)]`.

---

## 11. Safety, permissions and side effects

- **Tool risk classes:** `read` (search, fetch, read files), `write-local` (workspace files), `side-effect` (send email, post publicly, purchase, modify external systems, delete).
- `side-effect` tools require **approval**: the tool call is converted into a `block` question to the owner ("Card *X* wants to send this email: … Approve / reject / edit?") unless an allow-rule in config matches (e.g. "posting to my own Matrix room is always allowed").
- Workers cannot create `side-effect` roles; only the front desk (on owner instruction) or the human can create `ops` cards.
- Secrets live in config/env, are injected into tool implementations, and never appear in prompts, logs or artifacts. The call logger redacts known secret patterns.
- Inbound messages from unknown identities are dropped; web UI requires authentication.
- Web content fetched by `research` tools is treated as data. It is placed in clearly delimited blocks, and tool results can never change a card's toolset, budget or permissions (these are harness-owned).

> **Why:** a weak model is also easier to prompt-inject. Keeping permissions in the harness (P1, P4) means an injected instruction can at worst produce a bad result or a question to the owner, not an action.

---

## 12. Rejected alternatives

| Alternative | Why rejected |
|---|---|
| **Large tool catalogs + a `search_tools` meta-tool** | Tool choice accuracy collapses with count, especially for small models; meta-tools add a planning step weak models fail at. Scope work into cards with role toolsets instead (P4). |
| **Direct agent-to-agent chat / mailboxes** | Requires waiting sessions, shared context and free-text interpretation. Not durable, not inspectable. The board replaces it (P5). |
| **RPC-style `delegate_task` (parent blocks until child returns)** | Parent holds context while waiting; lost on crash; no human in the loop. Split + fresh-session join replaces it (P6). |
| **Workers deciding mid-task to spawn children at will** | Leads to over- or under-splitting. Decomposition happens at triage or through the `split` exit, both constrained and capped. |
| **Sessions bound to channels** | Causes cross-topic contamination and breaks when the owner switches devices. Topics replace it (P7). |
| **Agent picks the delivery channel** | Channel choice is policy; deterministic courier (§6.7). |
| **In-session "save to memory" nudges** | Distracts weak models from the task, creates duplicates. Offline consolidation instead (P10). |
| **Pure vector-chunk RAG as long-term memory** | No merging, no staleness, no provenance, poor for exact entities. Entity notes + claims + hybrid search instead (§7.3). |
| **Model-initiated memory queries as the primary retrieval path** | Unknown unknowns. Push retrieval + librarian instead (P9). |
| **Importance scores (1–10) for memory** | Noisy with small models. Binary rubric + code rule instead (§7.5). |
| **Long-running orchestrator agent holding the plan in context** | Context overflow and drift; plan state belongs on the board. |

---

## 13. Failure modes and mitigations

| Failure | Detection | Mitigation |
|---|---|---|
| Misrouted message | Owner `→` correction; provisional flag | Visible labels, one-word correction, re-parenting of cards, corrections added to eval set |
| Card explosion | `max_open_cards_per_root`, depth/fan-out caps | Harness refuses splits past caps; blocks to owner |
| Checkpoint loops | `continuation_n > max_continuations` | Block with "this seems mis-scoped" + progress summary |
| Worker never finishes | Turn budget | Last turn allows only terminal actions |
| Garbage output | Schema parse failure | Grammar-constrained decoding; 1 retry; then card `failed` → escalation |
| Verifier too lenient/strict | Owner feedback on results; eval set | Per-criterion judges; deterministic checks first; tune prompts via replay |
| Summary drift (topics, briefs) | Periodic rebuild | Rebuild from source, never from prior summary (§6.5, §7.8) |
| Memory pollution | Owner review, `/forget` | Rubric, provenance, retraction list, contradiction review |
| Stale memory used as truth | Volatility ages | `[as of date]` markers, refresh cards |
| Slow model starves the owner | Queue latency metrics | Interactive slot reservation, fast acks, priorities |
| Model server down | Gateway transport errors | Cards stay `ready`; outbox sends urgent alert via any working adapter; exponential backoff |
| Crash mid-session | Expired lease | Reclaim → `ready`; session transcript retained for debugging |
| Prompt injection via web content | — | Harness-owned permissions; side-effect approvals (§11) |

---

## 14. Observability, UI and evaluation

### 14.1 Web UI (v1 minimum)

- **Board:** columns by state; filter by topic/project/root; card detail with contract, events timeline, sessions, tool calls, result, comments; actions: comment, cancel, retry, edit goal, change priority.
- **Topics:** list with slug, status, summary; chat view per topic; composer with topic selector.
- **Memory:** notes browser (rendered body + claims with provenance, status), edit/retract, review queue (conflicts, recipe promotions), pending facts queue with consolidator decisions.
- **Calls:** `llm_calls` browser filtered by call type, with "label as correct/incorrect" and "add to golden set".
- **Health:** queue depth by priority, slot utilization, median latency per call type, failure rates.

### 14.2 Evaluation

- `evals/<call_type>/*.jsonl` golden sets: input + expected parsed output (or acceptable set).
- `sandman eval <call_type> [--prompt vN] [--profile p]` replays and reports accuracy, parse-failure rate and latency.
- Sources for golden data: owner corrections (routing), verifier outcomes vs owner feedback, consolidator decisions reviewed in UI, hand-written seeds (≥20 per classification call type before M-completion).
- Prompt changes MUST be versioned and evaluated against the previous version before becoming default.

> **Why:** with weak models, quality comes from iterating prompts, schemas and task decomposition against real data — which is impossible without replayable logs and per-call-type test sets.

---

## 15. Implementation plan (milestones)

Suggested repository layout:

```
sandman/
  core/        db.py, ids.py, config.py, events.py
  llm/         gateway.py, profiles.py, schemas.py, grammar.py
  prompts/     <call_type>/v1.md, v1.schema.json, examples.jsonl
  work/        board.py (state machine), dispatcher.py, triage.py, planner.py, worker.py,
               verifier.py, scheduler.py, roles/, recipes/
  conversation/ adapters/{web,matrix}.py, inbox.py, router.py, frontdesk.py, outbox.py, courier.py, commands.py
  memory/      facts.py, consolidator.py, retriever.py, librarian.py, notes.py, render.py
  tools/       web.py, files.py, artifacts.py, sandbox.py, notes_tool.py
  web/         api.py, ui/
  evals/       <call_type>/*.jsonl, runner.py
  docs/        DESIGN.md (this file), DEVIATIONS.md
```

### M0 — Foundations
- DB schema + migrations, ID generation, config loading.
- LLM gateway with JSON-schema constrained output, profiles, slots/priorities, `llm_calls` logging, retry policy.
- `sandman eval` runner skeleton.
- **Accept:** a test call type returns schema-valid output from the local server 50/50 times; logs are replayable.

### M1 — Board and workers
- Cards, deps, events, comments; state machine with all transitions of §5.3 as one tested module.
- Dispatcher with leases and crash recovery.
- Worker loop with union schema, dynamic restrictions, last-turn rule, context pack assembly with budgets.
- Roles `research` and `write`; tools `web_search`, `web_fetch`, artifacts.
- CLI: `sandman card create/list/show/cancel`.
- **Accept:** (1) a single research card completes with a schema-valid result; (2) killing the process mid-session and restarting reclaims and completes the card; (3) a card with `turns: 3` always ends with a terminal action.

### M2 — Decomposition and verification
- Triage, planner (recipe + free mode), split/join with `phase=synthesize`, `synthesize` role.
- Verifier (deterministic + judge), retry/escalation, checkpoint/continuations, hard limits.
- Two seed recipes.
- **Accept:** (1) a goal requiring comparison of 3 options produces a plan, 3–5 children, and a synthesized result; (2) depth/fan-out caps are enforced by grammar; (3) a deliberately failing criterion causes one retry with verifier feedback, then an escalation question.

### M3 — Conversation (web first)
- Web adapter + minimal chat UI with topic sidebar and selector.
- Inbox, debouncer, router stages 1, 4, 5, 6, 7; topic creation/lifecycle; corrections.
- Front desk with tools; rolling summaries; fast acks.
- Outbox + courier (single binding), labels, result reporting (`template` mode).
- **Accept:** (1) two interleaved subjects in one chat are routed to two topics with ≥90% accuracy on a 30-message scripted conversation; (2) `→ new` correctly re-routes and re-parents a created card; (3) a card created from chat reports its result back into the same topic.

### M4 — Matrix, questions, proactive
- Matrix adapter with threads/replies/reactions; router stages 2 and 3; topic ↔ thread mapping.
- `block` questions with handles, answer routing, nags.
- Courier: multi-binding resolution, quiet hours, digests, dedupe, rate limits.
- Reminders, schedules, commands (§6.10).
- **Accept:** (1) a question raised by a card on a web-originated topic is delivered to Matrix when the owner last spoke there, and a threaded reply unblocks the card; (2) a reminder set on web fires on the default binding at the right local time; (3) no message appears on a binding/thread belonging to a different topic in a 1-day soak test.

### M5 — Memory
- Facts queue from results/checkpoints/owner statements/negatives.
- Notes/claims, consolidator with rubric, contradiction policy, review items.
- Retriever (entity exact match, FTS + vector hybrid), librarian with `answered|narrow|proceed`, staleness + refresh cards.
- Project briefs; profile note; `/forget`.
- **Accept:** (1) researching topic X, then a week later asking a related question about X, results in the librarian answering or narrowing the new card; (2) duplicate facts from two cards merge into one claim with a corroboration; (3) a contradicting evergreen fact generates a review item; (4) owner-stated preferences appear in the profile and in subsequent sessions.

### M6 — UI, evaluation, hardening
- Full web UI (§14.1), eval sets ≥20 items per classification call type, prompt versioning workflow.
- `code` role with sandbox; side-effect approvals.
- Recipe promotion flow.
- **Accept:** eval reports for all classification call types; a replay of logged routing calls against a new prompt version shows comparative accuracy.

---

## 16. Configuration (defaults)

```yaml
owner: {name: "…", timezone: Europe/Zurich, quiet_hours: "22:30-07:30"}
bindings:
  default_for: {urgent: matrix_dm, normal: last_active, low: digest}
digest: {times: ["08:00", "18:00"]}
router: {debounce_seconds: 30, debounce_max: 120, sticky_minutes: 20, shortlist_size: 5,
         dormant_days: 14, correction_hint_times: 3}
frontdesk: {max_steps: 4, history_messages: 8, ack_after_seconds: 20, report_mode: template,
            summary_rebuild_every: 20}
board: {max_depth: 3, max_children_per_split: 5, max_attempts: 2, max_continuations: 4,
        max_open_cards_per_root: 25, lease_seconds: 300}
roles:
  research:   {max_turns: 12, tools: [web_search, web_fetch, open_note, read_artifact, write_artifact]}
  write:      {max_turns: 8,  tools: [read_artifact, write_artifact, open_note]}
  synthesize: {max_turns: 6,  tools: [read_artifact, open_note, write_artifact]}
  code:       {max_turns: 15, tools: [read_file, write_file, run, list_dir]}
context_budgets: {preamble: 300, card: 400, profile: 300, brief: 600, catalog: 400,
                  inputs: 1500, comments: 500}      # scaled by profile ctx
memory:
  volatility_max_age_days: {volatile: 7, slow: 180, evergreen: null}
  librarian: {enabled_roles: [research, write, synthesize], top_k: 6}
  consolidator: {schedule: "03:00", batch_size: 50, profile: small}
  refresh_cooldown_days: 7
questions: {nag_hours: 24, max_nags: 2}
courier: {rate_limit_per_10min: 6, binding_affinity_days: 7}
```

---

## 17. End-to-end walkthrough

A concrete trace to validate understanding. Owner on Matrix, Tuesday 10:02.

1. **Inbound.** Owner (Matrix DM, no thread): "can you figure out whether drip irrigation makes sense for my raised beds" → inbox → debounce (30 s, nothing else arrives).
2. **Route.** No command, no thread, no reply. Last message on this binding was 3 days ago → skip sticky. Shortlist: `taxes`, `bike`, `new`. Model → `new`, high. `topic_title` → "Raised bed irrigation", slug `irrigation`. Courier will open a Matrix thread `[irrigation] Raised bed irrigation`.
3. **Front desk.** Context: profile (lives in Zurich, prefers low-maintenance solutions), empty topic, catalog shows `not_garden_beds — "Owner's 3 raised beds, 40 m², south-facing balcony-adjacent"`. Calls `open_note`, then `create_card(title="Evaluate drip irrigation for raised beds", goal=…, done_when=["compare drip vs manual watering on cost, effort, water use", "recommend one option"], role=research)`, then `reply("On it — I'll compare drip kits against manual watering for your 3 beds and come back with a recommendation.")`.
4. **Librarian.** Entities: `drip irrigation`, `raised beds`. Exact match: `not_garden_beds`. Search: nothing on drip. Decision: `proceed`.
5. **Triage.** Recipe shortlist includes `rcp_research_compare_recommend` → `fits_one_session: no`, recipe chosen. Planner `plan_fill`: `subject="drip irrigation kits available in CH"`, `criteria="cost, setup effort, water use, suitability for 40 m²"`, `max_options=3`.
6. **Children.** `gather` (research) runs, finds 3 kits, `finish` with facts (prices, coverage). Harness fans out 3 `detail` cards. One `detail` card hits its turn budget → `checkpoint`; continuation card finishes. All `detail` done.
7. **Synthesize.** Parent re-queued with `phase=synthesize`; receives 3 summaries + artifacts list; `finish` with recommendation. Verifier: `min_items` passes, judge "recommend one option" passes → `done`.
8. **Report.** Root done → outbox `kind=result`, courier resolves topic's last inbound binding = Matrix DM, thread `irrigation` → posts summary + artifact links.
9. **Meanwhile**, at 11:15 the owner writes in the same DM (not in the thread): "also remind me to file the tax extension friday". Router: sticky check vs `irrigation` → `no`; shortlist → `taxes`. Front desk (topic `taxes`) → `remind(when="friday", …)` → reply `[taxes] Reminder set for Fri 2 Oct, 09:00.`
10. **Night.** Consolidator: facts about 3 kits → new entity notes (`volatility: volatile` for prices, `slow` for coverage specs); owner-sourced fact "prefers low-maintenance" corroborated; project brief for the garden project regenerated.
11. **Two weeks later**, the owner asks "what did that drip kit cost again, and is it still on sale?" Librarian finds the kit note via exact match → `narrow`: "Check current price of <kit> (last seen CHF 89 on Sept 29)". A 2-turn card instead of a full research tree.

---

## 18. Open questions (to decide during implementation)

1. **Router accuracy bar.** Is ≥90% on scripted conversations enough given visible corrections, or should low-confidence cases always ask? Decide from M3 data.
2. **Front desk report mode.** Template reports are fast but terse; front-desk-phrased reports are nicer but cost a model call per result. Maybe per-topic setting.
3. **Embedding model choice** and whether vector search adds enough over FTS + exact entity match at this scale (likely small corpus). Measure in M5 before depending on it.
4. **Multiple topics in one message** ("fix the garden thing and also book the bike service"). v1: route to primary topic; the front desk may `create_card` with another topic via an optional `topic` argument. Evaluate if splitting at the router is needed.
5. **Group chats with other humans.** Out of scope for v1; the identity model allows it later.
6. **Using a stronger model intermittently** (e.g. a remote API for consolidation and escalation only). The profile system supports it; privacy policy for what may leave the machine must be decided first.
7. **Automatic recipe learning** beyond human-approved promotion.

---

## Appendix A — Example prompt templates

These are illustrative starting points; the implementing agent should version them under `prompts/` and evaluate them.

### A.1 `route_sticky` (v1)

```
You sort chat messages into conversation topics.

Current topic: "{topic_title}"
Last messages in this topic:
{last_two_lines}

New message:
"{new_text}"

Is the new message about the current topic? Answer yes, no, or unsure.
```
Schema: `{"type":"object","properties":{"same":{"enum":["yes","no","unsure"]}},"required":["same"]}`

### A.2 `triage` (v1)

```
You decide whether a task can be completed in ONE work session.

A session has at most {max_turns} steps and these tools:
{tool_lines}

Task: {title}
Goal: {goal}
Done when:
{done_when_lines}

Known plans that might fit (or "none"):
{recipe_lines}

Examples:
- "Summarize this one article into 5 bullet points" with tools [read_artifact, write_artifact] → yes, none
- "Compare 4 health insurers on price and coverage and recommend one" → no, rcp_research_compare_recommend

Answer:
- fits_one_session: yes / no / unsure
- recipe_id: one of the listed ids, or none
- missing_info: a short question for the user ONLY if the task cannot start without it, else null
```

### A.3 `relevance_rubric` (v1)

```
A work session produced this candidate fact:
Subject: {subject}
Fact: "{text}"
Source: {source_type}
Task it came from: "{card_title}"

Answer each with true or false:
reusable: Could a DIFFERENT future task plausibly need this fact?
costly: Would finding this again take real effort (research or asking the user)?
durable: Will this likely still be true in a week?
task_mechanics: Is this only about how this task was carried out (tools used, steps taken)?
trivial: Is this common knowledge that any assistant already knows?
```

### A.4 `worker_step` footer (appended every turn)

```
Step {k} of {n}. Choose exactly one action.
- Use a tool if you need more information or need to save work.
- finish if every "done when" item is satisfied.
- split only if the task clearly needs 2–5 separate pieces of work.
- block if you cannot continue without the user's input.
- checkpoint if you are making progress but will run out of steps.
- fail if the task is impossible or out of scope.
{final_turn_notice}
```

---

*End of design document.*
