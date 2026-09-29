FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SICRY_DB_PATH=/data/sicry.db \
    TOR_SOCKS_HOST=torproxy \
    TOR_SOCKS_PORT=9050 \
    TOR_TIMEOUT=30 \
    SICRY_MAX_CHARS=12000 \
    SICRY_SEARCH_CACHE_TTL=3600 \
    SICRY_ENGINE_CACHE_TTL=3600

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/onionclaw

RUN git clone --depth 1 \
        https://github.com/JacobJandon/OnionClaw.git \
        . \
    && pip install --no-cache-dir \
        "requests[socks]>=2.28.0" \
        "beautifulsoup4>=4.11.0" \
        "python-dotenv>=1.0.0" \
        "stem>=1.8.0" \
        "mcp==1.30.0"

# SICRY usa il nome "description", mentre MCP 1.x
# espone questo parametro come "instructions".
RUN sed -i \
    's/FastMCP("sicry", description=/FastMCP("sicry", instructions=/' \
    /opt/onionclaw/sicry.py

# NOTE (user policy, 2026-09-10): do NOT prune/blacklist any engine from
# SEARCH_ENGINES. An earlier speed patch that deleted 7 "dead" engines was
# reverted on request — the rule is: try every engine each search, skip
# engines that fail (the code already does this per-engine). Offline engines
# cost latency (fresh search ~8 min, accepted), never coverage.

RUN mkdir -p /data

ENTRYPOINT ["python", "/opt/onionclaw/sicry.py", "serve"]