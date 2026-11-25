#!/usr/bin/env python3
"""
MCP "tools-lite" gateway: two constant meta-tools in front of N MCP servers.

Why this exists
===============
An MCP server advertises every tool it owns in `tools/list`, and the client
re-sends that list to the model on EVERY turn, at the top of the prompt (the
tools block renders inside the system area of the chat template).

This stack advertises 126 tools from the docker-mcp gateway (109) plus Home
Assistant (17): ~107k characters of JSON schema, i.e. 33k-47k tokens. On a
cloud model that is a cost. On the cheap LLM node (Orange Pi Zero 3W: 12.288
tokens of context, 20-35 tok/s of prefill) it is impossible - the 47k-token
prompt needs ~40 minutes to prefill and does not fit at all.

Two further constraints make "just prune the list per request" useless:

  * the tools block sits at the START of the prompt, so any change to it
    invalidates llama.cpp's prefix cache and the whole conversation is
    re-prefilled from zero (measured: an identical 8.7k-token prompt was
    re-prefilled, 617 s);
  * therefore the tool surface must be SMALL and CONSTANT across turns.

So this proxy replaces the whole surface with two tools that never change:

  tools_search(query, k=3, names_only=false)
      Semantic search over a local index of every tool name/description/
      schema. Returns the FULL JSON schema of the k best matches, so the
      model can call them in the very next step.

  tool_call(name, arguments)
      Executes any tool from the catalogue on its upstream server and relays
      the result verbatim (text, images, structured content - untouched).

Result: ~250 tokens instead of ~33k, all 126 tools remain reachable, and no
client needs to change: this is just another MCP server.

Deliberately stdlib only (no `mcp` SDK): it must start in a 128 MB python
container and never depend on PyPI at boot.

Upstreams
=========
`MCP_LITE_UPSTREAMS` is a JSON array; each entry needs a name and a url, and
either `token` or `token_env` (read from the environment):

  [{"name":"gateway","url":"http://mcp-gateway:8811/mcp",
    "token_env":"MCP_GATEWAY_AUTH_TOKEN"},
   {"name":"homeassistant","url":"http://host.docker.internal:8123/api/mcp",
    "token_env":"MCP_LITE_HA_TOKEN"}]

The proxy owns ONE streamable-HTTP session per upstream, heals it on
"session not found", and releases it after MCP_LITE_IDLE_TTL seconds without
client traffic (the gateway then stops the long-lived child containers it
keeps per session).

Embeddings
==========
The index is built with the stack's own `embedding-model` (Qwen3-Embedding-
0.6B on the cheap node), over HTTP, batched. Measured on 126 tools: recall@8
15/18 on a 18-query Italian benchmark, versus 9/18 for Open WebUI's BM25
search, at ~108 ms per query and ~100 s to build the index once (then it is
cached on disk and rebuilt only when the catalogue changes).

If the embedding endpoint is unreachable the proxy keeps working with a
lexical (IDF-weighted) scorer, so a down node degrades recall, never service.
"""

import hashlib
import hmac
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

# Optional bearer token on the front door. The upstream tokens stay inside the
# proxy (and in its environment): a client only ever needs this one, which is
# the same value the gateway itself uses, so existing clients keep working.
# Empty = no authentication (only sensible on a trusted, firewalled host).
AUTH_TOKEN = os.environ.get("MCP_LITE_AUTH_TOKEN", "")

LISTEN_HOST = os.environ.get("MCP_LITE_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MCP_LITE_PORT", "8814"))

EMBED_URL = os.environ.get("MCP_LITE_EMBED_URL", "")
EMBED_MODEL = os.environ.get("MCP_LITE_EMBED_MODEL", "embedding-model")
EMBED_BATCH = int(os.environ.get("MCP_LITE_EMBED_BATCH", "8"))
EMBED_TIMEOUT = float(os.environ.get("MCP_LITE_EMBED_TIMEOUT", "180"))

