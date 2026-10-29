# Thin wrapper around Microsoft's Playwright MCP image.
#
# Why: the base image's only entrypoint is `node /app/cli.js ...`. Playwright
# refuses to start when it finds a SingletonLock in the persistent profile,
# and it decides "locked" with a bare kill(pid, 0) — see entrypoint.sh. After a
# container restart that check matches an unrelated process in the new PID
# namespace, so every second start failed with "Browser is already in use".
#
# The wrapper clears those stale singleton files and then execs the real
# server with the same flags the base image used. Everything else (Chromium at
# /ms-playwright, user node/uid 1000, HOME=/home/node) is inherited untouched.
FROM mcp/playwright:latest

COPY --chmod=755 entrypoint.sh /usr/local/bin/playwright-mcp

ENTRYPOINT ["/usr/local/bin/playwright-mcp"]
