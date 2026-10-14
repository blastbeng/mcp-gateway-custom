# MCP Gateway Custom

A self-hosted, Docker Compose-based stack that runs the [Docker MCP Gateway](https://github.com/docker/mcp-gateway) together with a curated set of MCP (Model Context Protocol) servers: local web search, documentation lookup, semantic vector memory, Tor/dark-web OSINT, and a multi-model "AI council".

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
| `mcp-session-proxy` | `python:3.12-alpine` | Reverse proxy that heals MCP sessions: owns one upstream session, hands clients a **stable session id**, and transparently re-initializes on upstream 404 (`session not found`) |
| `searxng` | `searxng/searxng:latest` | Privacy-respecting metasearch engine, used by the SearXNG MCP server |
| `valkey` | `valkey/valkey:9-alpine` | Limiter/botdetection backend for SearXNG (disabled); search results are cached in SQLite on the `searxng-cache` volume |
| `chroma` | `chromadb/chroma:latest` | ChromaDB vector store, used by the Chroma MCP server for persistent semantic memory |
| `torproxy` | `dperson/torproxy` | SOCKS5 (9050) + HTTP (8118) Tor proxy, used by OnionClaw |
| `mcp-image-checker` | `docker:cli` | Cron-like sidecar (hourly) that pulls required images and rebuilds the two locally built MCP server images if missing |
| `model-council-sync` | `python:3.12-alpine` | Keeps the council roster in step with LiteLLM (all `inference4free` models, free-only OpenRouter models incl. the `openrouter/free` alias, small+free Groq/Ollama/Gemini): every seat is probe-verified before joining and re-probed while seated; drops vanished, paid, or repeatedly-failing seats; no static members — anything no rule claims is dropped; restarts the gateway only when the roster actually changed |

### MCP servers registered in the gateway

Defined in [`catalog/custom.yaml`](catalog/custom.yaml) as a custom catalog (`local-ai`):

| Server | Type | Description |
|---|---|---|
| `searxng` | Local | Web search through the local SearXNG instance |
| `open-websearch` | Local | Self-hosted multi-engine keyword search (Bing, DuckDuckGo, Brave, Startpage, Exa, Hacker News), no API keys — robust fallback to SearXNG |
| `tavily` | Local | Keyed search fallback (`TAVILY_API_KEY`, free tier, no card): search, extract, crawl, map, research — immune to scraper bot-detection and per-IP CAPTCHA cooldowns |
| `chroma` | Local | Persistent semantic vector memory (project knowledge, architectural decisions, code patterns) |
| `onionclaw` | Local | **OnionClaw / SICRY** — Tor and dark-web OSINT: multi-engine onion search, .onion fetch/crawl, engine health checks, structured OSINT export |
| `context7` | Remote | Up-to-date, version-specific library/framework documentation ([mcp.context7.com](https://mcp.context7.com)) |
| `model-council` | Local | Multi-model "AI council" backed by LiteLLM: second opinions, code review, architecture advice, multi-model synthesis |

### Locally built MCP images

| Directory | Built image | Notes |
|---|---|---|
| [`onionclaw/`](onionclaw/) | `onionclaw-mcp:latest` | Built from the [OnionClaw](https://github.com/JacobJandon/OnionClaw) repository |
| [`model-council/`](model-council/) | `model-council-mcp:latest` | Thin wrapper: installs the `model-council-mcp` PyPI package |

Both are built automatically by the `mcp-image-checker` sidecar when missing (or run manually: `docker build -t onionclaw-mcp:latest ./onionclaw`).

## Configuration

### Secrets (`.env`)

All secrets live in a `.env` file at the repo root — **it is git-ignored and must never be committed**. Required variables:

```dotenv
# Auth token required by the MCP gateway / session proxy
MCP_GATEWAY_AUTH_TOKEN=change-me
# Tavily Search API key (server `tavily`); free tier, no credit card
TAVILY_API_KEY=
```

This same file is mounted read-only into the gateway as `--secrets`, so any key added here is injected into the matching MCP server container at spawn time. A secret that is missing degrades to `<UNKNOWN>` rather than crashing the gateway, which is why the `tavily` server can be registered before a key exists.

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

The council server reads its roster **once at startup**, while the providers behind it change over time: new `inference4free` routers appear (and vanish), Groq / Ollama Cloud / Gemini expose a shifting mix of paid and free models, and OpenRouter rotates its `:free` catalogue. The `model-council-sync` sidecar reconciles the roster every `SYNC_INTERVAL` seconds (default 1h):

1. lists the models LiteLLM currently exposes (`GET /v1/models`, credentials taken from the config's own `providers.litellm` block);
2. decides which models belong on the council with a **rules engine** (`model-council-data/sync-rules.json`, auto-created with defaults on first run):

   - a **size engine** classifies each model from its name — an explicit parameter count (`8b`, `27b`, `675b`...) against the rule's `max_params_b` (default 32B), else whole-word small keywords (`flash`, `mini`, `nano`, `instant`, ...); unknown size is never added;
   - a **free engine** probes each candidate with a one-token chat completion and classifies the answer — since no credits are ever topped up on these providers, `402/401/403` (and billing-worded `400`) means *paid*, `429` means *free but throttled*, `404` means *gone*, `200` means *usable*; `5xx`/timeouts are *unknown* — for candidates they are retried (and count toward the candidate cooldown once they repeat), for seats they accumulate probe-fail strikes; a rule whose seats **all** probe paid — or **all** probe unknown — in one cycle is treated as a gateway-wide failure (circuit breaker), not N dead models;

3. **adds** qualifying models as seats `{ "id", "provider": "litellm", "model" }`; **strikes** owned seats that vanish from LiteLLM, probe as paid/gone, keep failing their own probe (`probe-fail`: `REMOVE_AFTER_MISSES` consecutive failed probes seat a model out, and it returns as a candidate the cycle it answers again), or no longer meet the size rule — removal happens after `REMOVE_AFTER_MISSES` consecutive strikes (default 2), except *paid*, which leaves the same cycle. Rejected **candidates** get the same economy: a model refused repeatedly with the same verdict (paid/gone/unknown) accumulates candidate strikes and, after `CANDIDATE_STRIKES` of them (default 2), stops being probed for `CANDIDATE_RETRY_HOURS` (default 24) — then it is retried once and re-cooled if still bad; a throttled `429` is never admitted but never counted and retried every cycle;
4. **drops** any member no rule claims — there are no static members: every seat came from LiteLLM through a rule and a passing probe (the former hand-written `openrouter/free` / `small-model` included), and the sync fills the initially empty `members` list by itself;
5. rewrites `config.json` atomically (previous copy kept as `config.json.bak`, strikes tracked in `.sync-state.json`) **only when something actually changed**, then restarts `mcp-gateway` so the council picks the new roster up — client sessions are healed transparently by `mcp-session-proxy`.

Rule knobs (per provider: `inference4free`, `groq`, `ollama-cloud`, `gemini`, `openrouter`): `pattern` (there is no `exclude` knob — an unclaimed member is a static leftover and is dropped), `max_params_b`, `small_keywords`, `allow` (regex *searched* against the model's last segment — the Gemini default admits only the text flash/flash-lite family and small Gemma models, skipping image/audio/TTS/preview variants; the OpenRouter rule admits only free models: ids ending in `:free`, the cost signal OpenRouter puts in the model id — LiteLLM does not carry the price through `/model/info` — plus litellm's `openrouter/free` alias), `free` (`always` = free by construction, probe = aliveness only; `probe` = the probe decides free vs paid), `probe_existing` (re-check owned seats every cycle; default on for every rule — a seat that turned paid, or that keeps failing its probe, must leave the roster). Probes run concurrently (`PROBE_WORKERS`, default 12). Global env knobs: `SYNC_INTERVAL`, `REMOVE_AFTER_MISSES`, `CANDIDATE_STRIKES`, `CANDIDATE_RETRY_HOURS`, `PROBE_ENABLED`, `PROBE_TIMEOUT`, `PROBE_WORKERS`, `RESTART_GATEWAY`, `GATEWAY_CONTAINER`, `LITELLM_BASE_URL`/`LITELLM_API_KEY`.

### SearXNG

`searxng/settings.yml` is bind-mounted read/write into the container (SearXNG regenerates/updates it at startup). Search results are cached in SQLite on the `searxng-cache` volume; the `valkey` container backs only the limiter/botdetection (currently disabled), **not** the search cache. The `outgoing` block raises the per-engine timeout so throttled upstreams still get a chance to answer.

Two engine overrides in `settings.yml` back the general/web category:

- **`bing` is enabled** (`disabled: false`). Bing ships disabled in SearXNG's defaults but is keyless and, measured on this host, answers a general/web query in ~0.6 s with ~10 results and no CAPTCHA. It keeps Web searches alive while DuckDuckGo is in a CAPTCHA cooldown.
- **`brave` is disabled.** The engine scrapes `search.brave.com`, which now answers scrapers with HTTP 429 and a byte-identical anti-bot page — from this host **and** from a Tor exit, so it is bot detection rather than an IP quota we could rotate around. It cannot return results, so it only spent a request per query. Brave-indexed results come from the keyed `tavily` server instead; re-enable the engine if Brave lifts the block.

Routing SearXNG's engines through the Tor proxy was evaluated and **rejected**: it buys nothing for `duckduckgo` (which answers fine from the host IP until it hits a volume-based CAPTCHA) and actively degrades `bing` (10 result blocks direct vs 0 via Tor).

### Open WebSearch

`open-websearch` runs as a gateway-managed local server (`ghcr.io/aas-ee/open-web-search:latest`, `MODE=stdio`) and provides a second, independent keyword-search path when the scraped SearXNG engines throttle. Its `search` tool takes an `engines` argument (`duckduckgo`, `brave`, `startpage`, `hackernews`, `exa`); no API keys are required.

It is **keyless, therefore still scraped**: it has no API-key support at all, so it shares SearXNG's upstream blocks and its `brave` engine returns the same 429. Its `duckduckgo`/`bing` lanes work; it is a second *lane*, not a different *kind* of access. For access that does not depend on scraping at all, use `tavily` below.

### Tavily Search

`tavily` runs as a gateway-managed local server (`mcp/tavily:latest`, stdio) and is the only search path here that is not scraping: it calls the Tavily API, so it is unaffected by bot detection or per-IP CAPTCHA cooldowns.

Enable it by putting a key in `.env` (the gateway injects it from there — see [Secrets](#secrets-env)):

```dotenv
TAVILY_API_KEY=tvly-...
```

The free tier is 1,000 credits/month with **no credit card** ([app.tavily.com](https://app.tavily.com)); a basic search costs 1 credit, an advanced search 2. The server exposes `tavily_search`, `tavily_extract`, `tavily_crawl`, `tavily_map` and `tavily_research`. Until a key is set the server still registers and the gateway logs a benign `Secret 'TAVILY_API_KEY' not found` warning when the server is first spawned.

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
├── onionclaw-data/          # SICRY SQLite DB (git-ignored)
├── chroma-data/             # ChromaDB persistence (git-ignored)
├── chroma-cache/            # ChromaDB/HNSW cache (git-ignored)
├── searxng/
│   └── settings.yml         # SearXNG settings
├── session-proxy/
│   ├── proxy.py             # Session-healing MCP reverse proxy
│   └── README.md            # Proxy design details
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

docker compose build          # builds onionclaw-mcp and model-council-mcp
docker compose up -d
```

### Endpoints

| Endpoint | URL | Purpose |
|---|---|---|
| Session proxy (recommended for clients) | `http://<host>:8811/mcp` or `:8812/mcp` | Stable MCP session id, auto-healing on 404 |
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

## Security notes

- **Never commit `.env`, `model-council-data/config.json`, or any runtime data.** They are git-ignored; only `config.example.json` is tracked.
- The gateway binds to `127.0.0.1` only; the session proxy fronts it for external clients and forwards the bearer token.
- Tor traffic from OnionClaw is routed exclusively through the `torproxy` container.
- `model-council-sync` mounts the Docker socket (read-only) to restart the gateway after roster changes; if the host is exposed, consider fronting it with a [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) restricted to `POST /containers/*/restart`.
- SearXNG and ChromaDB ports should be firewall-restricted if the host is exposed.

## License

MIT
