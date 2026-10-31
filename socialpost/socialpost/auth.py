"""One-time credential bootstrap for the four adapters.

Run it on the host or in a throwaway container with the same /data mount:

    python -m socialpost.auth youtube --client-id ... --client-secret ...
    python -m socialpost.auth meta --user-token EAAG... --app-id ... --app-secret ...
    python -m socialpost.auth tiktok --refresh-token ... --client-key ... --client-secret ...
    python -m socialpost.auth show

YouTube uses a loopback redirect (no public URL needed). Meta takes a
long-lived user token from Graph API Explorer and derives the Page token and
IG user id. TikTok expects a refresh token obtained from your app's OAuth
flow (its redirect must be HTTPS, so the exchange happens in the browser).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import requests

from . import store
from .platforms import meta as meta_mod
from .platforms import tiktok as tiktok_mod
from .platforms import youtube as youtube_mod

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
LOOPBACK = "http://127.0.0.1:8765/"


def _youtube(args) -> None:
    acc = store.account("youtube")
    client_id = args.client_id or acc.get("client_id") or os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = args.client_secret or acc.get("client_secret") or os.environ.get("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("missing --client-id/--client-secret (Google Cloud > Credentials > OAuth client, type Desktop)")

    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorized. You can close this tab and return to the terminal.")

        def log_message(self, *a):  # silence
            pass

    url = f"{GOOGLE_AUTH}?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": LOOPBACK,
        "response_type": "code",
        "scope": youtube_mod.AUTH_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })
    print("\nOpen this URL, sign in with the channel account and approve:\n\n" + url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    server = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    print("Waiting for the redirect on", LOOPBACK, "...")
    while "code" not in result and "error" not in result:
        threading.Event().wait(0.5)
    server.server_close()

    if "error" in result:
        sys.exit(f"authorization denied: {result['error']}")

    resp = requests.post(GOOGLE_TOKEN, data={
        "code": result["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": LOOPBACK,
        "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"token exchange failed ({resp.status_code}): {resp.text[:300]}")

    payload = resp.json()
    if not payload.get("refresh_token"):
        sys.exit("no refresh_token returned: remove the app's previous grant and retry (prompt=consent is set)")

    store.save_account("youtube", client_id=client_id, client_secret=client_secret,
                       refresh_token=payload["refresh_token"])
    store.save_access("youtube", access_token=payload["access_token"],
                      expires_at=__import__("time").time() + int(payload.get("expires_in", 3600)))
    print("YouTube OK — refresh token stored in", store.ACCOUNTS_PATH)


def _meta(args) -> None:
    user_token = args.user_token
    if not user_token:
        sys.exit("--user-token is required (Graph API Explorer, permissions: "
                 "instagram_content_publish, pages_manage_posts, pages_show_list)")

    app_id = args.app_id or os.environ.get("META_APP_ID")
    app_secret = args.app_secret or os.environ.get("META_APP_SECRET")
    token = user_token
    if app_id and app_secret:
        resp = requests.get(f"{meta_mod._base()}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": user_token,
        }, timeout=30)
        if resp.status_code == 200:
            token = resp.json()["access_token"]
            print("exchanged for a long-lived token")
        else:
            print("warning: long-lived exchange failed, using the token as-is:", resp.text[:200])

    resp = requests.get(f"{meta_mod._base()}/me/accounts", params={
        "fields": "id,name,access_token,instagram_business_account{id,username}",
        "access_token": token,
    }, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"cannot list pages ({resp.status_code}): {resp.text[:300]}")

    pages = resp.json().get("data", [])
    if not pages:
        sys.exit("no Facebook Pages visible for this token (are you Page admin?)")

    page = next((p for p in pages if p["id"] == args.page_id), None) if args.page_id else pages[0]
    if page is None:
        sys.exit(f"page {args.page_id} not found; available: {[p['id'] for p in pages]}")

    ig = page.get("instagram_business_account") or {}
    if not ig.get("id"):
        print("warning: no Instagram Business account linked to this Page — IG posts will fail")

    store.save_account(
        "meta",
        app_id=app_id or "",
        app_secret=app_secret or "",
        page_id=page["id"],
        page_name=page.get("name", ""),
        ig_user_id=ig.get("id", ""),
        ig_username=ig.get("username", ""),
        access_token=page["access_token"],
    )
    print(f"Meta OK — page {page['id']} ({page.get('name')}), IG {ig.get('username') or 'none'}")


def _tiktok(args) -> None:
    if not args.refresh_token:
        sys.exit("--refresh-token is required (from your app's TikTok OAuth flow; redirect must be HTTPS)")
    store.save_account(
        "tiktok",
        client_key=args.client_key or os.environ.get("TIKTOK_CLIENT_KEY", ""),
        client_secret=args.client_secret or os.environ.get("TIKTOK_CLIENT_SECRET", ""),
        refresh_token=args.refresh_token,
        audited=bool(args.audited),
        privacy_level=args.privacy or "SELF_ONLY",
        username=args.username or "",
    )
    try:
        tiktok_mod.access_token(force=True)
        print("TikTok OK — refresh token exchanged and stored")
    except Exception as exc:  # noqa: BLE001
        print(f"stored, but the refresh failed: {exc}")


def _tiktok_login(args) -> None:
    """Full PKCE flow: print the authorize URL, take the redirect back, store
    the refresh token. TikTok requires an HTTPS redirect registered on the app
    — the browser does the redirect, we only read the pasted URL."""
    client_key = args.client_key or os.environ.get("TIKTOK_CLIENT_KEY")
    client_secret = args.client_secret or os.environ.get("TIKTOK_CLIENT_SECRET")
    redirect_uri = args.redirect_uri or os.environ.get("TIKTOK_REDIRECT_URI")
    if not (client_key and client_secret and redirect_uri):
        sys.exit("need --client-key, --client-secret and --redirect-uri (the HTTPS URL registered on the app)")

    verifier = secrets.token_urlsafe(64)[:64]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_hex(8)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": client_key,
        "scope": "user.info.basic,video.publish",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("\nOpen this URL in a browser, sign in with the TikTok account to post from,\n"
          "approve, then paste the FULL url you were redirected to (even if the page fails to load):\n\n"
          + url + "\n")

    pasted = input("redirect url: ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if "error" in query:
        sys.exit(f"authorization denied: {query['error']}")
    code = (query.get("code") or [""])[0]
    if not code:
        sys.exit("no ?code= in the pasted URL")

    resp = requests.post(tiktok_mod.TOKEN_URL, data={
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"token exchange failed ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    if payload.get("error"):
        sys.exit(f"token exchange error: {payload['error']} {payload.get('error_description', '')}")

    store.save_account(
        "tiktok",
        client_key=client_key,
        client_secret=client_secret,
        refresh_token=payload["refresh_token"],
        audited=bool(args.audited),
        privacy_level=args.privacy or "SELF_ONLY",
        open_id=payload.get("open_id", ""),
    )
    print("TikTok OK — refresh token stored. Scopes:", payload.get("scope", "?"))
    print("Reminder: public posts need the Content Posting audit; until then everything is SELF_ONLY.")


def _show(_args) -> None:
    data = store.accounts()
    print(json.dumps({
        "data_dir": str(store.DATA_DIR),
        "platforms": {k: sorted(v.keys()) for k, v in data.items()},
        "tokens": {k: sorted(v.keys()) for k, v in store.tokens().items()},
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="socialpost.auth")
    sub = parser.add_subparsers(dest="command", required=True)

    yt = sub.add_parser("youtube", help="OAuth loopback flow for YouTube uploads")
    yt.add_argument("--client-id")
    yt.add_argument("--client-secret")
    yt.set_defaults(func=_youtube)

    me = sub.add_parser("meta", help="derive Page token + IG user id from a user token")
    me.add_argument("--user-token", required=True)
    me.add_argument("--app-id")
    me.add_argument("--app-secret")
    me.add_argument("--page-id")
    me.set_defaults(func=_meta)

    tt = sub.add_parser("tiktok", help="store a TikTok refresh token")
    tt.add_argument("--refresh-token", required=True)
    tt.add_argument("--client-key")
    tt.add_argument("--client-secret")
    tt.add_argument("--audited", action="store_true", help="the app passed the Content Posting audit")
    tt.add_argument("--privacy", default="SELF_ONLY")
    tt.add_argument("--username")
    tt.set_defaults(func=_tiktok)

    ttl = sub.add_parser("tiktok-login", help="PKCE login: prints the URL and stores the refresh token")
    ttl.add_argument("--client-key")
    ttl.add_argument("--client-secret")
    ttl.add_argument("--redirect-uri")
    ttl.add_argument("--audited", action="store_true")
    ttl.add_argument("--privacy", default="SELF_ONLY")
    ttl.set_defaults(func=_tiktok_login)

    show = sub.add_parser("show", help="print what is configured")
    show.set_defaults(func=_show)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
