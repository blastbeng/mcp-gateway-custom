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
| `valkey` | `valkey/valkey:9-alpine` | Cache backend for SearXNG |
| `chroma` | `chromadb/chroma:latest` | ChromaDB vector store, used by the Chroma MCP server for persistent semantic memory |
| `torproxy` | `dperson/torproxy` | SOCKS5 (9050) + HTTP (8118) Tor proxy, used by OnionClaw |
| `mcp-image-checker` | `docker:cli` | Cron-like sidecar (hourly) that pulls required images and rebuilds the two locally built MCP server images if missing |
| `model-council-sync` | `python:3.12-alpine` | Keeps the council's rule-owned seats in step with LiteLLM (inference4free `auto` routers, small+free Groq/Ollama/Gemini models, `:free` OpenRouter models): adds new ones, drops vanished ones (after a grace period), never touches hand-written members; restarts the gateway only when the roster actually changed |

### MCP servers registered in the gateway

Defined in [`catalog/custom.yaml`](catalog/custom.yaml) as a custom catalog (`local-ai`):

| Server | Type | Description |
|---|---|---|
| `searxng` | Local | Web search through the local SearXNG instance |
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
```

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
  "members": [
    { "id": "openrouter/free", "provider": "litellm", "model": "openrouter/free" },
    { "id": "groq/openai/gpt-oss-120b", "provider": "litellm", "model": "groq/openai/gpt-oss-120b" }
  ]
}
```

### Model Council auto-sync (`model-council-sync`)

The council server reads its roster **once at startup**, while the providers behind it change over time: new `inference4free` routers appear (and vanish), Groq / Ollama Cloud / Gemini expose a shifting mix of paid and free models, and OpenRouter rotates its `:free` catalogue. The `model-council-sync` sidecar reconciles the roster every `SYNC_INTERVAL` seconds (default 1h):

1. lists the models LiteLLM currently exposes (`GET /v1/models`, credentials taken from the config's own `providers.litellm` block);
2. decides which models belong on the council with a **rules engine** (`model-council-data/sync-rules.json`, auto-created with defaults on first run):

   - a **size engine** classifies each model from its name — an explicit parameter count (`8b`, `27b`, `675b`...) against the rule's `max_params_b` (default 32B), else whole-word small keywords (`flash`, `mini`, `nano`, `instant`, ...); unknown size is never added;
   - a **free engine** probes each candidate with a one-token chat completion and classifies the answer — since no credits are ever topped up on these providers, `402/401/403` (and billing-worded `400`) means *paid*, `429` means *free but throttled*, `404` means *gone*, `200` means *usable*; `5xx`/timeouts are *unknown* and retried next cycle, and a rule whose seats **all** probe paid in one cycle is treated as a gateway-wide failure (circuit breaker), not N dead models;

3. **adds** qualifying models as seats `{ "id", "provider": "litellm", "model" }`; **strikes** owned seats that vanish from LiteLLM, probe as paid/gone, or no longer meet the size rule — removal happens after `REMOVE_AFTER_MISSES` consecutive strikes (default 2), so a transient hiccup doesn't churn the roster. Rejected **candidates** get the same economy: a model refused with a stable verdict (paid/gone) accumulates candidate strikes and, after `CANDIDATE_STRIKES` of them (default 2), stops being probed for `CANDIDATE_RETRY_HOURS` (default 24) — then it is retried once and re-cooled if still bad, while `unknown` verdicts never count and keep being retried every cycle;
4. **never touches** members no rule claims — by default that is only `openrouter/free` and `small-model`; a rule's `exclude` list protects specific ids the same way;
5. rewrites `config.json` atomically (previous copy kept as `config.json.bak`, strikes tracked in `.sync-state.json`) **only when something actually changed**, then restarts `mcp-gateway` so the council picks the new roster up — client sessions are healed transparently by `mcp-session-proxy`.

Rule knobs (per provider: `inference4free`, `groq`, `ollama-cloud`, `gemini`, `openrouter`): `pattern`, `exclude`, `max_params_b`, `small_keywords`, `allow` (regex *searched* against the model's last segment — the Gemini default admits only the text flash/flash-lite family and small Gemma models, skipping image/audio/TTS/preview variants; the OpenRouter rule admits only ids ending in `:free`, the cost signal OpenRouter puts in the model id — LiteLLM does not carry the price through `/model/info`), `free` (`always` = free by construction, probe = aliveness only; `probe` = the probe decides free vs paid), `probe_existing` (re-check owned seats every cycle; default on for `free:"probe"`, but the OpenRouter rule opts **out** — re-probing hourly would burn the daily free quota, so seats there are policed by disappearance only, while candidates are still probed once before joining). Global env knobs: `SYNC_INTERVAL`, `REMOVE_AFTER_MISSES`, `CANDIDATE_STRIKES`, `CANDIDATE_RETRY_HOURS`, `PROBE_ENABLED`, `PROBE_TIMEOUT`, `RESTART_GATEWAY`, `GATEWAY_CONTAINER`, `LITELLM_BASE_URL`/`LITELLM_API_KEY`.

### SearXNG

`searxng/settings.yml` is bind-mounted read/write into the container (SearXNG regenerates/updates it at startup). Valkey is used for caching.

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
