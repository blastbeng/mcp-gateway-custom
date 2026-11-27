#!/usr/bin/env python3
"""
MCP "tools-lite" gateway: two constant meta-tools in front of N MCP servers.

Why this exists
===============
An MCP server advertises every tool it owns in `tools/list`, and the client
re-sends that list to the model on EVERY turn, at the top of the prompt (the
tools block renders inside the system area of the chat template).

This stack advertises 109 tools from the docker-mcp gateway. Measured on this
surface: ~107k characters of JSON schema, i.e. 33k-47k tokens. On a cloud
model that is a cost. On the cheap LLM node (Orange Pi Zero 3W: 12.288 tokens
of context, 20-35 tok/s of prefill) it is impossible - a 40k-token prompt
needs ~35 minutes to prefill and does not fit at all.

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

Result: ~250 tokens instead of ~33k, all 109 tools remain reachable, and no
client needs to change: this is just another MCP server.

The model cannot see the catalogue, so it cannot know what to search for. The
`tools_search` description therefore carries a CAPABILITY MAP: the domains
behind the proxy with the tool-name patterns of each, built from the live
catalogue and capped (`MCP_LITE_MAP_CHARS`). A domain label is curated
(`chroma_*` is semantic memory - the tool names do not say so); a tool no
label claims is listed under "other" by name, so a server added to the gateway
appears in the map without touching this file. A few hundred characters, and
the map changes only when the catalogue changes.

Deliberately stdlib only (no `mcp` SDK): it must start in a 128 MB python
container and never depend on PyPI at boot.

Upstreams
=========
`MCP_LITE_UPSTREAMS` is a JSON array; each entry needs a name and a url, and
either `token` or `token_env` (read from the environment):

  [{"name":"gateway","url":"http://mcp-gateway:8811/mcp",
    "token_env":"MCP_GATEWAY_AUTH_TOKEN"},
   {"name":"other","url":"http://host.docker.internal:9000/mcp",
    "token_env":"MCP_LITE_OTHER_TOKEN"}]

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

import fnmatch
import hashlib
import hmac
import json
import math
import os
import re
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

# --------------------------------------------------------------------------
# shortcut surface: curated flat tools next to the two meta-tools
# --------------------------------------------------------------------------
# The two meta-tools are the right surface for a capable model: one lookup, one
# call, and the whole catalogue stays reachable. A 1.5B-active MoE cannot do
# that indirection. Measured on the cheap node (LFM2.5-8B-A1B, Open WebUI
# payload, same system message and tools):
#
#   meta-tools only (find_tool/run_tool) -> hop 1 emits find_tool 4/5, but the
#       full find_tool -> run_tool -> answer chain is 1/3 usable (LOOP, LOOP,
#       empty answer): find_tool("Gerry Scotti") ranks `ask` and answers 14 kB;
#   + flat web_search(query)             -> the model calls web_search 5/5 with
#       the right arguments, 12-16 s, and writes the answer from the snippets.
#
# So the flat tools are a SHORTCUT, not a replacement: the same single surface
# advertises the everyday tools with curated minimal schemas AND find_tool/
# run_tool, which keep all 109 tools reachable. A tool named in
# MCP_LITE_FLAT_TOOLS without a hand-written schema gets a compact one derived
# from the catalogue (name + trimmed description + typed parameters), because
# the upstream schema of e.g. find_tool's own result is 1.4-3 kB per tool.
#
# Second hop, answering from the result: the upstream schema is also too much.
# With the raw 1.5 kB web_search JSON the model returns an EMPTY answer at
# reasoning budget 96-128 (0/3, 1/3); with two or three compact
# "title / url / snippet" lines it answers 3/3 at budget 256. So a flat tool
# here means: a curated MINIMAL schema (query + count only - the optional
# `freshness`/`site` fields were measured to be invented as `freshness:"day"`,
# `site:"italiano"`, which then filters out the answer) and a compact render of
# the result. `run_tool` keeps returning the payload verbatim.
#
# Off by default: with MCP_LITE_FLAT_TOOLS empty the surface stays the two
# meta-tools, so other clients of this proxy are untouched.
FLAT_TOOLS = [n.strip() for n in os.environ.get("MCP_LITE_FLAT_TOOLS", "").split(",") if n.strip()]
META_TOOLS_ENABLED = os.environ.get("MCP_LITE_META_TOOLS", "1") != "0"
# An auto-generated description, not the upstream one: the catalogue's longest
# descriptions run to 700+ chars and the model only needs to recognise the tool.
FLAT_AUTO_DESC = int(os.environ.get("MCP_LITE_FLAT_AUTO_DESC", "200"))
FLAT_PARAM_DESC = int(os.environ.get("MCP_LITE_FLAT_PARAM_DESC", "90"))
# Four results of 300 chars, not three of 220: the snippets are what the model
# answers from, and a too-short snippet is exactly why it decides it needs the
# whole page (and then reads page chrome, see _readable below).
# The deployment runs 5 results of 500 chars (measured on Open WebUI: a 300-char
# snippet of the Gerry Scotti Wikipedia lead cut off before the fact being asked
# for, so the model read the page anyway — one hop it did not need). The surface
# pays ~350 chars of schema-free output per call for it, which is one hop less.
FLAT_MAX_RESULTS = int(os.environ.get("MCP_LITE_FLAT_MAX_RESULTS", "4"))
FLAT_SNIPPET = int(os.environ.get("MCP_LITE_FLAT_SNIPPET", "300"))
# The memory renderer keeps its own sizes: widening the search knobs above must
# not silently double what a memory call returns.
MEMORY_MAX_RESULTS = int(os.environ.get("MCP_LITE_MEMORY_MAX_RESULTS", "4"))
MEMORY_SNIPPET = int(os.environ.get("MCP_LITE_MEMORY_SNIPPET", "600"))

# Reading a page: the upstream converts HTML to markdown but keeps the chrome,
# so on it.wikipedia.org the first 2 kB are "Vai al contenuto", "Menu
# principale", the language list and the search box. A model that asks for 2000
# chars therefore reads navigation and asks for the next page — measured on
# Open WebUI chat 1f7d4872: three tool turns of 55 s, then a turn with no
# content left (its whole thinking budget spent planning another fetch).
# So the flat tool asks upstream for a much larger window than the caller
# wanted, keeps the article, and only then truncates to the caller's size.
URL_READ_DEFAULT = int(os.environ.get("MCP_LITE_URL_READ_DEFAULT", "4000"))
URL_READ_WINDOW = int(os.environ.get("MCP_LITE_URL_READ_WINDOW", "60000"))
URL_READ_MIN_KEEP = int(os.environ.get("MCP_LITE_URL_READ_MIN_KEEP", "400"))

FLAT_SPECS = {
    "web_search": {
        "name": "web_search",
        # The description is the only system prompt this surface has (the router
        # deliberately injects none), so it carries the instruction. Measured on
        # the production sentence "cerca sul web chi è Gerry Scotti": the softer
        # "use it when the user asks about a person" wording answered from memory
        # in 1 of 3 runs (and got the person wrong), so this one says what to do,
        # forbids answering from memory, and lists the trigger words.
        "description": ("Search the public web right now. Call this tool whenever the user "
                        "asks you to search something (cerca, cercami, search, look up, "
                        "find), or asks about a person, a company, news, prices or any fact "
                        "you are not sure of. Never answer such a question from memory: the "
                        "snippets are the only source of truth. Returns title, URL and "
                        "snippet for the best results. Answer from these snippets: open a "
                        "page only when one is not enough, and then call search_and_read "
                        "once (it searches and reads in a single call)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "search keywords, e.g. 'Gerry Scotti biografia'"},
                "count": {"type": "integer", "minimum": 1, "maximum": 8,
                          "default": 3, "description": "how many results (default 3)"},
            },
            "required": ["query"],
        },
    },
    # send_message: the upstream schema has a `chatId` override, and the MoE
    # filled it with `"blast"` every time. The chat is fixed by the gateway, so
    # the flat schema does not offer the field at all.
    "send_message": {
        "description": ("Send a text message to the user on Telegram. Call it when the "
                        "user asks to send, notify or message something. The chat is "
                        "fixed: pass only the text."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "message text to send"},
            },
            "required": ["text"],
        },
    },
    "web_url_read": {
        # The page is fetched and then stripped of menus, navigation and footers
        # (_readable), so what comes back starts with the article. One call is
        # enough for a fact: read it, then answer.
        "description": ("Fetch a web page and return its article text (menus and navigation "
                        "already removed). Call it when the user gives a URL, or when a "
                        "search result is only a snippet and the answer needs the whole "
                        "page. One call is normally enough: read the result and answer."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "absolute URL to read"},
                "maxLength": {"type": "integer",
                              "description": "max characters to return (default 4000)"},
            },
            "required": ["url"],
        },
    },
    # search_and_read: this proxy's own tool (not in the catalogue), implemented
    # by running web_search and then web_url_read on the best hit. On the cheap
    # node a search and a page read are two model hops of ~30 s each (measured
    # on Open WebUI chat 0407eb2f: reasoning 11 s + search 1.8 s + reasoning
    # 28 s + reasoning 30 s + read 3 s + reasoning 30 s + answer, 252 s for
    # "chi è Gerry Scotti"). One tool, one hop, and the answer is already in the
    # result, so the model has nothing left to plan.
    "search_and_read": {
        "description": ("Search the public web and return the top result's article text in "
                        "one call (menus and navigation already removed), plus the other "
                        "results as one line each. Call it for any factual question the user "
                        "asks you to look up: it answers the search and the page read at "
                        "once, so you do not need web_search or web_url_read first. Prefer "
                        "it over searching and then reading a page in two calls."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "search keywords, e.g. 'Gerry Scotti biografia'"},
                "url": {"type": "string",
                        "description": "optional: read this URL instead of the top result"},
                "maxLength": {"type": "integer",
                              "description": "max characters of article text (default 4000)"},
            },
            "required": ["query"],
        },
    },
    # chroma_query_documents: upstream wants `collection_name` + an array of
    # query texts. A model that has to name a Chroma collection gets it wrong or
    # asks `find_tool` instead (0/3 on "cerca nella memoria del progetto"), so
    # the flat schema takes a plain `query` and the adapter below fills in the
    # collection configured for this deployment.
    "chroma_query_documents": {
        "description": ("Search the project's long-term memory (Chroma). Call it when the "
                        "user asks what was decided, known or done before about a topic."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "what to look for"},
                "n_results": {"type": "integer", "default": 3,
                              "description": "how many notes (default 3)"},
            },
            "required": ["query"],
        },
    },
}

# Collection the flat `chroma_query_documents` reads. Set per deployment: the
# cheap surface serves one project, and its collection name is the one thing the
# model cannot guess.
MEMORY_COLLECTION = os.environ.get("MCP_LITE_MEMORY_COLLECTION", "")


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
        streamable HTTP); a stateless server answers 200 with no header. A
        stateless upstream is usable without a session id; requiring one
        silently drops every tool it owns.
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


# A tool is often named by intent ("search the web") while its description is
# written in implementation words ("Runs headless Chromium against DuckDuckGo,
# then Bing, Marginalia..."). Cosine similarity then hands the query to a
# neighbouring tool with a chattier description -- measured: 4/15 correct top-1.
# This table maps intent words to the tool that answers them. It is server-side,
# so it costs ZERO prompt tokens: the index is unchanged, only the ranking moves.
# Measured with the blend below: 13/15 top-1 on the same queries.
ALIASES = {
    "web_search": ("search", "web", "news", "latest", "recent", "today", "current",
                   "internet", "google", "look up", "lookup", "online", "trending",
                   "cerca", "cercare", "notizie", "ultime", "oggi", "attualita",
                   "meteo", "weather", "prezzo", "price", "chi e", "who is",
                   "quando e", "when did", "how much"),
    "web_url_read": ("url", "http", "website", "web page", "read this link",
                     "open the link", "fetch the page", "articolo", "article"),
    "send_message": ("telegram", "send message", "send a message", "notify",
                     "notification", "invia messaggio", "messaggio su telegram"),
    "chroma_query_documents": ("memory", "memoria", "remember", "ricorda", "recall",
                               "preference", "preferenza", "knowledge base",
                               "memoria semantica", "what did i"),
    "chroma_add_documents": ("store in memory", "save to memory", "memorizza",
                             "salva in memoria", "salva questa preferenza",
                             "store a preference"),
    "browser_navigate": ("browser", "navigate", "open the page", "playwright",
                         "apri il sito"),
    "browser_click": ("click", "button", "submit", "form", "clicca"),
    "take_screenshot": ("screenshot", "schermata", "capture the page"),
    "klaket_ingest": ("audio", "video", "transcribe", "trascrivi", "youtube",
                      "podcast", "sottotitoli"),
    "read_image": ("image", "immagine", "photo", "foto", "picture", "ocr",
                   "chart", "grafico", "read the text from"),
    "ask": ("opinion", "parere", "second opinion", "another model", "council",
            "ask another llm", "consiglio"),
    "resolve-library-id": ("library docs", "documentation", "api reference",
                           "context7", "documentazione"),
}
ALIAS_WEIGHT = float(os.environ.get("MCP_LITE_ALIAS_WEIGHT", "0.08"))
# Small lexical top-up: intent words ("news", "telegram") that the embedding
# underweights still earn a bonus, scaled to the query's own maximum so it never
# depends on catalogue size. 0 disables it.
LEXICAL_WEIGHT = float(os.environ.get("MCP_LITE_LEXICAL_WEIGHT", "0.05"))


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
# capability map: what KIND of tools sit behind this proxy
# --------------------------------------------------------------------------
#
# The model sees two meta-tools and no catalogue, so it has no idea what is
# worth searching for. Naming the DOMAINS behind the proxy, with the tool-name
# patterns of each, is what tells it "there is a browser here, a vector
# memory, a web search". The map rides on the `tools_search` description and
# on `initialize`, so it costs a few hundred characters per turn instead of
# the 33k-47k tokens of the full surface.
#
# It is built FROM the live catalogue, not hard-coded, so it cannot rot:
#
#   * the labels are curated, because a human knows `chroma_*` is semantic
#     memory while the tool names do not say so;
#   * a domain that has no tools left drops out of the map (a removed server
#     spends no tokens);
#   * a tool no label claims is appended under "other", by name, so a server
#     added to the gateway shows up in the map with no edit in this file;
#   * the string is memoized on the catalogue digest, so it changes only when
#     the catalogue changes: the tool surface stays constant inside a
#     conversation, which is what keeps llama.cpp's prefix cache warm.
#
# `match` entries are fnmatch patterns over the tool name; `show` is the
# compact text printed for the domain (default: its patterns), written when
# the patterns are too many to spell out. `MCP_LITE_DOMAINS` (JSON list) is
# matched BEFORE these defaults, so a deployment can re-label, split or shadow
# a domain without touching the code.

MAP_CHARS = int(os.environ.get("MCP_LITE_MAP_CHARS", "760"))
OTHER_IN_MAP = int(os.environ.get("MCP_LITE_MAP_OTHER_NAMES", "8"))

DEFAULT_DOMAINS = [
    {"label": "browser automation (Playwright + Chrome DevTools)",
     "match": ["browser_*",
               "new_page", "navigate_page", "select_page", "list_pages", "close_page",
               "resize_page", "emulate", "click", "hover", "drag", "fill", "fill_form",
               "type_text", "press_key", "upload_file", "handle_dialog", "wait_for",
               "evaluate_script", "set_logpoints", "take_snapshot", "take_screenshot",
               "take_heapsnapshot", "list_console_messages", "get_console_message",
               "list_network_requests", "get_network_request", "get_css_styles",
               "performance_start_trace", "performance_stop_trace",
               "performance_analyze_insight", "lighthouse_audit", "screencast_start",
               "screencast_stop"],
     "show": "browser_* / navigate_page, click, fill, take_screenshot, "
             "console+network, traces, lighthouse_audit"},
    {"label": "Tor / dark-web OSINT (SICRY)", "match": ["sicry_*"]},
    {"label": "semantic long-term memory", "match": ["chroma_*"]},
    {"label": "web search + page fetch, keyless",
     "match": ["web_search", "web_search_status", "web_url_read", "searxng_*"],
     "show": "web_search, web_url_read, searxng_*"},
    {"label": "model council (other LLMs)",
     "match": ["ask", "ask_all", "list_council", "probe_models", "revise",
               "revision_prompt"],
     "show": "ask, ask_all, list_council, probe_models, revise*"},
    {"label": "Telegram notify (one fixed bot+chat)",
     "match": ["send_message", "send_document", "send_photo", "send_video",
               "send_audio", "get_updates"],
     "show": "send_*, get_updates"},
    {"label": "media ingest + transcription", "match": ["klaket_*"]},
    {"label": "library docs", "match": ["resolve-library-id", "query-docs"]},
    {"label": "image analysis", "match": ["read_image"]},
]


def _load_domains():
    doms = [d for d in DEFAULT_DOMAINS if d.get("match")]
    raw = os.environ.get("MCP_LITE_DOMAINS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, dict):
                extra = [extra]
            extra = [d for d in extra if isinstance(d, dict) and d.get("match")]
            doms = extra + doms          # first claim wins: env shadows a default
        except Exception as e:
            log("MCP_LITE_DOMAINS is not valid JSON (%s); using the defaults" % e)
    return doms


DOMAINS = _load_domains()


def _show_of(dom, shown):
    """The patterns to print for a domain.

    Spelled out while there are few, summarized by `show` when the domain is
    built from patterns too many to list. Only patterns that matched are
    printed, so a domain that shrinks stops advertising tools it no longer
    has: the map follows the catalogue, not this file.
    """
    if len(shown) <= 3:
        return ", ".join(shown)
    return dom.get("show") or ("%s, +%d more" % (shown[0], len(shown) - 1))


def _compact_show(text):
    """The first two patterns, for when the budget is tight.

    Used only to make room: naming a server nobody has labelled yet matters
    more than spelling out a domain the model can infer from one pattern.
    """
    text = text.split(" / ")[0]
    return ", ".join([t.strip() for t in text.split(", ")][:2])


def _domain_part(dom, names, compact=False):
    """-> ('label (patterns) (n)', {claimed tools}) or (None, set()).

    The count is printed only when it says something: one tool is not a crowd.
    """
    hits, shown = set(), []
    for pat in dom.get("match") or []:
        got = {n for n in names if fnmatch.fnmatch(n, pat)}
        if got:
            hits |= got
            shown.append(pat)
    if not hits:
        return None, set()
    text = _show_of(dom, shown)
    n = len(hits)
    if compact:
        text = _compact_show(text)
    label = dom["label"].split(" (")[0] if compact else dom["label"]
    return "%s (%s)%s" % (label, text, " (%d)" % n if n > 1 else ""), hits


def _prefix_of(name):
    """`chroma_add_documents` -> `chroma`, `query-docs` -> `query`."""
    for sep in ("__", "_", "-"):
        if sep in name:
            return name.split(sep, 1)[0]
    return name


def _rest_tokens(rest):
    """Unlabelled tools, grouped by name prefix: `foo_*`, `bar_a/baz_b`.

    This is what keeps the map honest with no rebuild: a server nobody has
    labelled yet arrives with its own prefix, shows up here under that prefix,
    and needs no code edit. Only the label is curated, never the membership.
    """
    groups = {}
    for n in rest:
        groups.setdefault(_prefix_of(n), set()).add(n)
    out = []
    for p, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out.append(("%s_*" % p) if len(members) >= 3 else "/".join(sorted(members)))
    return out


_TRUNC = "…"
_UNLABELLED = "unlabelled"


def _render_map(names, servers, compact, budget):
    """The map as one string. `compact` shortens labels/patterns to make room.

    First claim wins: a domain takes the tools it matches and the rest see
    only what is left, so a tool is counted once and an entry in
    MCP_LITE_DOMAINS really does shadow the default it overlaps.
    """
    parts, claimed, pool = [], set(), set(names)
    for dom in DOMAINS:
        part, hits = _domain_part(dom, sorted(pool), compact)
        if part:
            parts.append((len(hits), part))
            claimed |= hits
            pool -= hits
    head = "%d tools from %s: " % (len(names), ", ".join(servers)) if len(servers) > 1 \
        else "%d tools: " % len(names)
    parts.sort(key=lambda p: -p[0])
    rest = sorted(n for n in names if n not in claimed)
    out, used = [], len(head)
    for _, part in parts:
        if used + len(part) + 3 > budget:
            out.append(_TRUNC)
            break
        out.append(part)
        used += len(part) + 3
    if rest:
        tokens = _rest_tokens(rest)
        tail = "other (%d): %s" % (len(rest), ", ".join(tokens[:OTHER_IN_MAP]))
        if len(tokens) > OTHER_IN_MAP:
            tail += ", +%d more" % (len(tokens) - OTHER_IN_MAP)
        # An unlabelled server is never silently dropped: when its names do not
        # fit, its count is printed, which is enough to prompt a search.
        for cand in (tail, "other (%d %s)" % (len(rest), _UNLABELLED)):
            if used + len(cand) + 3 <= budget:
                if out and out[-1] == _TRUNC:
                    out[-1] = cand
                else:
                    out.append(cand)
                break
    return head + " · ".join(out)


def build_map(tools, budget=MAP_CHARS):
    """One line: '109 tools: web search + page fetch, keyless (...) (6) · ...'

    Built from the catalogue whenever its digest changes, so a tool, a gateway
    server, an upstream, an MCP server inside the gateway added or removed
    updates the map by itself: no rebuild, no redeploy, no edit in this file.
    The biggest domains render first, so the budget cut costs the least of the
    surface; tools no label claims are named by prefix at the end, and when
    they do not fit the map is re-rendered in a compact form that makes room
    for them, because naming an unlabelled server is the point of the map.
    """
    names = [t["name"] for t in tools]
    if not names:
        return "the catalogue is empty; it is built on boot, search anyway"
    servers = sorted({t["server"] for t in tools})
    full = _render_map(names, servers, False, budget)
    # A compact render wins only when it fixes something: naming every domain,
    # or naming an unlabelled server, beats spelling one domain out in detail
    # and cutting the rest.
    unlabelled, truncated = _UNLABELLED in full, _TRUNC in full
    if unlabelled or truncated:
        compact = _render_map(names, servers, True, budget)
        if (unlabelled and _UNLABELLED not in compact) \
                or (truncated and _TRUNC not in compact):
            return compact
    return full


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
        self._map = None
        self._map_digest = "init"

    # -- capability map ------------------------------------------------
    def map_line(self):
        """The one-line 'what is behind this proxy', memoized on the digest.

        Memoizing is not an optimization here: the string rides on the tool
        description, and a description that changes mid-conversation forces a
        full re-prefill. It moves when the catalogue moves, and never
        otherwise.
        """
        with self.lock:
            digest, tools = self._digest, list(self.tools)
        if digest == self._map_digest and self._map is not None:
            return self._map
        line = build_map(tools)
        with self.lock:
            self._map_digest, self._map = digest, line
        return line

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
        catalogue, a new tool inside an existing server, a new entry in
        MCP_LITE_UPSTREAMS) shows up at the next refresh; one
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

    def _alias_boost(self, query, tools):
        """Intent-word bonus per tool (see ALIASES). Never raises, never embeds."""
        if not ALIAS_WEIGHT:
            return [0.0] * len(tools)
        ql = " ".join(query.lower().split())
        boosts = [0.0] * len(tools)
        for i, t in enumerate(tools):
            terms = ALIASES.get(t["name"])
            if not terms:
                continue
            hits = sum(1 for term in terms if term in ql)
            if hits:
                # 2+ intent words is a strong signal, but never above ~0.16 so a
                # genuinely better semantic match can still win.
                boosts[i] = min(ALIAS_WEIGHT * hits, 0.16)
        return boosts

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
        else:
            lex = self._lexical(query, tools)
            top = max(lex) or 1.0
            if LEXICAL_WEIGHT and lex:
                scores = [s + LEXICAL_WEIGHT * (l / top) for s, l in zip(scores, lex)]
        scores = [s + b for s, b in zip(scores, self._alias_boost(query, tools))]

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
    """The whole tool surface: two tools, and they never change.

    The only variable text is the capability map, which moves exactly when the
    catalogue moves (`Catalog.map_line`) and is memoized so it cannot invalidate
    the prefix cache between two turns of the same conversation.
    """
    return [
        {
            # The names are the contract. "tools_search" / "tool_call" read like
            # "use the search tool" / "call a tool maybe", and a 1.1B-active MoE
            # answered exactly that way: it searched repeatedly (round 1 and 2
            # were both searches), then stopped with an empty answer -- measured
            # as the Open WebUI hang. "find_tool" / "run_tool" name the *effect*
            # (look up a schema vs produce real data) and the same model emits
            # find_tool then run_tool on the first try. Old names still work, so
            # prompts and clients that already say tools_search keep working.
            "name": "find_tool",
            "description": (
                "Look up the schema of a tool in the catalogue. " + CATALOG.map_line() +
                ". This only RETURNS SCHEMAS: it runs nothing and contains no "
                "real-world data. After it returns, call run_tool with the best name "
                "and its arguments. ALWAYS call this before acting: the schemas are "
                "not in the prompt, so you must not guess a tool name or its "
                "arguments. The list is the DOMAINS behind this proxy, not the whole "
                "catalogue: search even when nothing there seems to fit."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The ACTION you need, in English: 'search the "
                                       "web for X', 'read this URL', 'send a telegram "
                                       "message', 'query semantic memory'. Never the "
                                       "user's question verbatim."},
                    "k": {"type": "integer", "minimum": 1, "maximum": 8, "default": TOP_K,
                          "description": "how many schemas to return (default %d)" % TOP_K},
                    "names_only": {"type": "boolean", "default": False,
                                   "description": "names + one-line summary only, no "
                                                  "schemas; you cannot run_tool from this"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "run_tool",
            "description": (
                "Execute a catalogue tool by its exact name, with a JSON arguments "
                "object that matches the schema find_tool returned. This is the only "
                "way to obtain real data: web results, page content, sent messages, "
                "memory. The result is returned verbatim (text, images, structured "
                "content)."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "exact tool name from find_tool"},
                    "arguments": {"type": "object",
                                  "description": "arguments object for that tool"},
                },
                "required": ["name"],
            },
        },
    ]


def _short(text, limit):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def _compact_spec(tool):
    """Derive a minimal schema from a catalogue entry.

    For a tool in MCP_LITE_FLAT_TOOLS that has no hand-written spec: name, a
    trimmed description, and its parameters with type/enum/default and a
    trimmed description. Upstream schemas run to 1.4-3 kB each (long prose,
    nested objects, examples); what the model needs to pick a tool and fill it
    is this.
    """
    src = tool.get("inputSchema") or {}
    props = {}
    for pname, pspec in (src.get("properties") or {}).items():
        if not isinstance(pspec, dict):
            continue
        clean = {}
        ptype = pspec.get("type")
        if isinstance(ptype, list):
            ptype = next((t for t in ptype if isinstance(t, str)), None)
        if isinstance(ptype, str) and ptype != "null":
            clean["type"] = ptype
        if pspec.get("description"):
            clean["description"] = _short(pspec["description"], FLAT_PARAM_DESC)
        if isinstance(pspec.get("enum"), list) and pspec["enum"]:
            clean["enum"] = pspec["enum"][:12]
        if isinstance(pspec.get("default"), (str, int, float, bool)):
            clean["default"] = pspec["default"]
        props[pname] = clean
    schema = {"type": "object", "properties": props}
    required = [r for r in (src.get("required") or []) if r in props]
    if required:
        schema["required"] = required
    return {
        "name": tool["name"],
        "description": _short(tool.get("description"), FLAT_AUTO_DESC) or tool["name"],
        "inputSchema": schema,
    }


def flat_tools():
    """Schemas for the tools named in MCP_LITE_FLAT_TOOLS.

    A hand-written FLAT_SPECS entry wins (it is the instruction, when there is
    room for one); otherwise a compact schema is derived from the catalogue.
    Only tools the catalogue really has are exposed: a typo in the env var must
    not advertise a tool that cannot be executed. The exception is PROXY_TOOLS,
    which this proxy implements itself and so already has a hand-written schema.
    """
    out = []
    for name in FLAT_TOOLS:
        tool = CATALOG.by_name.get(name)
        if tool is None and name not in PROXY_TOOLS:
            log("flat tool %r is not in the catalogue, skipping" % name)
            continue
        spec = dict(FLAT_SPECS.get(name) or _compact_spec(tool))
        spec["name"] = name
        out.append(spec)
    return out


_SURFACE_DIGEST = None


def _log_surface(tools):
    """Log the advertised surface once per change (it rides every request)."""
    global _SURFACE_DIGEST
    digest = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
    if digest == _SURFACE_DIGEST:
        return
    _SURFACE_DIGEST = digest
    chars = len(json.dumps(tools))
    log("surface: %d tools, %d chars of schema (%s)"
        % (len(tools), chars, ", ".join(t["name"] for t in tools)))


def _flat_web_search(result):
    """Render web_search JSON as a few dense lines.

    The model copies what it reads: 1.5 kB of JSON with `lanes`, `cached` and
    `tookMs` is what turns the synthesis turn into an empty answer, while three
    "title / url / snippet" lines get answered. Anything unexpected (an error
    string, a different shape) is passed through untouched.
    """
    try:
        data = json.loads(result["content"][0]["text"])
        rows = data["results"]
        if not isinstance(rows, list) or not rows:
            return result
    except Exception:
        return result
    lines = []
    for i, row in enumerate(rows[:FLAT_MAX_RESULTS], 1):
        title = (row.get("title") or "").strip()
        lines.append("%d. %s" % (i, title or "(no title)"))
        if row.get("url"):
            lines.append("   %s" % row["url"].strip())
        snippet = (row.get("snippet") or row.get("content") or "").strip()
        if snippet:
            lines.append("   %s" % snippet[:FLAT_SNIPPET])
    if len(rows) > FLAT_MAX_RESULTS:
        lines.append("(%d more results not shown)" % (len(rows) - FLAT_MAX_RESULTS))
    return _text_result("\n".join(lines))


def _flat_memory(result):
    """Render chroma results as a few dense notes.

    Same reason as web_search: ids, distances and `null` arrays are what the
    synthesis turn copies instead of the documents.
    """
    try:
        data = json.loads(result["content"][0]["text"])
        docs = (data.get("documents") or [[]])[0]
        if not isinstance(docs, list) or not docs:
            return result
    except Exception:
        return result
    lines = []
    for doc in docs[:MEMORY_MAX_RESULTS]:
        text = " ".join(str(doc or "").split())
        if text:
            lines.append("- " + text[:MEMORY_SNIPPET])
    if not lines:
        return result
    if len(docs) > MEMORY_MAX_RESULTS:
        lines.append("(%d more notes not shown)" % (len(docs) - MEMORY_MAX_RESULTS))
    return _text_result("\n".join(lines))


_MD_IMAGE = re.compile(r"!\[[^\]\[]*\]\([^()\s]*(?:\s+\"[^\"]*\")?\)")
_MD_LINK = re.compile(r"\[([^\]\[]*)\]\([^()\s]*(?:\s+\"[^\"]*\")?\)")
_SENTENCE = re.compile(r"[.!?…][\"'”’)\]]?(?:\s|$)")
_BLOCKS = re.compile(r"\n\s*\n")
# A block that is only a menu label, in the two languages this stack is used in.
_CHROME_WORDS = re.compile(
    r"^(vai al contenuto|jump to content|menu principale|main menu|navigazione|"
    r"navigation|ricerca|search|aspetto|appearance|contenuti|contents|strumenti|"
    r"tools|lingue|languages|altri progetti|community|comunità|personal tools|"
    r"toggle sidebar|fai una donazione|donate|pagina principale|main page)$",
    re.IGNORECASE)
_LIST_MARKS = ("* ", "- ", "+ ", "|", ">")
_CITATION = re.compile(r"\[\\\[(\d+)\\\]\]\([^)]*\)")
_ESCAPES = re.compile(r"\\([.()\[\]!?,'\"-])")


def _plain(block):
    """The text a reader would see: links to their label, no markup."""
    text = _MD_IMAGE.sub(" ", block)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return " ".join(text.split())


def _is_chrome(block):
    stripped = block.strip()
    if not stripped:
        return True
    plain = _plain(stripped)
    if not plain:
        return True
    if _CHROME_WORDS.match(plain):
        return True
    if stripped.startswith(_LIST_MARKS[:3]) and len(plain) < 120 and not _SENTENCE.search(plain):
        # a link farm: "Pagina principale / Ultime modifiche / Una voce a caso"
        return True
    bullets = sum(1 for line in stripped.splitlines()
                  if line.lstrip().startswith(_LIST_MARKS[:3]))
    if bullets and not _SENTENCE.search(plain):
        # a menu drawn as a bullet list inside one block, with no prose in it
        # ("* Rust * Install * Learn * Tools * Governance * Community")
        return bullets >= 2 or len(plain) < 120
    if len(plain) < 60 and not stripped.startswith("#") and not _SENTENCE.search(plain):
        # a stray label ("Ricerca", "Aspetto") — a heading keeps its '#'
        return True
    return False


def _readable(text):
    """Keep the article: drop the chrome above it and the link farms inside it.

    Heuristic on markdown, no HTML parsing: the article starts at the first block
    that is not a menu label or a link farm, and those blocks are dropped
    everywhere after it. Anything that does not look like a page (JSON, an array
    of JSON objects) is returned untouched: a wrong guess must never eat the
    answer, and a page that starts with a link is a page.
    """
    head = text.lstrip()[:2]
    if head.startswith("{") or head in ('["', "[{"):
        return text
    blocks = _BLOCKS.split(text)
    for start, block in enumerate(blocks):
        if not _is_chrome(block):
            break
    else:
        return text
    kept = [b.strip() for b in blocks[start:] if not _is_chrome(b)]
    # a heading with nothing under it is still chrome (docs sidebars: "Table of
    # Contents", "Previous topic", "Next topic", "This page")
    while kept and "\n" not in kept[0] and kept[0].startswith("#"):
        kept.pop(0)
    out = "\n\n".join(kept).strip()
    return out if len(out) >= URL_READ_MIN_KEEP else text


def _flatten_links(text):
    """`[Gerry Scotti](https://it.wikipedia.org/wiki/...)` -> `Gerry Scotti`.

    On a Wikipedia article more than half the characters are hrefs and tooltips:
    they cost tokens on every later turn and tell the model nothing it needs to
    answer. The page URL is already the tool argument, so nothing is lost.
    """
    text = _MD_IMAGE.sub(" ", text)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    # Wikipedia citations: `[\[1\]](#cite_note-...)` -> `[1]`
    text = _CITATION.sub(lambda m: "[%s]" % m.group(1), text)
    # leftover markdown escapes: `2006\.` -> `2006.`
    text = _ESCAPES.sub(lambda m: m.group(1), text)
    return re.sub(r"[ \t]+\n", "\n", text)


def _truncate(text, want):
    """Cut at `want` characters, preferring a sentence or line boundary."""
    cut = text[:want]
    edge = max(cut.rfind(". "), cut.rfind("\n"))
    if edge > want - 300:
        cut = cut[:edge + 1]
    return cut.rstrip()


_URL_READ_STATE = threading.local()


def _adapt_url_read(arguments):
    """Ask upstream for the whole page; the formatter keeps the article.

    `section`, `readHeadings` and `paragraphRange` are targeted slices the model
    asked for explicitly: those pass through untouched (and uncleaned) because
    the filtering is already done upstream.
    """
    args = dict(arguments)
    if any(args.get(k) for k in ("section", "readHeadings", "paragraphRange")):
        _URL_READ_STATE.clean = False
        _URL_READ_STATE.want = 0
        return args
    try:
        want = int(args.get("maxLength") or URL_READ_DEFAULT)
    except (TypeError, ValueError):
        want = URL_READ_DEFAULT
    _URL_READ_STATE.clean = True
    _URL_READ_STATE.want = max(1, want)
    if want < URL_READ_WINDOW:
        args["maxLength"] = URL_READ_WINDOW
    return args


def _flat_url_read(result):
    """Clean the page and cut it to the size the caller asked for."""
    if result.get("isError"):
        return result
    try:
        text = result["content"][0]["text"]
        if not isinstance(text, str) or not text.strip():
            return result
    except Exception:
        return result
    if getattr(_URL_READ_STATE, "clean", False):
        text = _flatten_links(_readable(text))
    want = getattr(_URL_READ_STATE, "want", 0)
    if want and len(text) > want:
        text = _truncate(text, want)
    return _text_result(text)


FLAT_FORMATTERS = {"web_search": _flat_web_search,
                   "web_url_read": _flat_url_read,
                   "chroma_query_documents": _flat_memory}


def _adapt_memory(arguments):
    """Turn the flat `query` into what the Chroma server expects."""
    query = arguments.get("query") or arguments.get("query_texts") or ""
    if isinstance(query, str):
        texts = [query] if query else []
    elif isinstance(query, list):
        texts = [str(q) for q in query]
    else:
        texts = []
    out = {"query_texts": texts}
    try:
        out["n_results"] = max(1, int(arguments.get("n_results") or 3))
    except Exception:
        out["n_results"] = 3
    collection = MEMORY_COLLECTION or arguments.get("collection_name")
    if collection:
        out["collection_name"] = collection
    return out


FLAT_ADAPTERS = {"chroma_query_documents": _adapt_memory,
                 "web_url_read": _adapt_url_read}


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


def _rows_of(result):
    """The results array of a raw `web_search` reply, or None on any surprise."""
    try:
        rows = json.loads(result["content"][0]["text"])["results"]
        return rows if isinstance(rows, list) and rows else None
    except Exception:
        return None


def _search_and_read(arguments):
    """Search the web, then read the top hit: one tool call, one model hop.

    Both halves are the real upstream tools, so a client that calls them
    directly is untouched. The result is never empty: when the read fails the
    snippets come back with a line saying so, because an empty tool result is
    what sends a small model off to plan a third call.
    """
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise RpcError("search_and_read needs a 'query'")
    try:
        want = int(arguments.get("maxLength") or URL_READ_DEFAULT)
    except (TypeError, ValueError):
        want = URL_READ_DEFAULT
    want = max(1, want)

    rows, search, note = None, None, ""
    url = str(arguments.get("url") or "").strip()
    if not url:
        search = _run_tool("web_search", {"query": query, "count": FLAT_MAX_RESULTS})
        rows = _rows_of(search)
        if rows is None:
            return search          # nothing usable came back: pass through what we have
        url = next((str(r["url"]).strip() for r in rows
                    if isinstance(r, dict) and str(r.get("url") or "").strip()), "")
    if not url:
        return _flat_web_search(search)

    text = ""
    try:
        read = _run_tool("web_url_read", {"url": url, "maxLength": URL_READ_WINDOW})
    except RpcError as exc:
        note = "page read failed: %s" % exc
    else:
        if read.get("isError"):
            # the upstream's own error text, not the Python repr of its content
            try:
                why = str(read["content"][0]["text"])
            except Exception:
                why = str(read.get("content") or "")
            note = "page read failed: %s" % " ".join(why.split())[:200]
        else:
            try:
                text = str(read["content"][0]["text"] or "")
            except Exception:
                text = ""

    body = _truncate(_flatten_links(_readable(text)), want) if text.strip() else ""
    if not body:
        note = note or "the page returned no readable text"
        if search is None:
            search = _run_tool("web_search", {"query": query, "count": FLAT_MAX_RESULTS})
        body = _flat_web_search(search)["content"][0]["text"]

    lines = ["Source: %s" % url]
    if note:
        lines.append("(%s)" % note)
    lines += ["", body]
    other = [r for r in (rows or [])
             if isinstance(r, dict) and str(r.get("url") or "").strip() != url]
    if other:
        lines += ["", "Other results:"]
        for row in other[:FLAT_MAX_RESULTS - 1]:
            title = (row.get("title") or "(no title)").strip()
            lines.append("- %s - %s" % (title[:120], str(row["url"]).strip()))
    return _text_result("\n".join(lines))


# Tools this proxy implements itself instead of forwarding: the schema is the
# hand-written one in FLAT_SPECS, the handler runs the real tools above. They
# are advertised on the flat surface, so `flat_tools` must not drop them for
# being absent from the catalogue.
PROXY_TOOLS = {"search_and_read"}
PROXY_HANDLERS = {"search_and_read": _search_and_read}


def handle_tool_call(name, arguments):
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except Exception:
            raise RpcError("arguments must be a JSON object")
    if not isinstance(arguments, dict):
        arguments = {}

    if name in ("find_tool", "tools_search"):
        query = arguments.get("query") or arguments.get("q") or ""
        if not isinstance(query, str) or not query.strip():
            raise RpcError("find_tool needs a non-empty 'query'")
        try:
            k = int(arguments.get("k") or TOP_K)
        except Exception:
            k = TOP_K
        k = max(1, min(8, k))
        res = CATALOG.search(query, k=k, names_only=bool(arguments.get("names_only")))
        return _text_result(json.dumps(res, ensure_ascii=False))

    if name in ("run_tool", "tool_call"):
        target = arguments.get("name") or arguments.get("tool")
        if not target:
            raise RpcError("run_tool needs 'name' (the tool to execute)")
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

    # A tool this proxy builds itself, out of the real ones (search_and_read).
    if name in PROXY_HANDLERS:
        return PROXY_HANDLERS[name](arguments)

    # A client that still has a stale list can call a real tool directly.
    if name in FLAT_TOOLS:
        adapt = FLAT_ADAPTERS.get(name)
        if adapt is not None:
            arguments = adapt(arguments)
    out = _run_tool(name, arguments)
    fmt = FLAT_FORMATTERS.get(name)
    if fmt is not None and name in FLAT_TOOLS:
        return fmt(out)
    return out


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
                # The exact text a client is being told about this catalogue, so
                # the map can be checked (and its cost measured) without a
                # client round trip. Memoized: it costs nothing here.
                "map": CATALOG.map_line(),
                "map_chars": len(CATALOG.map_line()),
                "upstreams": [{"name": u.name, "session": bool(u.sid),
                               "stateless": u.stateless, "ready": u.ready}
                              for u in UPSTREAMS]}).encode()
            self._send(200, body)
            return
        if path == "/refresh":
            # Idempotent admin op: re-read tools/list from every upstream and
            # rebuild the index if the catalogue changed. Safe to call anytime
            # (deploy hook, after adding an MCP server to the gateway).
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
            instructions = ("This server exposes the whole MCP catalogue behind two tools: "
                            "tools_search and tool_call. Call tools_search with the task "
                            "before using any tool. Behind it: " + CATALOG.map_line())
            flat = flat_tools()
            if flat:
                # The flat tools are the everyday shortcut: name them first and
                # leave the meta-tools as the way to reach everything else.
                instructions = ("Available tools: " + ", ".join(t["name"] for t in flat) +
                                ". Call one of them directly with its arguments.")
                if META_TOOLS_ENABLED:
                    instructions += (" For anything else, use tools_search and then tool_call.")
            self._json(mid, {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mcp-tools-lite", "version": "1.0"},
                "instructions": instructions,
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
            tools = flat_tools() + (meta_tools() if META_TOOLS_ENABLED else [])
            _log_surface(tools)
            self._json(mid, {"tools": tools})
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