CACHE_PATH = os.environ.get("MCP_LITE_CACHE", "/cache/index.json")
TOP_K = int(os.environ.get("MCP_LITE_TOP_K", "3"))
NAMES_ONLY_LIMIT = int(os.environ.get("MCP_LITE_NAMES_ONLY_LIMIT", "40"))
OTHER_NAMES = int(os.environ.get("MCP_LITE_OTHER_NAMES", "12"))
REFRESH_TTL = float(os.environ.get("MCP_LITE_REFRESH_TTL", "3600"))
CALL_TIMEOUT = float(os.environ.get("MCP_LITE_CALL_TIMEOUT", "900"))
LIST_TIMEOUT = float(os.environ.get("MCP_LITE_LIST_TIMEOUT", "240"))
# A catalogue refresh must never hang on one broken MCP server: `tools/list`
# gets this much, in parallel across upstreams. A slow/failed upstream keeps
# its previous tools (see Catalog.refresh).
REFRESH_TIMEOUT = float(os.environ.get("MCP_LITE_REFRESH_TIMEOUT", "45"))
# The stale check costs nothing, but it must not run on every keystroke.
REFRESH_MIN_GAP = float(os.environ.get("MCP_LITE_REFRESH_MIN_GAP", "30"))
IDLE_TTL = float(os.environ.get("MCP_LITE_IDLE_TTL", "900"))
IDLE_SWEEP = max(1.0, float(os.environ.get("MCP_LITE_IDLE_SWEEP", "30")))

STABLE_SID = "tools-lite-stable"


def log(msg):
    sys.stderr.write("[tools-lite] %s\n" % msg)
    sys.stderr.flush()


class RpcError(Exception):
    pass


# --------------------------------------------------------------------------
# upstream: one MCP client session per server, streamable HTTP
# --------------------------------------------------------------------------

def _parse_body(data):
    """Parse an upstream reply: plain JSON, or SSE framing (`data: {...}`)."""
    if not data:
        return None
    text = data.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    if "data:" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                return json.loads(chunk)
            except Exception:
                continue
    return None


