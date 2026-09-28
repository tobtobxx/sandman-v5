"""Tool implementations. A ToolEnv is built by the harness per session; the
model only ever sees the tools of its role (design §5.7, P4)."""
import html
import json
import os
import re
import subprocess
import uuid

import requests

RESULT_CHARS = 2500


class FakeWeb:
    """Offline web over a fixed corpus: [{url, title, text}]. Used by the bench
    and for offline demos, so results are deterministic."""

    def __init__(self, pages):
        self.pages = pages

    def search(self, query):
        q = set(re.findall(r"\w+", query.lower()))
        scored = []
        for p in self.pages:
            words = set(re.findall(r"\w+", (p["title"] + " " + p["text"]).lower()))
            score = len(q & words) + 2 * len(q & set(re.findall(r"\w+", p["title"].lower())))
            if score:
                scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return "No results."
        return "\n".join(f"- {p['title']}\n  {p['url']}\n  {p['text'][:160]}..." for _, p in scored[:5])

    def fetch(self, url):
        for p in self.pages:
            if p["url"].rstrip("/") == url.rstrip("/"):
                return f"# {p['title']}\n\n{p['text']}"
        return "Error: 404 not found."


class RealWeb:
    """Best-effort live web: DuckDuckGo HTML search + plain fetch."""

    def search(self, query):
        try:
            r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=20,
                              headers={"User-Agent": "Mozilla/5.0"})
            items = re.findall(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
                               r.text, re.S)
            out = []
            for url, title, snip in items[:5]:
                m = re.search(r"uddg=([^&]+)", url)
                if m:
                    url = requests.utils.unquote(m.group(1))
                out.append(f"- {_text(title)}\n  {url}\n  {_text(snip)}")
            return "\n".join(out) or "No results."
        except requests.RequestException as e:
            return f"Error: {e}"

    def fetch(self, url):
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            return _text(r.text)[:20000]
        except requests.RequestException as e:
            return f"Error: {e}"


def _text(h):
    h = re.sub(r"(?is)<(script|style|nav|footer|header).*?</\1>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


class ArtifactStore:
    """Artifacts of one card tree: files on disk + a small index (design §8)."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.index_path = os.path.join(root, "artifacts.json")
        self.index = json.load(open(self.index_path)) if os.path.exists(self.index_path) else {}

    def write(self, name, content, card_id=None):
        art_id = "art_" + uuid.uuid4().hex[:8]
        safe = re.sub(r"[^\w.\-]", "_", name)[:60] or "file.txt"
        path = os.path.join(self.root, f"{art_id}_{safe}")
        open(path, "w").write(content)
        self.index[art_id] = {"name": safe, "path": path, "card_id": card_id, "chars": len(content),
                              "summary": content[:100].replace("\n", " ")}
        json.dump(self.index, open(self.index_path, "w"), indent=1)
        return art_id

    def read(self, art_id, offset=0, length=RESULT_CHARS):
        if art_id not in self.index:
            return "Error: unknown artifact."
        c = open(self.index[art_id]["path"]).read()
        part = c[offset:offset + length]
        if offset == 0 and len(c) <= length:
            return part
        # the marker goes first so transcript truncation can't cut it off
        more = f", read on with read_artifact(art_id='{art_id}', offset={offset + length})" \
            if len(c) > offset + length else ", end of file"
        return f"[characters {offset}-{min(len(c), offset + length)} of {len(c)}{more}]\n{part}"

    def lines(self, ids=None):
        return [f"{a} — {m['name']} — {m['summary']}" for a, m in self.index.items() if ids is None or a in ids]


class ToolEnv:
    def __init__(self, web=None, artifacts=None, workdir=None, notes=None, card_id=None):
        self.web = web or RealWeb()
        self.artifacts = artifacts
        self.workdir = workdir
        self.notes = notes or {}     # note_id -> rendered text
        self.card_id = card_id
        self.written = []            # artifact ids written in this session

    def _path(self, p):
        full = os.path.realpath(os.path.join(self.workdir, p))
        if not full.startswith(os.path.realpath(self.workdir)):
            raise ValueError("path outside work folder")
        return full

    def execute(self, a):
        t = a["action"]
        try:
            if t == "web_search":
                return self.web.search(a["query"])
            if t == "web_fetch":
                page = self.web.fetch(a["url"])
                if len(page) > RESULT_CHARS and self.artifacts is not None:
                    art = self.artifacts.write(re.sub(r"\W+", "_", a["url"])[-50:] + ".txt", page, self.card_id)
                    return "(long page, saved) " + self.artifacts.read(art)
                return page
            if t == "read_artifact":
                return self.artifacts.read(a["art_id"], a.get("offset", 0))
            if t == "write_artifact":
                art = self.artifacts.write(a["name"], a["content"].rstrip() + "\n", self.card_id)
                self.written.append(art)
                return f"Saved as {art}."
            if t == "read_file":
                return open(self._path(a["path"])).read()
            if t == "write_file":
                p = self._path(a["path"])
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").write(a["content"].rstrip() + "\n")
                return f"Wrote {len(a['content'].splitlines())} lines to {a['path']}."
            if t == "list_dir":
                p = self._path(a.get("path") or ".")
                return "\n".join(sorted(os.listdir(p))) or "(empty)"
            if t == "run":
                # Prototype: no real sandbox (see docs/DEVIATIONS.md), just cwd + timeout.
                r = subprocess.run(a["command"], shell=True, cwd=self.workdir, capture_output=True,
                                   text=True, timeout=60)
                return f"exit code {r.returncode}\n{(r.stdout + r.stderr)[-RESULT_CHARS:]}"
        except Exception as e:  # tool errors are shown to the model, never raised
            return f"Error: {type(e).__name__}: {e}"
        return "Error: unknown tool."
