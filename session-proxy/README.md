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
  calls (POSTs). Default 600 in the code, `900` in docker-compose.yml.
  Long-running dark-web searches via OnionClaw
  fan out to ~19 onion engines over Tor and routinely take 3–8 minutes; the
  original hard-coded 120s killed them with `proxy upstream error: timed
  out` even though they completed server-side and were cached in
  `/opt/docker/compose/mcp/onionclaw-data/sicry.db` (TTL 1800s). If a search
  times out once, retrying the SAME query within 30 min returns instantly
  from cache.
- `MCP_PROXY_IDLE_TTL` — seconds of client silence after which the proxy
  closes its upstream session, which makes the gateway stop every `longLived`
  child container it keeps for that session (see below). `900` in
  docker-compose.yml; `0` disables idle release.
- `MCP_PROXY_IDLE_SWEEP` — how often the sweeper checks for an idle session,
  in seconds. Default 30.
- `MCP_PROXY_UPSTREAM`, `MCP_PROXY_HOST`, `MCP_PROXY_PORT` — wiring, see above.

### Idle release (why RAM is not pinned)

A `longLived: true` catalog entry is cached by the gateway **per session**: one
container per (server, session) pair, kept from the moment it is first called
until that session is closed. With a session that lives forever, every
long-lived server (both browsers, Chromium/Chrome included) stays resident even
when nobody is using it.

`MCP_PROXY_IDLE_TTL` fixes that without giving up the stable session id:

1. after `IDLE_TTL` seconds with no forwarded request, the sweeper takes the
   upstream session id and clears it atomically (a request arriving right after
   simply initializes a fresh session, so the DELETE can only hit the abandoned
   one);
2. it sends `DELETE /mcp` with that id;
3. the gateway's `ReleaseClientsForSession` closes every cached client, which
   closes the `docker run --rm -i …` stdin of each child, so the container exits
   and is removed;
4. the next tool call re-initializes the session and starts only the container
   that call actually needs.

Requests in flight are never interrupted (`_inflight` guard), and the SSE GET
relay is deliberately not counted as in-flight — a client may hold that stream
open for hours and it must not pin the containers alive. When a release happens
while a client is holding that stream, the stream just goes quiet; the client
reconnects on its next request and gets a fresh upstream session.

Trade-off: the first call after a release pays a cold start (a fraction of a
second for the python/node servers, a few seconds for the browser servers), and
browser state (open page, cookies, logged-in tabs) is gone. Keep the TTL above
the longest pause inside a single agent task; 15 minutes is the default.

Also pre-seeded: `/opt/docker/compose/mcp/chroma-cache/onnx_models/` holds the
all-MiniLM-L6-v2 model so chroma MCP documents embed server-side on first use.