class Upstream:
    def __init__(self, name, url, token):
        self.name = name
        self.url = url
        self.token = token or ""
        self.sid = None
        self.stateless = False
        self.ready = False
        self.init_result = None
        self.lock = threading.Lock()
        self._ids = 0
        self._id_lock = threading.Lock()

    def _next_id(self):
        with self._id_lock:
            self._ids += 1
            return self._ids

    def _request(self, body, method="POST", timeout=LIST_TIMEOUT, with_sid=True):
        req = urllib.request.Request(self.url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        if with_sid and self.sid:
            req.add_header("Mcp-Session-Id", self.sid)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
        except Exception as e:
            return 502, {}, json.dumps({"error": {"message": "upstream %s: %s" % (self.name, e)}}).encode()

    def ensure(self, force=False):
        """(Re-)initialize this upstream's session.

        Two shapes are supported, because servers disagree: the docker-mcp
        gateway answers `initialize` with an Mcp-Session-Id header (stateful
        streamable HTTP), Home Assistant answers 200 and no header (stateless).
        A stateless upstream is usable without a session id; requiring one
        silently dropped all 17 of its tools.
        """
        with self.lock:
            if force and self.ready:
                # Release, do not leak: a live session keeps the gateway's
                # long-lived child containers alive for as long as it exists.
                if self.sid:
                    self._request(None, method="DELETE", timeout=30, with_sid=True)
                self.sid = None
                self.ready = False
            if self.ready:
                return True
            payload = json.dumps({
                "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "mcp-tools-lite", "version": "1.0"}}}).encode()
            status, headers, data = self._request(payload, with_sid=False)
            obj = _parse_body(data) or {}
            if status != 200 or "result" not in obj:
                log("initialize failed on %s: HTTP %s %s"
                    % (self.name, status, json.dumps(obj)[:200]))
                return False
            self.sid = headers.get("mcp-session-id") or None
            self.stateless = self.sid is None
            self.init_result = obj["result"]
            self.ready = True
            note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
            self._request(note, timeout=60)
            log("session up: %s (%s)" % (self.name, self.sid or "stateless"))
            return True

    def rpc(self, method, params=None, timeout=None, retry=True):
        """Call a JSON-RPC method; heal the session once on 404."""
        if not self.ensure():
            raise RpcError("upstream %s unreachable" % self.name)
        body = json.dumps({"jsonrpc": "2.0", "id": self._next_id(),
                           "method": method, "params": params or {}}).encode()
        status, _headers, data = self._request(body, timeout=timeout or LIST_TIMEOUT)
        if status == 404 and retry and b"session not found" in (data or b"").lower():
            self.ensure(force=True)
            return self.rpc(method, params, timeout, retry=False)
        obj = _parse_body(data)
        if obj is None:
            raise RpcError("upstream %s: empty reply (HTTP %s)" % (self.name, status))
        if "error" in obj:
            err = obj["error"]
            raise RpcError(str(err.get("message") if isinstance(err, dict) else err))
        return obj.get("result") or {}

    def release(self):
        with self.lock:
            if not self.ready:
                return
            if self.sid:
                self._request(None, method="DELETE", timeout=30, with_sid=True)
                log("session released: %s (%s)" % (self.name, self.sid))
            self.sid = None
            self.ready = False


def _build_upstreams():
    raw = os.environ.get("MCP_LITE_UPSTREAMS", "").strip()
    specs = []
    if raw:
        try:
            specs = json.loads(raw)
        except Exception as e:
            log("MCP_LITE_UPSTREAMS is not valid JSON (%s); falling back" % e)
    if not specs:
        specs = [{"name": "gateway",
                  "url": os.environ.get("MCP_LITE_UPSTREAM", "http://mcp-gateway:8811/mcp"),
                  "token_env": "MCP_GATEWAY_AUTH_TOKEN"}]
    ups = []
    for s in specs:
        token = s.get("token") or ""
        if not token and s.get("token_env"):
            token = os.environ.get(s["token_env"], "")
        ups.append(Upstream(s.get("name") or "upstream", s.get("url"), token))
    return ups


UPSTREAMS = _build_upstreams()
UP_BY_NAME = {u.name: u for u in UPSTREAMS}


# --------------------------------------------------------------------------
# embeddings + lexical fallback
# --------------------------------------------------------------------------

STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "be", "with", "this", "that", "it", "my", "me", "you", "your", "i", "do",
    "does", "can", "how", "what", "which", "from", "by", "at", "as", "use",
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "da", "in",
    "con", "su", "per", "che", "chi", "cosa", "come", "dove", "quando", "mi",
    "ti", "si", "ci", "e", "ed", "o", "a", "ad", "del", "della", "dei", "delle",
    "al", "alla", "ai", "alle", "nel", "nella", "questo", "questa", "quello",
    "sono", "sei", "essere", "ho", "hai", "ha", "fare", "fai", "puoi", "vorrei",
}


def _tokens(text):
    out, cur = [], []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return [t for t in out if len(t) > 2 and t not in STOPWORDS]


def _norm(vec):
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _embed(texts):
    """Embed a list of texts through the stack's embedding-model."""
    if not EMBED_URL:
        raise RpcError("no embedding endpoint configured")
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        body = json.dumps({"model": EMBED_MODEL, "input": batch}).encode()
        req = urllib.request.Request(EMBED_URL, data=body, method="POST")
        req.add_header("content-type", "application/json")
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
        data = payload.get("data") or []
        if len(data) != len(batch):
            raise RpcError("embedding count mismatch (%d != %d)" % (len(data), len(batch)))
        out.extend(item["embedding"] for item in data)
    return out


# --------------------------------------------------------------------------
# catalogue: tools from every upstream + semantic index
# --------------------------------------------------------------------------

def _doc(tool):
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    desc = " ".join((tool.get("description") or "").split())
    if props:
        return "%s: %s (parameters: %s)" % (tool["name"], desc[:600], ", ".join(props))
    return "%s: %s" % (tool["name"], desc[:600])


def _wrap(fn, up, results):
    """Record a per-upstream refresh failure instead of losing the thread."""
    try:
        fn(up)
    except Exception as e:
        results[up.name] = ("err", e)


class Catalog:
    def __init__(self, upstreams):
        self.upstreams = upstreams
        self.lock = threading.Lock()
        self.tools = []
        self.by_name = {}
        self.vecs = []
        self.semantic = False
        self.built_at = 0.0          # when the index was (re)built
        self.checked_at = 0.0        # when the upstreams were last read
        self._embed_failed_at = 0.0
        self._last_try = 0.0
        self._building = False
        self._build_lock = threading.Lock()
        self._digest = None

    # -- persistence ---------------------------------------------------
    def _load_cache(self, digest):
        try:
            with open(CACHE_PATH, encoding="utf-8") as fh:
                cached = json.load(fh)
        except Exception:
            return False
        if cached.get("hash") != digest:
            return False
        tools = cached.get("tools") or []
        vecs = cached.get("vecs") or []
        if not tools or len(vecs) != len(tools):
            return False
        with self.lock:
            self.tools = tools
            self.by_name = {t["name"]: t for t in tools}
            self.vecs = vecs
            self.semantic = True
            self.built_at = time.time()
        log("index loaded from cache: %d tools (semantic)" % len(tools))
        return True

    def _save_cache(self, digest):
        with self.lock:
            payload = {"hash": digest, "ts": int(time.time()),
                       "tools": self.tools,
                       "vecs": [[round(x, 6) for x in v] for v in self.vecs]}
        tmp = CACHE_PATH + ".tmp"
        try:
            os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, CACHE_PATH)
            log("index cached to %s" % CACHE_PATH)
        except Exception as e:
            log("could not cache the index: %s" % e)

    # -- build ---------------------------------------------------------
    def refresh(self):
        """Fetch tools/list from every upstream and (re)build the index.

        Idempotent by construction: the catalogue is DERIVED from what the
        upstreams answer right now, never accumulated. A tool added upstream
        (new `--servers` entry in the gateway, a new MCP server in the
        catalogue, a new Home Assistant entity exposed by its MCP endpoint, a
        new tool inside an existing server) shows up at the next refresh; one
        that disappears drops out of the index and out of `by_name`, and
        `tool_call` starts answering "unknown tool" for it instead of hanging.
        No state to migrate, no restart, nothing to remember.

        The one exception is an upstream that FAILS the request: it keeps the
        tools it had before instead of losing them to a transient error, so a
        gateway restart does not empty a third of the catalogue.
        """
        # One thread per upstream, so a hung server costs REFRESH_TIMEOUT once
        # instead of once per upstream, and never blocks the others.
        results = {}

        def _list(up):
            params, pages, batch = {}, 0, []
            while pages < 40:
                result = up.rpc("tools/list", params, timeout=REFRESH_TIMEOUT)
                batch.extend(result.get("tools") or [])
                cursor = result.get("nextCursor")
                pages += 1
                if not cursor:
                    break
                params = {"cursor": cursor}
            results[up.name] = ("ok", batch)

        threads = []
        for up in self.upstreams:
            th = threading.Thread(target=_wrap, args=(_list, up, results),
                                  name="list-" + up.name, daemon=True)
            th.start()
            threads.append((up, th))
        deadline = time.time() + REFRESH_TIMEOUT + 5
        for up, th in threads:
            th.join(max(0.1, deadline - time.time()))

        fresh, ok, failed = [], set(), []
        for up in self.upstreams:
            state, payload = results.get(up.name, ("err", "timed out"))
            if state != "ok":
                log("tools/list failed on %s: %s (keeping its previous tools)"
                    % (up.name, payload))
                failed.append(up.name)
                continue
            ok.add(up.name)
            for raw in payload:
                name = raw.get("name")
                if not name:
                    continue
                fresh.append({"name": name,
                              "server": up.name,
                              "description": " ".join((raw.get("description") or "").split()),
                              "inputSchema": raw.get("inputSchema") or
                              {"type": "object", "properties": {}}})

        with self.lock:
            previous = list(self.tools)

        # Stale-but-kept tools first (failed upstreams), then the fresh ones,
        # deduplicated by name: an upstream that came back wins.
        tools, seen = [], {}
        for t in previous:
            if t["server"] in failed and t["name"] not in seen:
                seen[t["name"]] = t["server"]
                tools.append(t)
        for t in fresh:
            if t["name"] in seen:
                log("duplicate tool %r from %s (kept %s)" % (t["name"], t["server"], seen[t["name"]]))
                continue
            seen[t["name"]] = t["server"]
            tools.append(t)

        if not fresh and not tools:
            log("refresh produced no tools; keeping the previous catalogue")
            return False
        if not fresh:
            log("no upstream answered; catalogue unchanged (%d tools)" % len(tools))
            return False

        digest = hashlib.sha256(json.dumps(
            sorted([t["name"], t["description"], t["server"]] for t in tools)).encode()).hexdigest()

        with self.lock:
            prev = {t["name"]: t for t in self.tools}
            self.tools = tools
            self.by_name = {t["name"]: t for t in tools}
            changed = digest != getattr(self, "_digest", None)
            self._digest = digest
            # The upstreams were read, whether or not the index needed a
            # rebuild: that is what staleness is measured against.
            self.checked_at = time.time()
        if not changed and self.semantic and self.vecs:
            return True
        if self._load_cache(digest):
            return True

        log("building index for %d tools (%d new)" % (len(tools), sum(1 for t in tools if t["name"] not in prev)))
        docs = [_doc(t) for t in tools]
        try:
            vecs = [_norm(v) for v in _embed(docs)]
        except Exception as e:
            log("embedding unavailable (%s): lexical fallback only" % e)
            with self.lock:
                self.vecs = []
                self.semantic = False
                self.built_at = time.time()
            self._embed_failed_at = time.time()
            return True
        with self.lock:
            self.vecs = vecs
            self.semantic = True
            self.built_at = time.time()
        log("index built: %d tools, %d dims" % (len(vecs), len(vecs[0]) if vecs else 0))
        self._save_cache(digest)
        return True

    def _stale(self):
        """(is the catalogue stale?, has a refresh been tried too recently?)"""
        now = time.time()
        with self.lock:
            age = now - self.checked_at
            empty = not self.tools
            retry_embed = (not self.semantic) and (now - self._embed_failed_at > 300)
            too_soon = (now - self._last_try) < REFRESH_MIN_GAP
        return (age > REFRESH_TTL or empty or retry_embed), too_soon

    def _run_refresh(self):
        """One refresh at a time, whichever thread asked for it."""
        if not self._build_lock.acquire(blocking=False):
            return False
        self._building = True
        try:
            return self.refresh()
        except Exception as e:
            log("background refresh error: %s" % e)
            return False
        finally:
            self._building = False
            self._build_lock.release()

    def maybe_refresh(self):
        """Refresh when stale, when the catalogue is empty, or when the index
        could only be built lexically (the embeddings may be back by now)."""
        wanted, too_soon = self._stale()
        if not wanted or too_soon:
            return
        self._last_try = time.time()
        self._run_refresh()

    def maybe_refresh_async(self):
        """Kick a refresh in the background when the catalogue may be stale.

        Never blocks the caller: tools_search and tools/list must stay fast even
        while an upstream is hanging. A refresh already in flight is reused.
        """
        if self._building:
            return False
        wanted, too_soon = self._stale()
        if not wanted or too_soon:
            return False
        self._last_try = time.time()
        threading.Thread(target=self._run_refresh, name="catalog-refresh",
                         daemon=True).start()
        return True

    def _force(self):
        """Force a refresh now (admin endpoint / deploy hook / watcher)."""
        with self.lock:
            self.checked_at = 0.0
            self._last_try = 0.0
        return self._run_refresh()

    def background_refresh(self):
        self._last_try = time.time()
        self._run_refresh()

    # -- search --------------------------------------------------------
    def _lexical(self, query, tools):
        qt = _tokens(query)
        if not qt:
            return [0.0] * len(tools)
        df = {}
        docs = []
        for t in tools:
            d = _tokens(t["name"] + " " + t["description"])
            docs.append(d)
            for w in set(d):
                df[w] = df.get(w, 0) + 1
        n = len(docs) or 1
        idf = {w: math.log(1.0 + n / (1.0 + df.get(w, 0))) for w in set(qt)}
        scores = []
        for t, d in zip(tools, docs):
            have = {}
            for w in d:
                have[w] = have.get(w, 0) + 1
            s = 0.0
            for w in qt:
                if w in have:
                    weight = idf.get(w, 1.0)
                    if w in _tokens(t["name"]):
                        weight *= 3.0
                    s += weight * (1.0 + math.log(have[w]))
            scores.append(s)
        return scores

    def search(self, query, k=TOP_K, names_only=False):
        # A tool may have appeared or gone since the last build: look at the
        # upstreams again, in the background, without delaying this answer.
        self.maybe_refresh_async()
        with self.lock:
            tools = list(self.tools)
            vecs = list(self.vecs)
            semantic = self.semantic
        if not tools:
            return {"catalog_size": 0, "matched": [], "error": "catalogue empty"}

        scores = None
        if semantic and vecs:
            try:
                qv = _norm(_embed([query])[0])
                scores = [sum(a * b for a, b in zip(qv, v)) for v in vecs]
            except Exception as e:
                log("query embedding failed (%s): lexical fallback" % e)
        if scores is None:
            scores = self._lexical(query, tools)

        order = sorted(range(len(tools)), key=lambda i: -scores[i])
        if names_only:
            picked = order[:NAMES_ONLY_LIMIT]
            return {"catalog_size": len(tools),
                    "mode": "names",
                    "tools": ["%s (%s) - %s" % (tools[i]["name"], tools[i]["server"],
                                                tools[i]["description"][:110]) for i in picked]}
        picked = order[:max(1, k)]
        rest = order[max(1, k):max(1, k) + OTHER_NAMES]
        return {"catalog_size": len(tools),
                "servers": sorted({t["server"] for t in tools}),
                "matched": [{"name": tools[i]["name"],
                             "server": tools[i]["server"],
                             "description": tools[i]["description"],
                             "inputSchema": tools[i]["inputSchema"]} for i in picked],
                "other_matches": [tools[i]["name"] for i in rest]}


CATALOG = Catalog(UPSTREAMS)


# --------------------------------------------------------------------------
# the constant tool surface
# --------------------------------------------------------------------------

def meta_tools():
    n = len(CATALOG.tools)
    servers = sorted({t["server"] for t in CATALOG.tools})
    where = ", ".join(servers) if servers else "the MCP backends"
    count = ("%d tools are available (from: %s). " % (n, where)) if n else ""
    return [
        {
            "name": "tools_search",
            "description": (
                "Search the MCP tool catalogue. " + count +
                "Pass the task in natural language; returns the FULL JSON schema of the "
                "k best matching tools plus the names of other close matches. ALWAYS call "
                "this before acting: the schemas are not in the prompt, so you must not "
                "guess a tool name or its arguments. Use names_only=true to browse the "
                "whole catalogue by name."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "what you need to do, in natural language"},
                    "k": {"type": "integer", "minimum": 1, "maximum": 8, "default": TOP_K,
                          "description": "how many full schemas to return (default %d)" % TOP_K},
                    "names_only": {"type": "boolean", "default": False,
                                   "description": "return only a name+one-line list instead of full schemas"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "tool_call",
            "description": (
                "Execute any tool from the catalogue by its exact name, with a JSON "
                "arguments object that matches the schema returned by tools_search. "
                "The result is returned verbatim (text, images, structured content)."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "exact tool name"},
                    "arguments": {"type": "object",
                                  "description": "arguments object for that tool"},
                },
                "required": ["name"],
            },
        },
    ]


