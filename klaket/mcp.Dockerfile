# Klaket MCP server — thin stdio client of the Klaket REST API
# (upstream apps/mcp). Tools: klaket_ingest, klaket_job_status,
# klaket_get_result, klaket_find_moment.
#
# Base is node:22-alpine (not upstream's node:20) to reuse the node base image
# this stack already protects and pulls for telegram-notify-mcp — one less
# ~180 MB base image on the small root filesystem. The server is plain
# TypeScript with global fetch, so 22 is a superset of what it needs.
FROM node:22-alpine AS build

RUN apk add --no-cache git

WORKDIR /src

RUN git clone --depth 1 --branch v0.7.1 \
        https://github.com/huseyinstif/klaket.git .

WORKDIR /src/apps/mcp

RUN npm ci \
    && npm run build \
    && npm prune --omit=dev

FROM node:22-alpine

WORKDIR /app

ENV NODE_ENV=production

COPY --from=build /src/apps/mcp/dist ./dist
COPY --from=build /src/apps/mcp/node_modules ./node_modules
COPY --from=build /src/apps/mcp/package.json ./

# KLAKET_API_URL is injected by the gateway (see catalog/custom.yaml).
ENTRYPOINT ["node", "dist/index.js"]
