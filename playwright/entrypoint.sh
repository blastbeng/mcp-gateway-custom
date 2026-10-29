#!/bin/sh
# Entrypoint for the `playwright-mcp` image (see Dockerfile).
#
# Playwright-MCP guards its persistent Chromium profile with
# ~/.cache/ms-playwright-mcp/*/SingletonLock, a symlink named
# "<hostname>-<pid>".  Its isProfileLocked() only asks `kill(pid, 0)`:
#
#     const pid = parseInt(target.split("-").pop(), 10);
#     process.kill(pid, 0);      // alive -> "locked"
#
# Inside a fresh container that check is meaningless.  The lock was written by
# the previous container (different hostname), but Chromium's PIDs are low and
# the new PID namespace recycles them, so `kill(pid, 0)` succeeds for an
# unrelated process and startup aborts with
#
#     Browser is already in use for .../mcp-chrome-for-testing-<hash>,
#     use --isolated to run multiple instances of the same browser
#
# The browser that wrote the lock died with its container, so the lock is
# stale by definition: clear the singleton files and let Chromium recreate
# them.  Chromium's own ProcessSingleton keeps guarding against a real second
# instance on the same profile.
set -eu

HOME="${HOME:-/home/node}"
export HOME

for dir in "$HOME"/.cache/ms-playwright-mcp/*/; do
    [ -d "$dir" ] || continue
    rm -f "${dir}SingletonLock" "${dir}SingletonCookie" "${dir}SingletonSocket"
done

exec node /app/cli.js --headless --browser chromium --no-sandbox "$@"
