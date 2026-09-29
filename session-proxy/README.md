# mcp-session-proxy — why this exists

The docker-mcp gateway (`docker/mcp-gateway:2.0.1`, streamable HTTP) keeps MCP
sessions in memory, evicts idle ones, and loses them on restart. Clients that
cache `Mcp-Session-Id` at initialize time (AiderDesk) then POST the stale id
forever and get HTTP 404 `session not found` — the gateway never re-teaches
them, and the client never re-initializes on its own.

`proxy.py` sits in front of the gateway and fixes this at the protocol layer:

- owns ONE upstream gateway session (initialize + notifications/initialized),
- answers the client's `initialize` locally with the gateway's real
  capabilities and a **stable** `Mcp-Session-Id: proxy-stable`,
- forwards everything else over its upstream session and, on 404
  `session not found`, transparently re-initializes and retries (heal),
- consumes client `initialize`/`notifications/initialized`/DELETE locally so
  the client lifecycle never touches the upstream session state,
- relays the SSE GET stream for server->client messages.

Wiring (docker-compose.yml, service `mcp-session-proxy`):
- host `127.0.0.1:8811` -> proxy -> gateway (this is what AiderDesk talks to;
  URL unchanged, stale sessions heal transparently),
- host `127.0.0.1:8812` -> proxy (alias),
- host `127.0.0.1:8813` -> gateway direct (debug/bypass).

Operated by `docker-compose@mcp.service` (systemd template unit at
/lib/systemd/system/docker-compose@.service — DO NOT EDIT; changes belong in
/opt/docker/compose/mcp/docker-compose.yml). Restart the STACK with
`sudo systemctl restart docker-compose@mcp.service`, never individual
containers (`--abort-on-container-exit` tears the unit down on any stop).

Environment (docker-compose.yml, service `mcp-session-proxy`):
- `MCP_PROXY_UPSTREAM_TIMEOUT` — seconds the proxy waits on forwarded tool
  calls (POSTs). Default 900. Long-running dark-web searches via OnionClaw
  fan out to ~19 onion engines over Tor and routinely take 3–8 minutes; the
  original hard-coded 120s killed them with `proxy upstream error: timed
  out` even though they completed server-side and were cached in
  `/opt/docker/compose/mcp/onionclaw-data/sicry.db` (TTL 1800s). If a search
  times out once, retrying the SAME query within 30 min returns instantly
  from cache.
- `MCP_PROXY_UPSTREAM`, `MCP_PROXY_HOST`, `MCP_PROXY_PORT` — wiring, see above.

Also pre-seeded: `/opt/docker/compose/mcp/chroma-cache/onnx_models/` holds the
all-MiniLM-L6-v2 model so chroma MCP documents embed server-side on first use.
