# MCP Gateway Custom

A self-hosted, Docker Compose-based stack that runs the [Docker MCP Gateway](https://github.com/docker/mcp-gateway) together with a curated set of MCP (Model Context Protocol) servers: local web search, documentation lookup, semantic vector memory, Tor/dark-web OSINT, headless browser automation, local image reading, and a multi-model "AI council".

## Architecture

```
                          ┌────────────────────┐
   MCP clients ─────►     │ mcp-session-proxy  │  :8811 / :8812
                          │  (stable sessions) │
                          └─────────┬──────────┘
                                    │
                          ┌─────────▼──────────┐
                          │    mcp-gateway     │  :8813 (internal 8811)
                          │  (docker/mcp-gate) │
                          └──┬────┬────┬────┬──┘
                             │    │    │    │
        ┌────────────────────┘    │    │    └────────────────────┐
        ▼                         ▼    ▼                         ▼
  ┌──────────┐  ┌──────────┐  ┌────────┐                 ┌──────────────┐
  │ searxng  │  │  chroma  │  │context7│                 │ onionclaw    │
  │ + valkey │  │ (vectors)│  │(remote)│                 │ (SICRY/Tor)  │
  └──────────┘  └──────────┘  └────────┘                 └──────┬───────┘
        │                                                        │
  ┌─────▼─────┐  ┌────────────────┐                            ▼
  │  valkey   │  │ model-council  │                      ┌────────────┐
  │  (cache)  │  │ (LiteLLM back) │                      │ torproxy   │
  └───────────┘  └────────────────┘                      └────────────┘
```

### Services

| Service | Image | Purpose |
|---|---|---|
| `mcp-gateway` | `docker/mcp-gateway:latest` | Central MCP gateway (streamable-HTTP on port 8811, exposed as `127.0.0.1:8813`) |
| `mcp-session-proxy` | `python:3.12-alpine` | Reverse proxy that heals MCP sessions: owns one upstream session, hands clients a **stable session id**, transparently re-initializes on upstream 404 (`session not found`), and after 15 min without traffic releases the upstream session so the gateway stops the idle `longLived` child containers (browsers included) — they cold-start again on the next call |
| `mcp-tools-lite` | `python:3.12-alpine` | Fronts the whole catalogue (gateway **and** Home Assistant) with two constant meta-tools — `tools_search` (semantic, over the stack's `embedding-model`) and `tool_call` — so a model on a small CPU node does not have to prefill tens of thousands of tokens of tool schemas. Catalogue derived from the upstreams, self-refreshing, see [Tools-lite](#tools-lite-a-constant-tiny-tool-surface) |
| `searxng` | `searxng/searxng:latest` | Metasearch engine restricted to keyless engines (`searxng/settings.yml`): the always-up fallback behind the `websearch` MCP |
| `valkey` | `valkey/valkey:9-alpine` | Limiter/botdetection backend for SearXNG (disabled); search results are cached in SQLite on the `searxng-cache` volume |
| `chroma` | `chromadb/chroma:latest` | ChromaDB vector store, used by the Chroma MCP server for persistent semantic memory |
| `torproxy` | `torproxy:latest` (local build, `torproxy/`) | SOCKS5 (9050) + HTTP (8118) Tor proxy, used by OnionClaw. Built on Alpine with Tor 0.4.9: `dperson/torproxy` is frozen at Tor 0.4.4, which the network rejects (`Desc=3`/`Microdesc=3`) |
| `mcp-image-checker` | `docker:cli` | Cron-like sidecar (hourly) that pulls required images and rebuilds the locally built images (MCP servers + `torproxy` + `klaket-*`) if missing |
| `klaket-api` | `klaket-api:latest` (local build, `klaket/`) | Klaket job API (Go): accepts ingest requests, queues them on Redis, serves status/result/export |
| `klaket-worker` | `klaket-worker:latest` (local build, `klaket/`) | Offline media pipeline: ffmpeg + faster-whisper + sherpa-onnx diarization + OpenCV scenes + OCR. No torch |
| `model-council-sync` | `python:3.12-alpine` | Keeps the council roster in step with LiteLLM: exactly two pinned seats — `gpt4free/auto` (g4f's AnyProvider router) and `openrouter/free` — seated as soon as LiteLLM lists them and never probed; no static members — anything no rule claims is dropped; restarts the gateway only when the roster actually changed |

### Idle release

The gateway keeps one container per `longLived` server for as long as an MCP
session is open, so a permanently open session means permanently resident
Chromium/Chrome instances. `mcp-session-proxy` closes its upstream session after
`MCP_PROXY_IDLE_TTL` seconds (900 by default) without client traffic: the
gateway then stops all the children it cached for that session, and the next
tool call re-initializes and starts only the container it needs. See
[`session-proxy/README.md`](session-proxy/README.md#idle-release-why-ram-is-not-pinned).
While a server is idle its tool list still comes from the catalog, so clients
see all servers at all times.

### Tools-lite: a constant, tiny tool surface

`mcp-tools-lite` fronts the whole catalogue with **two** meta-tools and a
semantic index, because the full surface is unusable from a small model on a
small machine:

- the gateway + Home Assistant expose **126 tools**; rendered into a chat
  template that is tens of thousands of tokens (measured: 46,909 tokens for the
  168 tools Open WebUI attached, ~107 KB of raw JSON schema);
- prefill on the CPU-only node is **20–35 tok/s**, so that prompt is ~40 minutes
  of work before the first answer token;
- the tools block is rendered at the **start** of the template, so *any* change
  to it invalidates llama.cpp's prefix cache and forces a full re-prefill
  (measured: an identical 8,690-token prompt re-prefilled in 617 s). Per-request
  tool selection therefore does not help either: the surface must be small **and
  constant**.

`tools/list` answers with exactly two tools (**1,332 chars, ~370 tokens**):

| Tool | What it does |
|---|---|
| `tools_search(query, k=3, names_only=false)` | Semantic search over the catalogue with the stack's own `embedding-model`; returns the **full JSON schema** of the k best matches, the names of close matches, the catalogue size and the servers. `names_only=true` browses the whole catalogue by name (40 entries) |
| `tool_call(name, arguments)` | Executes any catalogue tool on its upstream and relays the result verbatim (text, images, structured content), including `isError` |

Measured on the Italian routing benchmark: **recall@8 15/18 (83%)** for the
semantic index versus 9/18 (50%) for Open WebUI's BM25 tool search, 105–150 ms
per query (query embedding included), index build 126 tools in ~100 s once,
then loaded from `/cache/index.json`.

#### Idempotency: the catalogue follows the upstreams

The catalogue is **derived**, never accumulated, so adding or removing an MCP
server (or a tool inside one, or an entity in Home Assistant) needs no edit, no
restart and no migration:

- every refresh reads `tools/list` from **all** upstreams (in parallel, bounded
  by `MCP_LITE_REFRESH_TIMEOUT`) and replaces the catalogue with what they
  answer *now*; a tool that disappears drops out of the index and out of
  `by_name`, and `tool_call` on it answers `unknown tool ... closest names: ...`
  instead of hanging;
- a refresh is triggered by a client `tools/list`, by `tools_search` when the
  catalogue is older than `MCP_LITE_REFRESH_TTL` (always in the background: a
  search is never delayed by it), by the **catalogue watcher** every
  `min(600, TTL/4)` seconds while a session is open (this is what makes a
  removed server disappear on its own), and by `GET /refresh` (deploy hook);
- a digest of *(name, description, server)* decides whether the index must be
  rebuilt: an unchanged catalogue costs one `tools/list` and nothing else (no
  re-embedding, no cache write);
- an upstream that **fails** keeps its previous tools, so a transient gateway
  restart cannot empty a third of the catalogue;
- with the embeddings unreachable the index is built lexically and rebuilt
  semantically as soon as they return.

`GET /health` reports the state used to verify all of the above:

```json
{"ok":true,"tools":126,"semantic":true,"index_age_s":5,"checked_age_s":5,
 "digest":"a3f00f0d4593","servers":["gateway","homeassistant"],
 "upstreams":[{"name":"gateway","session":true,"stateless":false,"ready":true},
              {"name":"homeassistant","session":false,"stateless":true,"ready":true}]}
```

#### Adding an upstream

Append an entry to `MCP_LITE_UPSTREAMS` (JSON) in `docker-compose.yml`:

```json
{"name":"homeassistant","url":"http://host.docker.internal:8123/api/mcp","token_env":"MCP_LITE_HA_TOKEN"}
```

`token_env` names a variable in the proxy's own environment, so no secret is
written in the compose file. Both **stateful** streamable-HTTP servers (the
gateway returns an `Mcp-Session-Id`) and **stateless** ones (Home Assistant
answers 200 with no session header) are supported. Clients that prefer the
native surface keep using the session proxy on `:8811`; nothing changes for
them.

#### Env

| Variable | Default | Meaning |
|---|---|---|
| `MCP_LITE_UPSTREAMS` | gateway only | JSON list of `{name,url,token_env}` |
| `MCP_LITE_EMBED_URL` / `_MODEL` | none / `embedding-model` | OpenAI-compatible embeddings endpoint; empty means lexical only |
| `MCP_LITE_TOP_K` | `3` | default `k` for `tools_search` |
| `MCP_LITE_REFRESH_TTL` | `3600` | catalogue age that triggers a background refresh |
| `MCP_LITE_REFRESH_TIMEOUT` | `45` | per-upstream `tools/list` budget |
| `MCP_LITE_REFRESH_MIN_GAP` | `30` | minimum gap between two stale checks |
| `MCP_LITE_AUTH_TOKEN` | gateway token | bearer required on `/mcp` and `/refresh`; `/health` is always open |
| `MCP_LITE_CACHE` | `/cache/index.json` | index cache (survives restarts) |
| `MCP_LITE_IDLE_TTL` | `900` | release upstream sessions after this many idle seconds |
| `MCP_LITE_CALL_TIMEOUT` | `900` | budget of one real tool call |

`manage-docker` restarts the service on its own when `tools-lite/proxy.py` is
newer than the running container (the image is a plain `python:3.12-alpine`, so
no pull or digest change would ever catch it).

### MCP servers registered in the gateway

Defined in [`catalog/custom.yaml`](catalog/custom.yaml) as a custom catalog (`local-ai`):

| Server | Type | Description |
|---|---|---|
| `websearch` | Local | **Primary web search** — no API key, no quota: headless Chromium reads DuckDuckGo's HTML endpoint, then Bing, Marginalia, the local keyless SearXNG instance, and Wikipedia / Stack Exchange / Hacker News APIs; 24 h cache + per-lane circuit breaker + hard timeouts. Tools: `web_search`, `web_search_status` |
| `searxng` | Local | **Always-up fallback** — the local SearXNG instance on keyless engines only, plus `web_url_read` for fetching a page you already have a URL for |
| `telegram-notify` | Local | One-way Telegram notifications: one fixed bot, one fixed chat id (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) — task-finished, needs-attention and Open WebUI automation alerts |
| `chroma` | Local | Persistent semantic vector memory (project knowledge, architectural decisions, code patterns) |
| `onionclaw` | Local | **OnionClaw / SICRY** — Tor and dark-web OSINT: multi-engine onion search, .onion fetch/crawl, engine health checks, structured OSINT export |
| `context7` | Remote | Up-to-date, version-specific library/framework documentation ([mcp.context7.com](https://mcp.context7.com)) |
| `model-council` | Local | Multi-model "AI council" backed by LiteLLM: second opinions, code review, architecture advice, multi-model synthesis |
| `playwright` | Local | Headless Chromium browser automation (Microsoft Playwright MCP): navigate, snapshot, click, type, screenshot, evaluate, inspect network — runs as a gateway container instead of a host `npx` process |
| `chrome-devtools` | Local | Google Chrome DevTools MCP: live Chrome for Testing automation and debugging — navigate, snapshot, click/fill, screenshots, console/network inspection, script evaluation, performance traces and Lighthouse audits |
| `local-image-reader` | Local | Reads a local image file and returns it as base64 `ImageContent` for a vision-capable client model (single tool: `read_image`) |
| `klaket` | Local | **Klaket** — offline audio/video analysis: transcript with word timestamps and speaker labels, language detection, scenes/keyframes, OCR, SRT/VTT, in-video search. Tools: `klaket_ingest`, `klaket_job_status`, `klaket_get_result`, `klaket_find_moment` |

### Locally built MCP images

| Directory | Built image | Notes |
|---|---|---|
| [`onionclaw/`](onionclaw/) | `onionclaw-mcp:latest` | Built from the [OnionClaw](https://github.com/JacobJandon/OnionClaw) repository |
| [`model-council/`](model-council/) | `model-council-mcp:latest` | Thin wrapper: installs the `model-council-mcp` PyPI package |
| [`telegram-notify/`](telegram-notify/) | `telegram-notify-mcp:latest` | Thin wrapper on `node:22-alpine`: installs the `telegram-notifier-mcp` npm package (pinned) |
| [`local-image-reader/`](local-image-reader/) | `local-image-reader-mcp:latest` | Thin wrapper on `python:3.12-slim`: installs the `mcp-local-image-reader` PyPI package (pinned, `mcp<2`) |
| [`playwright/`](playwright/) | `playwright-mcp:latest` | Thin wrapper on `mcp/playwright:latest`: clears the stale Chromium `SingletonLock` before exec |
| [`websearch/`](websearch/) | `websearch-mcp:latest` | Primary search: `mcp/playwright:latest` plus hand-written stdio JSON-RPC (`server.cjs`, `search.cjs`, `browser.cjs`) — no npm install, no SDK |
| [`chrome-devtools/`](chrome-devtools/) | `chrome-devtools-mcp:latest` | Node 22 image with Chrome for Testing + the `chrome-devtools-mcp` npm package (both resolved fresh at build time) |
| [`klaket/`](klaket/) | `klaket-api:latest`, `klaket-worker:latest`, `klaket-mcp:latest` | Three images from the pinned upstream tag `v0.7.1` ([huseyinstif/klaket](https://github.com/huseyinstif/klaket)): Go API, Python worker, stdio MCP server |

All of them are built automatically by the `mcp-image-checker` sidecar when missing (or run manually, e.g. `docker build -t local-image-reader-mcp:latest ./local-image-reader` — Klaket: `docker build -f klaket/worker.Dockerfile -t klaket-worker:latest ./klaket`). The base images they need (`node:22-alpine`, `node:22-bookworm-slim`, `golang:1.23-alpine`, `python:3.12-slim`, `alpine:3.20`, `mcp/playwright:latest`) are on the sidecar's pull list and, like these images, are in `PROTECTED_IMAGES` of `/usr/local/bin/manage-docker` so the monthly cleanup never deletes an image a build depends on.

## Configuration

### Secrets (`.env`)

All secrets live in a `.env` file at the repo root — **it is git-ignored and must never be committed**. Required variables:

```dotenv
# Auth token required by the MCP gateway / session proxy
MCP_GATEWAY_AUTH_TOKEN=change-me
# Telegram notifications (server `telegram-notify`): bot token + the ONE chat
# that may be notified. On this host both are copied from
# /opt/docker/compose/discord-llm-bot/.env.telegram (`TELOXIDE_TOKEN`,
# `TEL_ALLOWED_CHATS`) — same bot, same chat, no second bot needed.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

This same file is mounted read-only into the gateway as `--secrets`, so any key added here is injected into the matching MCP server container at spawn time. A secret that is missing degrades to `<UNKNOWN>` rather than crashing the gateway. No search server needs one: both `websearch` and `searxng` are keyless by design.

The Model Council server reads its API key from [`model-council-data/config.json`](model-council-data/config.example.json) — see below.

### Model Council config (`model-council-data/config.json`)

⚠️ **The real `config.json` contains an API key and is git-ignored.** Use the template instead:

```bash
cp model-council-data/config.example.json model-council-data/config.json
# then edit base_url + api_key
```

The config defines the LiteLLM provider and the council **members** (models that get asked in parallel):

```json
{
  "timeout": 30,
  "retries": 0,
  "providers": {
    "litellm": {
      "base_url": "http://your-litellm-host:4000/v1",
      "api_key": "sk-YOUR_LITELLM_API_KEY",
      "format": "openai"
    }
  },
  "members": []
}
```

### Model Council auto-sync (`model-council-sync`)

The council server reads its roster **once at startup**, while the providers behind it change over time. The roster is deliberately tiny — exactly two pinned seats, `gpt4free/auto` (g4f's `Model("auto", base_provider=AnyProvider)` router) and LiteLLM's `openrouter/free` alias; every other model the providers expose is unclaimed and gets dropped. The `model-council-sync` sidecar reconciles the roster every `SYNC_INTERVAL` seconds (default 1h):

1. lists the models LiteLLM currently exposes (`GET /v1/models`, credentials taken from the config's own `providers.litellm` block);
2. decides which models belong on the council with a **rules engine** (`model-council-data/sync-rules.json`, auto-created with defaults on first run):

   - a **size engine** classifies each model from its name — an explicit parameter count (`8b`, `27b`, `675b`...) against the rule's `max_params_b` (default 32B), else whole-word small keywords (`flash`, `mini`, `nano`, `instant`, ...); unknown size is never added;
   - a **free engine** probes each candidate with a one-token chat completion and classifies the answer — since no credits are ever topped up on these providers, `402/401/403` (and billing-worded `400`) means *paid*, `429` means *free but throttled*, `404` means *gone*, `200` means *usable*; `5xx`/timeouts are *unknown* — for candidates they are retried (and count toward the candidate cooldown once they repeat), for seats they accumulate probe-fail strikes; a rule whose seats **all** probe paid — or **all** probe unknown — in one cycle is treated as a gateway-wide failure (circuit breaker), not N dead models;

3. **adds** qualifying models as seats `{ "id", "provider": "litellm", "model" }`; **strikes** owned seats that vanish from LiteLLM, probe as paid/gone, keep failing their own probe (`probe-fail`: `REMOVE_AFTER_MISSES` consecutive failed probes seat a model out, and it returns as a candidate the cycle it answers again), or no longer meet the size rule — removal happens after `REMOVE_AFTER_MISSES` consecutive strikes (default 2), except *paid*, which leaves the same cycle. Rejected **candidates** get the same economy: a model refused repeatedly with the same verdict (paid/gone/unknown) accumulates candidate strikes and, after `CANDIDATE_STRIKES` of them (default 2), stops being probed for `CANDIDATE_RETRY_HOURS` (default 24) — then it is retried once and re-cooled if still bad; a throttled `429` is never admitted but never counted and retried every cycle;
4. **drops** any member no rule claims — there are no static members: every seat came from LiteLLM through a rule and a passing probe (the former hand-written `openrouter/free` / `small-model` included), and the sync fills the initially empty `members` list by itself;
5. rewrites `config.json` atomically (previous copy kept as `config.json.bak`, strikes tracked in `.sync-state.json`) **only when something actually changed**, then restarts `mcp-gateway` so the council picks the new roster up — client sessions are healed transparently by `mcp-session-proxy`.

Rule knobs (defaults: the `gpt4free` rule owns `^(?:gpt4free|g4f)/auto$`, the `openrouter` rule owns `^openrouter/free$`): `pattern` (there is no `exclude` knob — an unclaimed member is a static leftover and is dropped), `max_params_b`, `small_keywords`, `allow` (regex *searched* against the model's last segment), `free` (`always` = free by construction; `probe` = the probe decides free vs paid), `pin` (regexes on the full model id: a pinned model is seated as soon as LiteLLM lists it, is never probed and no probe verdict can strike it out; it still leaves when LiteLLM stops listing it — **both default seats are pinned**, which also keeps the hourly sync from exercising g4f's whole provider chain: on this host that drove `gpt4free-chromium` into OOM), `probe_existing` (re-check owned seats every cycle; pinned seats are never re-probed). Probes run concurrently (`PROBE_WORKERS`). Global env knobs: `SYNC_INTERVAL`, `REMOVE_AFTER_MISSES`, `CANDIDATE_STRIKES`, `CANDIDATE_RETRY_HOURS`, `PROBE_ENABLED`, `PROBE_TIMEOUT`, `PROBE_WORKERS`, `RESTART_GATEWAY`, `GATEWAY_CONTAINER`, `LITELLM_BASE_URL`/`LITELLM_API_KEY`.

### Web Search (`websearch`)

Primary search path, in [`websearch/`](websearch/), built locally as
`websearch-mcp:latest` (`mcp/playwright` as the base, so there is no npm install
and no build step: `server.cjs` speaks newline-delimited JSON-RPC over stdio by
hand).

Lanes run **in order**, merging until `count` results are collected (so a
`count` of 8 usually costs one browser request, not three):

| Lane | What it is | Why it is here |
|---|---|---|
| `duckduckgo` | headless Chromium on `html.duckduckgo.com/html/` | the strongest keyless SERP from this host. Plain HTTP clients get HTTP 202 + an anomaly page; a real browser gets a normal list (`result__a` / `result__snippet`) in ~1-2 s with ten on-topic results. Paced (`DDG_MIN_INTERVAL_S`, 3 s) and budgeted (`DDG_MAX_PER_HOUR`, 150) because it is still somebody else's endpoint |
| `bing` | headless Chromium on `bing.com/search?form=QBLH` | second opinion with broader coverage: `form=QBLH` is the only Bing variant that is not degraded (over plain HTTP Bing honours just the first keyword, and `format=rss` returns unrelated results). **This IP gets throttled**: after a handful of queries Bing answers "There are no results for ..." (`li.b_no`), so the lane is paced (`BING_MIN_INTERVAL_S`, 8 s), budgeted (`BING_MAX_PER_HOUR`, 40) and three empty SERPs trip its breaker |
| `marginalia` | headless Chromium on `marginalia-search.com` | independent index (small/curated web), no key, ~2 s JS bot gate |
| `searxng` | local SearXNG JSON API | the L5 fallback, `categories=general,it` (see below) |
| `wikipedia`, `stackexchange`, `hn` | official JSON APIs | keyless tail that cannot be bot-blocked; always answers |

Behaviour that makes it usable from an agent loop:

- **hard timeouts** — `LANE_TIMEOUT_MS` (20 s) per lane, `TOTAL_TIMEOUT_MS`
  (45 s) for the whole call; a lane that misses its budget is recorded and the
  next one runs, so a throttled upstream can never make the tool hang;
- **24 h cache** — file cache on `/var/lib/websearch/cache`
  (`CACHE_TTL_S`, `CACHE_MAX` entries, oldest swept out), so repeating a query
  is instant and does not spend a DuckDuckGo/Bing budget slot;
- **per-lane circuit breaker** — `BREAKER_THRESHOLD` consecutive real failures
  (or empty SERPs) open a lane for `BREAKER_COOLDOWN_S` (10 min); the state
  persists in `/var/lib/websearch/state/breakers.json`, so a blocked IP is not
  relearned after every container restart;
- **one shared Chromium** (one browser, one page, requests serialised) so
  concurrent tool calls cannot fight over the page or multiply RAM;
- **no summaries, no truncation by engine** — every result carries `title`,
  `url`, `snippet` and the `engine` that produced it, and the reply reports the
  per-lane outcome (`lanes: [{name, status, ms, results}]`).

`web_search_status` returns the lane list, breaker state, last run per lane and
cache size — the first thing to look at when a search comes back thin.

Volume: `/opt/docker/compose/mcp/websearch-data` (cache + breaker state, owned
by uid 1000 like the rest of the local MCP images). It is in
`MCP_GATEWAY_DOCKER_BIND_ALLOW_WRITABLE_PATHS`; the directory is git-ignored.

### SearXNG (keyless fallback)

`searxng/settings.yml` is bind-mounted into the container and restricts the
instance to engines that need **no API key and no bot-friendly IP**:
`mwmbl`, `wiby`, `github`, `hackernews`, `stackoverflow`, `mdn`, `pypi`, `npm`,
`crates.io`, `arxiv`, `openalex`, `crossref` (12 engines, all explicitly
`disabled: false` — most ship disabled in SearXNG's defaults).

Every general-purpose SERP scraper was **removed on purpose**, measured from this
host: DuckDuckGo answers HTTP 202 with an anomaly page, Brave 429, Google a
`/sorry` CAPTCHA, Startpage a suspension notice, Mojeek an ALTCHA JavaScript
challenge, Ecosia 403, and Bing a degraded page over HTTP. Keeping them only
spent a request per query and filled `unresponsive_engines` with noise. (DDG is
served to the `websearch` browser lane instead: the same endpoint answers a
real browser.) General-web coverage is what the
`websearch` MCP adds; this instance is the fallback that cannot be
rate-limited into silence.

Two details worth knowing:

- SearXNG's default category is `general` only, which on this engine set means
  `mwmbl` + `wiby`. The engine mix is spread over `it` (github, mdn,
  stackoverflow, hackernews) and `science` (arxiv, openalex, crossref), so
  callers should ask for `categories=general,it` — `websearch` does. `science`
  is opt-in: measured on `docker compose healthcheck interval`, adding it
  contributed 39 marginally relevant papers that pushed the real hits off the
  page (23 results with `general,it`, 62 with `science` added).
- The package registries (`npm`, `pypi`, `crates.io`) ship in the `it`
  category, so every technical query came back larded with unrelated crates
  (3 of 6 results in one test). `searxng/settings.yml` pins their category to
  `packages`: they stay reachable with a bang (`!npm react`) but no longer
  pollute a plain search.
- The `valkey` container backs only the limiter/botdetection, which is disabled;
  results are cached in SQLite on the `searxng-cache` volume.

Routing these engines through the Tor proxy was evaluated and **rejected**: it
buys nothing (none of them blocks this IP) and adds latency.

### Telegram Notify

`telegram-notify` pushes one-way notifications to a single Telegram chat — "task finished", "needs attention", or the result of an Open WebUI automation. It is the off-the-shelf [`telegram-notifier-mcp`](https://github.com/AdeshAtole/telegram-notifier-mcp) npm package (pinned `1.3.1`, MCP SDK + native `fetch`, no Telegram SDK), run from a thin local image so the gateway needs no network at spawn time:

```bash
docker build -t telegram-notify-mcp:latest ./telegram-notify
```

Both values come from `.env` (see [Secrets](#secrets-env)); the gateway injects them into the server container:

```dotenv
TELEGRAM_BOT_TOKEN=<token from @BotFather>
TELEGRAM_CHAT_ID=<your numeric chat id>
```

**One destination only.** The server defaults every send to `TELEGRAM_CHAT_ID`, so no notification needs a chat id; the tools are `send_message`, `send_document`, `send_photo`, `send_video`, `send_audio` and `get_updates`. Two limits belong in the threat model:

- the tools accept an optional `chatId` override — the container is deliberately *not* patched, so the only thing keeping the agent on one chat is that nothing ever asks it to pass one (and the fact that a bot can only reach chats that started it). Treat the model as untrusted: if a prompt-injected agent should be *unable* to message elsewhere, that needs server-side enforcement (a wrapper that drops `chatId`), not this image;
- `get_updates` long-polls. The bot used here is the same one as `discord-llm-bot`, whose Teloxide client is already polling, so calling `get_updates` can hit a `409 Conflict` and steal pending updates from that bot. Sending is unaffected (plain `sendMessage`); just don't reach for `get_updates` while the other bot runs.

Verify from the host (session proxy, bearer token from `.env`) — the gateway exposes these tools unprefixed (`send_message`, not `telegram-notify_send_message`):

```bash
curl -s -X POST http://localhost:8811/mcp \
  -H "Authorization: Bearer $MCP_GATEWAY_AUTH_TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"send_message","arguments":{"text":"test"}}}'
```

### Playwright

`playwright` is Microsoft's [Playwright MCP](https://github.com/microsoft/playwright-mcp) run as a gateway-managed container instead of the host `npx @playwright/mcp` process AiderDesk used to spawn. It drives headless Chromium — `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_take_screenshot`, `browser_evaluate`, `browser_network_requests` and the rest (23 tools). The catalog entry is `image: playwright-mcp:latest` with `longLived: true`, so the gateway keeps one long-lived child and every client session reuses that same browser. Chromium stays inside the container, so there is still no host Node/npx dependency: the gateway starts it with `--init` (which reaps the Chromium process tree) and caps it at `--cpus 1 --memory 2Gb`, and the image is ~1.7 GB — hence its place on the `mcp-image-checker` pull list and in `PROTECTED_IMAGES` of `/usr/local/bin/manage-docker`.

**One server, one browser.** Playwright keeps a persistent Chromium profile guarded by `~/.cache/ms-playwright-mcp/*/SingletonLock` (a `<hostname>-<pid>` symlink), and its `isProfileLocked()` only checks `kill(pid, 0)`. Inside a fresh container that check is meaningless: the lock was written by the previous container, but Chromium's PIDs are low and the new PID namespace recycles them, so the pid exists again and startup aborts with `Browser is already in use ... use --isolated`.

**Why a wrapper image.** `playwright-mcp:latest` ([`playwright/`](playwright/)) is `mcp/playwright:latest` plus an entrypoint that deletes `SingletonLock` / `SingletonCookie` / `SingletonSocket` from every `~/.cache/ms-playwright-mcp/*/` and then `exec`s the base image's own `node /app/cli.js --headless --browser chromium --no-sandbox "$@"`. The lock left behind by a dead container is stale by definition, so clearing it is safe; Chromium's own `ProcessSingleton` still guards against a genuinely concurrent second instance on the same profile.

**The browser profile lives on the host, not in the container.** Chromium itself is baked into the image (`PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`), but the persistent profile is not — the server writes it to `$HOME/.cache/ms-playwright-mcp` (`HOME=/home/node`, uid 1000), which would otherwise end up in the container's writable layer, i.e. inside `/var/lib/docker` on the host's **root** filesystem. On this host `/` is the small filesystem (117 GB, ~23 GB free, 56 GB already in `/var/lib/docker`) while `/opt` is the big one (453 GB), so the profile is bind-mounted read-write from `/opt`:

```yaml
volumes:
  - "/opt/blast/cache/ms-playwright-mcp:/home/node/.cache/ms-playwright-mcp:rw"
  - "/opt/blast/cache/ms-playwright:/home/node/.cache/ms-playwright:rw"
  - "/tmp:/tmp:rw"
```

The first two host directories are owned by uid 1000, which is the container's `node` user, so no chown is needed. The second one exists because the server's browser *registry* ignores `PLAYWRIGHT_BROWSERS_PATH`; without the mount Docker creates the missing parent `/home/node/.cache` as `root:root` and the unprivileged `node` user fails with `EACCES: permission denied, mkdir '/home/node/.cache/ms-playwright'`. The container is spawned by the gateway, so all three binds must also appear in `MCP_GATEWAY_DOCKER_BIND_ALLOWED_PATHS` (read-write ones in `MCP_GATEWAY_DOCKER_BIND_ALLOW_WRITABLE_PATHS`) in `docker-compose.yml`. Note that the profile **directory name follows the browser build** baked into the image (`mcp-chrome-for-testing-<hash>`), so profiles written by a different Playwright version sit in sibling directories rather than being reused.

### Chrome DevTools

`chrome-devtools` is Google's [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) — a real Chrome driven over the DevTools protocol, not just page automation. 32 tools: `new_page` / `navigate_page` / `list_pages`, `take_snapshot` + `click` / `fill` / `type_text` / `press_key`, `take_screenshot`, `list_console_messages` / `get_console_message`, `list_network_requests` / `get_network_request`, `evaluate_script`, `performance_start_trace` / `performance_stop_trace` / `performance_analyze_insight`, `lighthouse_audit`, `take_heapsnapshot` and the rest. It complements `playwright`: Playwright is the general automation lane, Chrome DevTools is the debugging/performance lane (traces, console with source maps, network bodies, Lighthouse).

Google publishes only the npm package — there is no upstream image and no hosted endpoint — so [`chrome-devtools/`](chrome-devtools/) builds a local image. Chrome for Testing is the upstream-supported browser; both its stable-channel version (from Google's `last-known-good-versions.json`) and the npm package (`chrome-devtools-mcp@latest`) are resolved at **build** time, so rebuilding the image is how the server is updated. `mcp-image-checker` only builds it when missing.

**Non-root + sandbox probe.** Chrome refuses to run as root (crbug.com/638180), so the image runs as `node` (uid 1000). Even non-root, Chrome's sandbox needs either unprivileged user namespaces or its SUID helper; this host's Docker setup allows neither. The entrypoint therefore probes Chrome once at startup and appends `--chrome-arg=--no-sandbox` only when the probe reports a sandbox/namespace failure (it logs which mode it chose to stderr). The container — `no-new-privileges` at the gateway level and the gateway's own CPU/memory caps — remains the isolation boundary. `--chrome-arg=--disable-dev-shm-usage` is always passed because Docker's default `/dev/shm` is 64 MB.

**Throwaway profile.** `--isolated` gives every browser run a temporary user-data dir. The default profile under `$HOME/.cache` would survive container restarts and Chrome's `ProcessSingleton` would then refuse to start — the same failure the Playwright wrapper had to work around — so no host profile mount exists and no lock cleanup is needed. Trade-off: cookies and logins do not outlive the container (they do survive across tool calls for as long as the long-lived child runs).

**Privacy and noise.** Usage statistics, CrUX trace lookups and npm update checks are disabled (`--usageStatistics=false`, `--performanceCrux=false`, `CHROME_DEVTOOLS_MCP_NO_*` env vars), so the server makes no Google calls of its own.

**Filesystem.** The catalog mounts `/tmp` read-write at the same path: that is both the server's OS temp dir and the only root its file tools may touch (clients that do not negotiate MCP roots are restricted to the temp dir). Screenshots and downloads written as `/tmp/...` are therefore on the host `/tmp`, where `telegram-notify` and `local-image-reader` can pick them up. `/tmp` is already in `MCP_GATEWAY_DOCKER_BIND_ALLOW_WRITABLE_PATHS`.

The image is ~1 GB (Chrome for Testing + runtime libraries), built by `mcp-image-checker` when missing and kept by `PROTECTED_IMAGES` in `/usr/local/bin/manage-docker`, together with its `node:22-bookworm-slim` base.

### Local Image Reader

`local-image-reader` is a single-tool local server — `read_image` — that returns a local image file as base64 `ImageContent` for a vision-capable client model (PNG/JPEG/GIF/WebP/BMP/SVG). It is the containerised form of the `mcp-local-image-reader` host MCP AiderDesk used to launch with `uvx`; see [`local-image-reader/`](local-image-reader/).

The project trees (`/opt/projects`, `/opt/docker/compose`) and `/tmp` are bind-mounted **read-only at their original paths**, so a path the agent already knows resolves unchanged inside the container:

```yaml
volumes:
  - "/opt/projects:/opt/projects:ro"
  - "/opt/docker/compose:/opt/docker/compose:ro"
  - "/tmp:/tmp:ro"
```

`/tmp` is shared so a screenshot or download written to a scratch path can be read back at the same path. Everything *outside* these three roots is invisible to the container — the old host `uvx` process could read any path, this one cannot.

To expose another root, add it both in `catalog/custom.yaml` and in `MCP_GATEWAY_DOCKER_BIND_ALLOWED_PATHS` in `docker-compose.yml` — the gateway only permits host binds under a path listed there (read-write binds additionally require `MCP_GATEWAY_DOCKER_BIND_ALLOW_WRITABLE_PATHS`).

The same `/tmp` share is mounted, read-only, in `telegram-notify` (its `send_document` / `send_photo` / `send_video` / `send_audio` take a local `filePath`) and in `model-council` (`materials[].path` reads a local file). `playwright` gets it **read-write**: its output directory is `/tmp`, so screenshots and downloads land on the host `/tmp` where the other two can reach them — that rw bind is why `/tmp` is also in `MCP_GATEWAY_DOCKER_BIND_ALLOW_WRITABLE_PATHS`.

Playwright-MCP restricts its own filesystem access to two **allowed roots**: its output directory and its workspace root (`cwd`, `/home/node`). The output directory is set to `/tmp` via `PLAYWRIGHT_MCP_OUTPUT_DIR`, which makes `/tmp` an allowed root — `browser_file_upload`, `browser_drop` and explicit filenames under `/tmp` work **without** `--allow-unrestricted-file-access` (that flag would open the whole filesystem instead of just adding `/tmp`). Auto-named outputs (snapshots, unnamed screenshots) go to that output dir; a *relative* filename is resolved against the workspace root instead, so pass an absolute `/tmp/...` path when the file has to be visible to `telegram-notify` or `local-image-reader`.

The Dockerfile pins `mcp<2`: the upstream package declares only `mcp>=1.0.0`, so pip would otherwise resolve the 2.x MCP SDK, which dropped the low-level `Server.list_tools()` the server still calls.

### Klaket

`klaket` turns audio and video files into LLM-ready data **fully offline**: timestamped transcript with speaker labels, detected language, scenes + keyframes, on-screen text (OCR), SRT/VTT export and in-video search. No cloud, no API keys, no GPU. Source is [huseyinstif/klaket](https://github.com/huseyinstif/klaket), pinned to tag `v0.7.1` in all three Dockerfiles.

Upstream is a four-part application; here it is three containers plus the gateway-spawned MCP server:

| Piece | Container | Role |
|---|---|---|
| REST API | `klaket-api` (Go) | `POST /v1/ingest`, job status/result/export, `/healthz` |
| Queue | *shared Redis* | jobs state — **db 3** of the host instance in `/opt/docker/compose/redis`, reached via `host.docker.internal:6379`; db0/db1/db2 belong to other services |
| Worker | `klaket-worker` (Python) | ffmpeg + faster-whisper + sherpa-onnx diarization + OpenCV scenes + rapidocr |
| MCP server | spawned by the gateway | `klaket-mcp:latest`, stdio client of the API (`KLAKET_API_URL`) |

Upstream hardcodes Redis database 0; both Dockerfiles patch in a `REDIS_DB` env var and **fail the build** if the patch stops applying. The shared Redis has no password — if one is ever added, this needs a further small patch (`REDIS_ADDR` carries host:port only).

Usage is asynchronous:

1. `klaket_ingest` with `url` — a link **or a path visible to the worker** (`/tmp`, `/opt/projects`, `/opt/docker/compose`, all read-only). Prefer `/opt/projects` for real footage: `/tmp` is a 4 GB tmpfs, i.e. RAM.
2. `klaket_job_status` until `done` (the worker logs `stage`/`progress`).
3. `klaket_get_result` for markdown (transcript, chapters, scenes, OCR) or `klaket_find_moment` to search inside it.

Artifacts land in `/opt/docker/compose/mcp/klaket-data` (with `TMPDIR=/data/tmp`); models in `/opt/docker/compose/mcp/klaket-models` — both on `/opt`, never in `/var/lib/docker` on the small root filesystem. First job downloads whisper `small` (~600 MB) and the sherpa-onnx diarization models (~40 MB); afterwards it is fully offline.

Settings live in `docker-compose.yml` under `klaket-worker`: `WHISPER_MODEL` = `${KLAKET_WHISPER_MODEL:-small}` (override in `.env` with `tiny|base|small|medium|large-v3`), `KLAKET_OCR=on`, `KLAKET_DIARIZE=on`, `KLAKET_VLM=off`, `KLAKET_MAX_DURATION=7200` (jobs longer than 2 h are rejected) and `OMP_NUM_THREADS=2`. Budget **1–3 hours of CPU per hour of media** on this Pi; the worker is capped at 3 GB and will swap.

Scope: speaker labels are **anonymous** (`S1`, `S2`, …) and consistent within one job. This is transcription + diarization — not voiceprint/identification and not emotion analysis. Argos translation is deliberately not installed (it would pull torch, spacy and stanza for a feature the MCP server does not expose).

## Repository layout

```
.
├── catalog/
│   └── custom.yaml          # Custom MCP catalog (server definitions for the gateway)
├── model-council/
│   └── Dockerfile           # Builds model-council-mcp:latest
├── model-council-data/
│   ├── config.example.json  # Council config template (safe to commit)
│   ├── sync-rules.json      # Auto-sync engine rules (safe to commit)
│   ├── config.json          # Real config with API key (git-ignored)
│   ├── config.json.bak      # Pre-sync backup (git-ignored)
│   └── .sync-state.json     # Sync strike state (git-ignored)
├── model-council-sync/
│   └── sync.py              # Auto-sync of dynamic council seats (rules engine)
├── onionclaw/
│   └── Dockerfile           # Builds onionclaw-mcp:latest
├── playwright/
│   ├── Dockerfile           # Builds playwright-mcp:latest (wrapper on mcp/playwright)
│   └── entrypoint.sh        # Clears the stale Chromium SingletonLock
├── chrome-devtools/
│   ├── Dockerfile           # Builds chrome-devtools-mcp:latest (Chrome for Testing + npm package)
│   └── entrypoint.sh        # Headless defaults, sandbox probe, isolated profile
├── onionclaw-data/          # SICRY SQLite DB (git-ignored)
├── chroma-data/             # ChromaDB persistence (git-ignored)
├── chroma-cache/            # ChromaDB/HNSW cache (git-ignored)
├── websearch/
│   ├── server.cjs           # MCP stdio entrypoint (JSON-RPC by hand)
│   ├── search.cjs           # Lane order, cache, circuit breaker, timeouts
│   ├── browser.cjs          # Shared Chromium + DuckDuckGo/Bing/Marginalia extractors
│   └── Dockerfile           # Builds websearch-mcp:latest on mcp/playwright
├── websearch-data/          # Query cache + breaker state (git-ignored)
├── searxng/
│   └── settings.yml         # SearXNG settings (keyless engine set = L5)
├── session-proxy/
│   ├── proxy.py             # Session-healing MCP reverse proxy
│   └── README.md            # Proxy design details
├── tools-lite/
│   ├── proxy.py             # Two-meta-tool front over the whole catalogue (semantic index)
│   └── cache/               # Index cache: tools + vectors (git-ignored)
├── telegram-notify/
│   └── Dockerfile           # Builds telegram-notify-mcp:latest
├── local-image-reader/
│   └── Dockerfile           # Builds local-image-reader-mcp:latest
├── klaket/
│   ├── api.Dockerfile       # Builds klaket-api:latest (Go, upstream v0.7.1)
│   ├── worker.Dockerfile    # Builds klaket-worker:latest (ffmpeg + whisper + sherpa-onnx)
│   └── mcp.Dockerfile       # Builds klaket-mcp:latest (stdio client of the API)
├── klaket-data/             # Klaket job artifacts (git-ignored)
├── klaket-models/           # Klaket model cache (git-ignored)
├── docker-compose.yml       # Full stack definition
└── .env                     # Secrets (git-ignored)
```

## Usage

### Prerequisites

- Docker with Compose v2
- Docker MCP Gateway plugin (`docker mcp` CLI) available on the host
- A reachable LiteLLM proxy (or adjust `base_url` to any OpenAI-compatible endpoint)

### Start the stack

```bash
cp .env.example .env 2>/dev/null || true
echo "MCP_GATEWAY_AUTH_TOKEN=$(openssl rand -hex 32)" >> .env

cp model-council-data/config.example.json model-council-data/config.json
# edit model-council-data/config.json with your LiteLLM URL + key

docker compose build          # builds onionclaw-mcp, model-council-mcp, telegram-notify-mcp, local-image-reader-mcp, playwright-mcp and torproxy
# klaket-api/worker/mcp have no `build:` stanza (one source tree, three targets), and
# chrome-devtools-mcp is built on demand: the mcp-image-checker builds them on its
# first pass, or build them by hand:
#   for f in api worker mcp; do docker build -f klaket/$f.Dockerfile -t klaket-$f:latest ./klaket; done
#   docker build -t chrome-devtools-mcp:latest ./chrome-devtools
docker compose up -d
```

### Endpoints

| Endpoint | URL | Purpose |
|---|---|---|
| Session proxy (recommended for clients) | `http://<host>:8811/mcp` or `:8812/mcp` | Stable MCP session id, auto-healing on 404 |
| Tools-lite (small/cheap models) | `http://<host>:8814/mcp` | Two meta-tools over the whole catalogue; `/health` and `/refresh` (admin, idempotent) on the same port |
| Gateway (direct) | `http://127.0.0.1:8813/mcp` | Direct gateway access (host-local only) |
| SearXNG UI | `http://127.0.0.1:8080` | Metasearch web UI |
| ChromaDB | `http://<host>:8000` | Vector store API |

### Connecting an MCP client

```bash
docker run -i --rm mcp/inspector \
  --transport streamable-http \
  --server-url http://<host>:8812/mcp
```

Or point any MCP client (Claude Desktop, AiderDesk, etc.) at `http://<host>:8812/mcp` with the `Authorization: Bearer <MCP_GATEWAY_AUTH_TOKEN>` header.

Clients backed by small models — Open WebUI on the cheap LLM node, a local
AiderDesk with the gateway attached — should use `http://<host>:8814/mcp`:
same catalogue, two tools, no prompt cost. Use the **same** bearer token as the
gateway (`MCP_GATEWAY_AUTH_TOKEN`): the proxy only ever hands it the two
meta-tools, and holds the upstream tokens itself. `/health` is unauthenticated
so the container healthcheck works.

## Security notes

- **Never commit `.env`, `model-council-data/config.json`, or any runtime data.** They are git-ignored; only `config.example.json` is tracked.
- The gateway binds to `127.0.0.1` only; the session proxy fronts it for external clients and forwards the bearer token.
- Tor traffic from OnionClaw is routed exclusively through the `torproxy` container.
- Playwright's browser profile is bind-mounted **read-write** from `/opt/blast/cache/ms-playwright-mcp`, so a page the browser visits can write inside that directory. The container runs with `no-new-privileges` and is capped at `--cpus 1 --memory 2Gb`, and the mount is a cache directory on `/opt` (`MCP_GATEWAY_DOCKER_BIND_ALLOW_WRITABLE_PATHS`) — keep it pointed at a directory like this one, not at a tree worth protecting.
- Telegram notifications are one-way and confined to one chat by configuration (`TELEGRAM_CHAT_ID`); the bot token is shared with `discord-llm-bot` but is only ever used to *send*. The tools are not patched to reject a `chatId` override — see [Telegram Notify](#telegram-notify) if that guarantee ever needs to be hard.
- `model-council-sync` mounts the Docker socket (read-only) to restart the gateway after roster changes; if the host is exposed, consider fronting it with a [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) restricted to `POST /containers/*/restart`.
- SearXNG and ChromaDB ports should be firewall-restricted if the host is exposed.

## License

MIT
