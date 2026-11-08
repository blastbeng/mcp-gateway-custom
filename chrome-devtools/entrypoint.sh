#!/bin/sh
# Entrypoint for the `chrome-devtools-mcp` image (see Dockerfile).
#
# Defaults:
#   --headless       this stack is server-side, there is no display
#   --isolated       a temporary user-data-dir per browser run. The default
#                    profile under $HOME/.cache would survive container
#                    restarts and Chrome's ProcessSingleton would refuse the
#                    next start exactly like the Playwright wrapper did; a
#                    throwaway profile has no such state. Trade-off: cookies
#                    and logins do not outlive the container.
#   --executable-path the CfT build baked at /opt/chrome/current
#   --chrome-arg=--disable-dev-shm-usage  /dev/shm is 64 MB in Docker by
#                    default; Chrome crashes on larger pages without this.
#   --usageStatistics=false / --performanceCrux=false  no telemetry, and no
#                    trace URLs sent to Google's CrUX API.
#
# Chrome's OS sandbox: entrypoint.sh probes it once at start (as this non-root
# user) and falls back to --no-sandbox only when the probe fails. When the
# sandbox works, no flag is added; the container stays the outer boundary.
set -eu

CHROME=/opt/chrome/current/chrome
SANDBOX_FLAG=""

# Chrome starts, writes a blank page and exits. SIGSEGV/SIGTRAP or a non-zero
# exit with a sandbox message means the namespace/SUID helper is unavailable
# in this Docker setup.
if ! "$CHROME" --headless --disable-gpu --no-first-run --disable-dev-shm-usage \
        --user-data-dir=/tmp/cdt-sandbox-probe --dump-dom about:blank \
        >/dev/null 2>/tmp/cdt-sandbox-probe.log; then
    if grep -qiE 'sandbox|namespace|setuid|SUID' /tmp/cdt-sandbox-probe.log; then
        SANDBOX_FLAG="--chrome-arg=--no-sandbox"
    fi
fi
rm -rf /tmp/cdt-sandbox-probe /tmp/cdt-sandbox-probe.log

if [ -n "$SANDBOX_FLAG" ]; then
    echo "chrome-devtools-mcp: Chrome sandbox unavailable, starting with --no-sandbox" >&2
else
    echo "chrome-devtools-mcp: Chrome sandbox active" >&2
fi

exec chrome-devtools-mcp \
    --headless \
    --isolated \
    --executable-path="$CHROME" \
    --chrome-arg=--disable-dev-shm-usage \
    --usageStatistics=false \
    --performanceCrux=false \
    $SANDBOX_FLAG \
    "$@"
