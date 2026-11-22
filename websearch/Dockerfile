# Local keyless web search MCP.
#
# Built on the same mcp/playwright image the gateway already pulls, so the
# Chromium binary is shared (no second ~400 MB download) and the Playwright
# version matches the browser baked at /ms-playwright. The server itself is
# dependency-free CommonJS: `playwright` is the only module it requires, and it
# resolves from /app/node_modules.
FROM mcp/playwright:latest

# Files live under /app so Node resolves `playwright` from the base image.
COPY server.cjs search.cjs browser.cjs /app/websearch/

ENV BROWSER_HEADLESS=true \
    NODE_OPTIONS=--max-old-space-size=256

ENTRYPOINT ["node", "/app/websearch/server.cjs"]
