#!/bin/sh
#
# torproxy: Tor SOCKS5 (9050) + Privoxy HTTP (8118) supervised in one
# container, replacing dperson/torproxy (Tor 0.4.4, 2021) which the Tor
# network no longer accepts.
set -eu

TOR_LOG_LEVEL="${TOR_LOG_LEVEL:-err}"

install -d -o tor -g tor -m 700 /var/lib/tor
install -d -o privoxy -g privoxy -m 755 /var/log/privoxy

cleanup() {
  trap - TERM INT
  [ -n "${PRIVOXY_PID:-}" ] && kill -TERM "$PRIVOXY_PID" 2>/dev/null || true
  [ -n "${TOR_PID:-}" ] && kill -TERM "$TOR_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup TERM INT

tor -f /etc/tor/torrc --Log "${TOR_LOG_LEVEL} stderr" &
TOR_PID=$!

privoxy --no-daemon /etc/privoxy/config &
PRIVOXY_PID=$!

# If either daemon dies the container must die too: a torproxy without a
# working SOCKS port would keep OnionClaw broken while looking healthy.
while kill -0 "$TOR_PID" 2>/dev/null && kill -0 "$PRIVOXY_PID" 2>/dev/null; do
  sleep 2
done

echo "torproxy: a daemon exited, shutting down" >&2
cleanup
exit 1
