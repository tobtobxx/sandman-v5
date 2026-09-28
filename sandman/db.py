"""SQLite store (design §9, trimmed to what the prototype uses)."""
import json
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (id TEXT PRIMARY KEY, slug TEXT UNIQUE, title TEXT, status TEXT DEFAULT 'active',
    summary TEXT DEFAULT '', summary_msg_count INTEGER DEFAULT 0, last_activity_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, topic_id TEXT, binding TEXT, direction TEXT, text TEXT,
    routed_by TEXT, route_confidence TEXT, provisional INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS questions (id TEXT PRIMARY KEY, handle TEXT, card_id TEXT, topic_id TEXT, text TEXT,
    options JSON, status TEXT, answer TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, topic_id TEXT, kind TEXT, priority TEXT, body TEXT,
    card_id TEXT, question_id TEXT, dedupe_key TEXT, not_before TEXT, status TEXT, sent_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS cards (id TEXT PRIMARY KEY, kind TEXT, role TEXT, title TEXT, goal TEXT, original_goal TEXT,
    done_when JSON, constraints JSON, inputs JSON, depth INTEGER, parent_id TEXT, root_id TEXT,
    origin_topic_id TEXT, state TEXT, phase TEXT DEFAULT 'execute', attempt INTEGER DEFAULT 1,
    continuation_n INTEGER DEFAULT 0, priority TEXT DEFAULT 'normal', created_by TEXT, recipe_id TEXT,
    recipe_step TEXT, recipe_params JSON, due_at TEXT, lease_owner TEXT, lease_expires_at REAL,
    result JSON, model_profile TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS card_deps (card_id TEXT, depends_on TEXT, PRIMARY KEY(card_id, depends_on));
CREATE TABLE IF NOT EXISTS card_events (id INTEGER PRIMARY KEY, card_id TEXT, from_state TEXT, to_state TEXT,
    event TEXT, payload JSON, at TEXT);
CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, card_id TEXT, author TEXT, body TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, aliases JSON, one_liner TEXT,
    status TEXT DEFAULT 'active', dirty INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, note_id TEXT, text TEXT, source JSON, observed_at TEXT,
    volatility TEXT, status TEXT DEFAULT 'active', superseded_by TEXT, corroborations INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, card_id TEXT, subject TEXT, text TEXT, source JSON,
    volatility TEXT, status TEXT DEFAULT 'pending', decision TEXT, decision_reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS review_items (id TEXT PRIMARY KEY, kind TEXT, payload JSON, status TEXT);
CREATE TABLE IF NOT EXISTS llm_calls (id TEXT PRIMARY KEY, call_type TEXT, model TEXT, input JSON, raw TEXT,
    parsed JSON, ok INTEGER, error TEXT, tokens_in INTEGER, tokens_out INTEGER, ms INTEGER, at TEXT,
    session_id TEXT, card_id TEXT, topic_id TEXT, provider TEXT, cost REAL, attempt INTEGER, temperature REAL,
    max_tokens INTEGER, schema JSON);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, card_id TEXT, topic_id TEXT, type TEXT, model TEXT,
    info JSON, turns INTEGER, outcome TEXT, started_at TEXT, ended_at TEXT);
CREATE TABLE IF NOT EXISTS tool_calls (id TEXT PRIMARY KEY, session_id TEXT, turn INTEGER, tool TEXT, args JSON,
    result TEXT, ok INTEGER, ms INTEGER, at TEXT, after_call TEXT);
CREATE INDEX IF NOT EXISTS llm_calls_session ON llm_calls(session_id);
CREATE INDEX IF NOT EXISTS llm_calls_card ON llm_calls(card_id);
CREATE INDEX IF NOT EXISTS sessions_card ON sessions(card_id);
CREATE INDEX IF NOT EXISTS tool_calls_session ON tool_calls(session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(note_id UNINDEXED, title, aliases, one_liner, claims);
"""

JSON_COLS = {"done_when", "constraints", "inputs", "result", "options", "aliases", "source", "payload",
             "recipe_params", "parsed", "input", "schema", "info", "args"}
# columns added after the first prototype; added to old databases on open
MIGRATIONS = [("llm_calls", c) for c in ("session_id TEXT", "card_id TEXT", "topic_id TEXT", "provider TEXT",
                                         "cost REAL", "attempt INTEGER", "temperature REAL", "max_tokens INTEGER",
                                         "schema JSON")] + [("tool_calls", "after_call TEXT")]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_id(prefix):
    # Design wants ULIDs; time-ordered hex is enough for the prototype.
    return f"{prefix}_{int(time.time() * 1000):x}{uuid.uuid4().hex[:6]}"


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        for table, col in MIGRATIONS:
            try:
                self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # exists already, or the table is new
        self.c.executescript(SCHEMA)

    def q(self, sql, *args):
        return [self._row(r) for r in self.c.execute(sql, args).fetchall()]

    def one(self, sql, *args):
        r = self.q(sql, *args)
        return r[0] if r else None

    def x(self, sql, *args):
        return self.c.execute(sql, args)

    def insert(self, table, **kw):
        kw = {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in kw.items()}
        self.c.execute(f"INSERT INTO {table} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))

    def update(self, table, id, **kw):
        kw = {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in kw.items()}
        self.c.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), id))

    def get(self, table, id):
        return self.one(f"SELECT * FROM {table} WHERE id=?", id)

    @staticmethod
    def _row(r):
        d = dict(r)
        for k in d:
            if k in JSON_COLS and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d

    def tx(self):
        return _Tx(self.c)

    def log_call(self, rec):
        self.insert("llm_calls", id=rec["id"], call_type=rec["call_type"], model=rec["model"],
                    input={"system": rec["system"], "system_hint": rec.get("system_hint", ""), "user": rec["user"]}, raw=rec["raw"], parsed=rec["parsed"],
                    ok=int(rec["ok"]), error=rec["error"], tokens_in=rec["tokens_in"], tokens_out=rec["tokens_out"],
                    ms=rec["ms"], at=rec["at"], session_id=rec.get("session_id"), card_id=rec.get("card_id"),
                    topic_id=rec.get("topic_id"), provider=rec.get("provider"), cost=rec.get("cost"),
                    attempt=rec.get("attempt"), temperature=rec.get("temperature"),
                    max_tokens=rec.get("max_tokens"), schema=rec.get("schema"))


class _Tx:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        self.c.execute("BEGIN IMMEDIATE")

    def __exit__(self, et, *a):
        self.c.execute("ROLLBACK" if et else "COMMIT")
