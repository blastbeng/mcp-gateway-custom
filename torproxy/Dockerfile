FROM alpine:3.22

# Tor >= 0.4.8 is mandatory on today's Tor network: the consensus requires the
# Desc=3 / Microdesc=3 descriptor protocols. The old dperson/torproxy:latest
# image still ships Tor 0.4.4.8 (built 2021) and can no longer bootstrap as a
# client: it dies after ~120s with "At least one protocol listed as required in
# the consensus is not supported by this version of Tor", and under
# `docker compose up --abort-on-container-exit` that single exit tore down (and
# recreated) the whole MCP stack every ~2.5 minutes.
RUN apk add --no-cache tor privoxy curl \
 && for f in /etc/privoxy/*.new; do cp "$f" "${f%.new}"; done \
 && rm -f /etc/privoxy/*.new \
 && install -d -o tor -g tor -m 700 /var/lib/tor

COPY torrc /etc/tor/torrc
COPY privoxy.conf /etc/privoxy/config
COPY entrypoint.sh /entrypoint.sh

RUN chmod 0644 /etc/tor/torrc /etc/privoxy/config \
 && chmod 0755 /entrypoint.sh

EXPOSE 9050 8118

ENTRYPOINT ["/entrypoint.sh"]
