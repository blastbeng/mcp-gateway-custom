# Klaket API — Go job orchestrator (upstream apps/api).
#
# Pinned to the upstream tag: the whole Klaket stack (api/worker/mcp) is
# built from the SAME commit, so a mixed-version trio can never happen.
# Upstream publishes only the Python SDK on PyPI — no images — hence the
# in-Dockerfile clone, same pattern as onionclaw/.
FROM golang:1.23-alpine AS build

RUN apk add --no-cache git

WORKDIR /src

RUN git clone --depth 1 --branch v0.7.1 \
        https://github.com/huseyinstif/klaket.git .

WORKDIR /src/apps/api

# REDIS_DB: upstream hardcodes database 0 (redis.NewClient only gets Addr).
# Klaket shares the host-wide Redis instance, whose db0/db1/db2 are already
# owned by other services, so the index must be selectable. The helper lives
# in its own file to avoid touching the upstream import block; the sed below
# wires it in and the grep makes the build fail if the patch ever stops
# applying (e.g. upstream changes that line) instead of silently writing to
# db0.
RUN cat > redisdb_patch.go <<'EOF'
package main

import (
	"os"
	"strconv"
)

func redisDB() int {
	if v := os.Getenv("REDIS_DB"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return 0
}
EOF

RUN sed -i 's/redis.NewClient(&redis.Options{Addr: redisAddr})/redis.NewClient(\&redis.Options{Addr: redisAddr, DB: redisDB()})/' main.go \
    && grep -q 'DB: redisDB()' main.go

RUN go mod download \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /klaket-api .

FROM alpine:3.20

# The upstream compose runs the api unprivileged: it only reads /data
# (result.json and exported files written by the worker) and talks to Redis.
RUN adduser -D klaket

USER klaket

COPY --from=build /klaket-api /usr/local/bin/klaket-api

EXPOSE 8484

ENTRYPOINT ["klaket-api"]
