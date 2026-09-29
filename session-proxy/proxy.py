#!/usr/bin/env python3
"""
MCP session-healing reverse proxy for the docker-mcp gateway.

Problem: the docker-mcp gateway (docker/mcp-gateway:2.0.1, streamable HTTP on
:8811) keeps MCP sessions in memory and evicts them (idle TTL) and drops them
on restart. A streamable-HTTP client (AiderDesk) caches its Mcp-Session-Id at
initialize time and keeps POSTing it after eviction -> permanent
"session not found" (HTTP 404) until the client itself re-initializes, which
it never does.

Fix: this proxy listens on :8812 and presents a STABLE, forever-valid session
to the client. It owns ONE upstream session against the gateway, lazily
(initialize + notifications/initialized), and transparently re-initializes
whenever the upstream answers 404 "session not found". Client-facing requests
that drive the session lifecycle are answered locally:

  initialize                -> canned result (cached from the real upstream
                               initialize: serverInfo + capabilities) +
                               stable Mcp-Session-Id: proxy-stable
  notifications/initialized -> 202 (consumed)
  notifications/*           -> forwarded best-effort, always 202
  HTTP DELETE (session end) -> consumed locally (204), upstream NOT touched
  everything else           -> forwarded over the proxy's upstream session,
                               auto re-init + single retry on 404

Stdlib only (no deps). Run: python3 proxy.py  (env overrides below)
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("MCP_PROXY_UPSTREAM", "http://127.0.0.1:8811/mcp")
LISTEN_HOST = os.environ.get("MCP_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("MCP_PROXY_PORT", "8812"))

# Upstream wait for forwarded POSTs (tool calls).  Long-running dark-web
# searches over Tor (12-engine fan-out, onion latency) routinely exceed a
# 120s window, so this must stay comfortably above the slowest legitimate
# tool call.  Override with MCP_PROXY_UPSTREAM_TIMEOUT.
UPSTREAM_TIMEOUT = int(os.environ.get("MCP_PROXY_UPSTREAM_TIMEOUT", "600"))
TOKEN = ""
_envpath = os.environ.get("MCP_PROXY_ENV", "/opt/docker/compose/mcp/.env")
try:
    with open(_envpath, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("MCP_GATEWAY_AUTH_TOKEN="):
                TOKEN = line.split("=", 1)[1].strip()
                break
except OSError:
    pass

STABLE_SID = "proxy-stable"
CLIENT_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

_lock = threading.Lock()
_up_sid = None          # upstream Mcp-Session-Id
_init_result = None     # cached initialize result object (capabilities/serverInfo)


def _upstream(headers, body=None, method="POST", timeout=UPSTREAM_TIMEOUT):
    req = urllib.request.Request(UPSTREAM, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp.read(), None
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), None
    except Exception as e:  # connection refused, timeout...
        return 502, {}, json.dumps({"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32603, "message": f"proxy upstream error: {e}"}}).encode(), e


def _init_upstream():
    """(Re-)initialize the upstream session. Returns (session_id, init_result)."""
    global _up_sid, _init_result
    body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "session-proxy", "version": "1.0"}}}).encode()
    status, hdrs, data, _ = _upstream(CLIENT_HEADERS, body)
    sid = None
    for k, v in hdrs.items():
        if k.lower() == "mcp-session-id":
            sid = v
            break
    result = None
    try:
        text = data.decode("utf-8", "replace").strip()
        # The gateway answers POSTs with SSE framing: "event: message\ndata: {...}".
        if text.startswith("event:") or "\ndata:" in text:
            for ln in text.splitlines():
                if ln.startswith("data:"):
                    text = ln[len("data:"):].strip()
                    break
        payload = json.loads(text)
        if isinstance(payload, dict) and "result" in payload:
            result = payload["result"]
    except Exception:
        pass
    if sid and status == 200:
        _up_sid = sid
        if result is not None:
            _init_result = result
        # complete the lifecycle so the session is usable
        nbody = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
        _upstream({**CLIENT_HEADERS, "Mcp-Session-Id": sid}, nbody)
        return sid, result
    return None, result


def _ensure_session(force=False):
    global _up_sid
    with _lock:
        if force:
            _up_sid = None
        if _up_sid is None:
            sid, _ = _init_upstream()
            if sid is None:
                return None
        return _up_sid


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet, one-line on stderr
        sys.stderr.write("[mcp-session-proxy] %s\n" % (fmt % args))

    def _send(self, status, headers, body):
        self.send_response(status)
        seen_ct = False
        for k, v in headers.items():
            lk = k.lower()
            if lk in ("transfer-encoding", "connection", "content-length"):
                continue
            if lk == "content-type":
                seen_ct = True
            if lk == "mcp-session-id":
                v = STABLE_SID  # always hand the client a stable id
            self.send_header(k, v)
        self.send_header("Mcp-Session-Id", STABLE_SID)
        if not seen_ct and body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _forward_with_heal(self, body):
        """POST body upstream over our session; heal + retry once on 404."""
        for attempt in (1, 2):
            sid = _ensure_session(force=(attempt == 2))
            if sid is None:
                return 502, {"Content-Type": "application/json"}, json.dumps(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32603, "message": "proxy could not (re-)initialize upstream session"}}).encode()
            status, hdrs, data, _ = _upstream({**CLIENT_HEADERS, "Mcp-Session-Id": sid}, body)
            if status == 404 and b"session not found" in data.lower():
                continue  # healed; retry
            return status, hdrs, data
        return 502, {"Content-Type": "application/json"}, json.dumps(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32603, "message": "proxy upstream session failed after heal"}}).encode()

    def do_POST(self):
        global _init_result
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
        except Exception:
            raw = b""
        try:
            msg = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except Exception:
            msg = {}
        method = msg.get("method") or ""
        is_request = "id" in msg

        if method == "initialize":
            if _init_result is None:
                _ensure_session()  # fills the cache from the real gateway
            result = _init_result or {
                "capabilities": {"tools": {"listChanged": True}},
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "mcp-session-proxy", "version": "1.0"}}
            body = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": result}).encode()
            self._send(200, {"Content-Type": "application/json"}, body)
            return

        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        status, hdrs, data = self._forward_with_heal(raw)
        # notifications have no id -> gateway replies 202/empty; relay as-is
        self._send(status, hdrs, data)

    def do_DELETE(self):
        # client closing ITS (stable) session: consume locally, keep upstream alive
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # streamable-HTTP GET opens the server->client SSE stream; open an
        # upstream stream over our session and relay it verbatim.
        sid = _ensure_session()
        if sid is None:
            self._send(502, {"Content-Type": "text/plain"}, b"proxy upstream unavailable")
            return
        try:
            req = urllib.request.Request(UPSTREAM + ("?" + self.path.split("?", 1)[1] if "?" in self.path else ""),
                                         method="GET")
            req.add_header("Accept", "text/event-stream")
            req.add_header("Mcp-Session-Id", sid)
            if TOKEN:
                req.add_header("Authorization", "Bearer " + TOKEN)
            resp = urllib.request.urlopen(req, timeout=3600)
        except urllib.error.HTTPError as e:
            self._send(e.code, dict(e.headers), e.read())
            return
        except Exception as e:
            self._send(502, {"Content-Type": "text/plain"}, str(e).encode())
            return
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, STABLE_SID if k.lower() == "mcp-session-id" else v)
        self.send_header("Mcp-Session-Id", STABLE_SID)
        self.end_headers()
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass


def main():
    # warm the upstream session at boot (also validates the gateway is up)
    _ensure_session()
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"[mcp-session-proxy] listening on {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM}\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()