def _text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def _run_tool(name, arguments):
    """Execute a real tool on its upstream; return the raw MCP result."""
    tool = CATALOG.by_name.get(name)
    if tool is None:
        sugg = CATALOG.search(name, k=3)
        names = [m["name"] for m in sugg.get("matched", [])]
        raise RpcError("unknown tool %r. Closest names: %s. Use tools_search first."
                       % (name, ", ".join(names) or "none"))
    up = UP_BY_NAME.get(tool["server"])
    if up is None:
        raise RpcError("upstream %r for %r is not configured" % (tool["server"], name))
    return up.rpc("tools/call", {"name": name, "arguments": arguments}, timeout=CALL_TIMEOUT)


def handle_tool_call(name, arguments):
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except Exception:
            raise RpcError("arguments must be a JSON object")
    if not isinstance(arguments, dict):
        arguments = {}

    if name == "tools_search":
        query = arguments.get("query") or arguments.get("q") or ""
        if not isinstance(query, str) or not query.strip():
            raise RpcError("tools_search needs a non-empty 'query'")
        try:
            k = int(arguments.get("k") or TOP_K)
        except Exception:
            k = TOP_K
        k = max(1, min(8, k))
        res = CATALOG.search(query, k=k, names_only=bool(arguments.get("names_only")))
        return _text_result(json.dumps(res, ensure_ascii=False))

    if name == "tool_call":
        target = arguments.get("name") or arguments.get("tool")
        if not target:
            raise RpcError("tool_call needs 'name' (the tool to execute)")
        inner = arguments.get("arguments")
        if inner is None:
            inner = arguments.get("args") or {}
        if isinstance(inner, str):
            # Models sometimes hand back the arguments as a JSON string
            # instead of an object (llama.cpp templates are lenient). Upstreams
            # are not: a string reached chroma and hung the call for 3 minutes.
            try:
                inner = json.loads(inner or "{}")
            except Exception:
                raise RpcError("'arguments' must be a JSON object")
        if not isinstance(inner, dict):
            inner = {}
        return _run_tool(target, inner)

    # A client that still has a stale list can call a real tool directly.
    return _run_tool(name, arguments)


