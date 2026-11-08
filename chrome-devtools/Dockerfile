# Chrome DevTools MCP — local image, no upstream Docker image exists.
#
# Google publishes only the `chrome-devtools-mcp` npm package (plus the
# `chrome-devtools` CLI). The server drives a real Chrome over the DevTools
# protocol, so the image needs three things this stack builds in:
#   - a Chrome for Testing build (the upstream-supported browser),
#   - the shared libraries Chrome links against (bookworm-slim has none),
#   - the npm package itself.
#
# Both versions are resolved at BUILD time ("latest"): the CfT stable channel
# version comes from Google's last-known-good-versions.json and npm resolves
# the package tag. Rebuild the image (see README) to pick up newer releases;
# mcp-image-checker only builds it when it is missing, so a running stack is
# never surprised by a brand-new browser.
#
# Chrome refuses to run as root (crbug.com/638180), hence USER node. The
# unprivileged account is also what makes Chrome's OS sandbox possible at all;
# whether this host's Docker setup allows it is decided in entrypoint.sh.
FROM node:22-bookworm-slim

ARG TARGETARCH

# Runtime libraries for Chrome/Chromium on Debian bookworm. Kept to the set
# Chrome actually dlopen()s at startup; fonts-liberation stops pages from
# rendering with missing glyphs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
        fonts-liberation \
        libasound2 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

# Chrome for Testing, stable channel, via the official CDN. The archive's top
# directory differs between architectures (`chrome-linux64` on x64,
# `chrome-linux-arm64` on arm64), so it is normalised to /opt/chrome/current
# and the entrypoint never has to guess.
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) cft=linux64 ;; \
        arm64) cft=linux-arm64 ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    ver="$(node -e 'fetch("https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json").then(r => r.json()).then(d => process.stdout.write(d.channels.Stable.version))')"; \
    echo "Chrome for Testing ${ver} (${cft})"; \
    curl -fsSL -o /tmp/chrome.zip \
        "https://storage.googleapis.com/chrome-for-testing-public/${ver}/${cft}/chrome-${cft}.zip"; \
    mkdir -p /opt/chrome; \
    unzip -q /tmp/chrome.zip -d /opt/chrome; \
    mv /opt/chrome/chrome-linux* /opt/chrome/current; \
    rm -f /tmp/chrome.zip; \
    /opt/chrome/current/chrome --version; \
    echo "${ver}" > /opt/chrome/VERSION

# `latest` by request: the build always resolves the current release. Pin a
# version here if a future release ever breaks a tool.
RUN npm install -g chrome-devtools-mcp@latest \
    && npm cache clean --force

# Chrome never runs as root. `node` (uid 1000) already exists in the base
# image and owns /home/node, so its profile/temp dirs are writable.
ENV HOME=/home/node \
    CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS=1 \
    CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1

USER node
WORKDIR /home/node

COPY --chmod=755 entrypoint.sh /usr/local/bin/chrome-devtools-mcp-entrypoint

ENTRYPOINT ["/usr/local/bin/chrome-devtools-mcp-entrypoint"]