# --------------------------------------------------------------------------
# idle release: let the gateway stop the children it keeps per session
# --------------------------------------------------------------------------

_lock = threading.Lock()
_inflight = 0
_last_activity = time.monotonic()


def _touch():
    global _last_activity
    with _lock:
        _last_activity = time.monotonic()


def _begin_request():
    global _inflight, _last_activity
    with _lock:
        _inflight += 1
        _last_activity = time.monotonic()


def _end_request():
    global _inflight, _last_activity
    with _lock:
        _inflight -= 1
        _last_activity = time.monotonic()


def _idle_sweeper():
    while True:
        time.sleep(IDLE_SWEEP)
        try:
            if IDLE_TTL <= 0:
                continue
            with _lock:
                if _inflight > 0 or time.monotonic() - _last_activity < IDLE_TTL:
                    continue
            for up in UPSTREAMS:
                try:
                    up.release()
                except Exception as e:
                    log("release %s failed: %s" % (up.name, e))
        except Exception as e:
            log("idle sweeper error: %s" % e)


def _catalog_watcher():
    """Keep the catalogue in step with the upstreams without spawning work.

    Only runs when every upstream session is already alive (i.e. a client is
    using the stack): refreshing means `tools/list`, which the gateway serves
    by talking to each child container, so polling an idle stack would start
    Chromium for nothing. When nobody is connected the catalogue is refreshed
    on the next client `tools/list` anyway.

    This is the poll that makes a removed MCP server disappear on its own, so
    it forces the refresh instead of waiting for REFRESH_TTL; the digest makes
    an unchanged catalogue free (no re-embedding, no cache write).
    """
    while True:
        time.sleep(max(120.0, min(600.0, REFRESH_TTL / 4 or 600.0)))
        try:
            if not all(u.ready for u in UPSTREAMS):
                continue
            log("catalogue watcher: re-reading tools/list")
            CATALOG._force()
        except Exception as e:
            log("catalog watcher error: %s" % e)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(fmt % args)

    def _send(self, status, body=b"", ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body or b"")))
        if status != 200 or ctype == "application/json":
            self.send_header("Mcp-Session-Id", STABLE_SID)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, mid, result=None, error=None):
        payload = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        self._send(200, json.dumps(payload, ensure_ascii=False).encode())

    def _authorized(self):
        if not AUTH_TOKEN:
            return True
        got = (self.headers.get("Authorization") or "").strip()
        return hmac.compare_digest(got.encode(), ("Bearer " + AUTH_TOKEN).encode())

    def do_DELETE(self):
        if not self._authorized():
            self._send(401, b'{"error":"unauthorized"}')
            return
        # The client closing ITS session must not close ours: other clients
        # may share it. The idle sweeper owns the lifecycle.
        self._send(204)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        # /health stays open: it is the container healthcheck and it carries
        # counts, not data. Everything else needs the token.
        if path != "/health" and not self._authorized():
            self._send(401, b"unauthorized", "text/plain")
            return
        if path == "/health":
            tools = len(CATALOG.tools)
            body = json.dumps({
                "ok": tools > 0,
                "tools": tools,
                "semantic": CATALOG.semantic,
                "index_age_s": int(time.time() - CATALOG.built_at) if CATALOG.built_at else None,
                "checked_age_s": (int(time.time() - CATALOG.checked_at)
                                  if CATALOG.checked_at else None),
                "digest": (CATALOG._digest or "")[:12],
                "servers": sorted({t["server"] for t in CATALOG.tools}),
                "upstreams": [{"name": u.name, "session": bool(u.sid),
                               "stateless": u.stateless, "ready": u.ready}
                              for u in UPSTREAMS]}).encode()
            self._send(200, body)
            return
        if path == "/refresh":
            # Idempotent admin op: re-read tools/list from every upstream and
            # rebuild the index if the catalogue changed. Safe to call anytime
            # (deploy hook, after adding an MCP server, after an HA reload).
            CATALOG._force()
            self._send(200, json.dumps({"ok": True,
                                        "tools": len(CATALOG.tools),
                                        "digest": (CATALOG._digest or "")[:12]}).encode())
            return
        # Streamable-HTTP GET: relay the first upstream's SSE stream verbatim.
        up = UPSTREAMS[0]
        sid = up.ensure()
        if sid is None:
            self._send(502, b"upstream unavailable", "text/plain")
            return
        req = urllib.request.Request(up.url, method="GET")
        req.add_header("Accept", "text/event-stream")
        req.add_header("Mcp-Session-Id", sid)
        if up.token:
            req.add_header("Authorization", "Bearer " + up.token)
        try:
            resp = urllib.request.urlopen(req, timeout=3600)
        except Exception as e:
            self._send(502, str(e).encode(), "text/plain")
            return
        self.send_response(resp.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Mcp-Session-Id", STABLE_SID)
        self.end_headers()
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
        except Exception:
            raw = b""
        if not self._authorized():
            self._send(401, b'{"error":"unauthorized"}')
            return
        try:
            msg = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except Exception:
            self._send(400, b'{"error":"invalid json"}')
            return
        if isinstance(msg, list):  # JSON-RPC batch: not used by MCP clients here
            msg = msg[0] if msg else {}

        method = msg.get("method") or ""
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            self._json(mid, {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mcp-tools-lite", "version": "1.0"},
                "instructions": ("This server exposes the whole MCP catalogue behind two tools: "
                                 "tools_search and tool_call. Call tools_search with the task "
                                 "before using any tool."),
            })
            return

        if method.startswith("notifications/"):
            self._send(202)
            return

        _touch()

        if method == "ping":
            self._json(mid, {})
            return

        if method == "tools/list":
            self._json(mid, {"tools": meta_tools()})
            CATALOG.maybe_refresh_async()
            return

        if method == "tools/call":
            name = params.get("name") or ""
            args = params.get("arguments")
            _begin_request()
            try:
                result = handle_tool_call(name, args)
            except RpcError as e:
                result = _text_result("ERROR: %s" % e, is_error=True)
            except Exception as e:
                log("tool %s failed: %s" % (name, e))
                result = _text_result("ERROR: %s" % e, is_error=True)
            finally:
                _end_request()
            self._json(mid, result)
            return

        if method in ("prompts/list", "resources/list", "resources/templates/list"):
            empty = {"prompts": [], "resources": [], "resourceTemplates": []}
            key = {"prompts/list": "prompts", "resources/list": "resources",
                   "resources/templates/list": "resourceTemplates"}[method]
            self._json(mid, {key: empty[key]})
            return

        # Anything else (logging/setLevel, completion/complete, ...) goes to
        # the first upstream, which is the full gateway.
        up = UPSTREAMS[0]
        _begin_request()
        try:
            result = up.rpc(method, params, timeout=LIST_TIMEOUT)
        except RpcError as e:
            self._json(mid, error={"code": -32603, "message": str(e)})
            return
        finally:
            _end_request()
        self._json(mid, result)


def main():
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    srv.daemon_threads = True
    log("listening on %s:%d (upstreams: %s)"
        % (LISTEN_HOST, LISTEN_PORT, ", ".join(u.name for u in UPSTREAMS)))
    log("embedding endpoint: %s (%s)" % (EMBED_URL or "none", EMBED_MODEL))
    threading.Thread(target=CATALOG.background_refresh, name="catalog-boot", daemon=True).start()
    threading.Thread(target=_catalog_watcher, name="catalog-watcher", daemon=True).start()
    if IDLE_TTL > 0:
        threading.Thread(target=_idle_sweeper, name="idle-sweeper", daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
