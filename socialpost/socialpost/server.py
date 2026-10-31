"""MCP server: three tools, four adapters, no database.

    post_short    publish one file to any subset of the four platforms
    doctor        what is configured, which tokens expire, what the APIs say
    post_history  the local append-only log of published shorts
"""

from __future__ import annotations

import logging
import os
import sys
import time

from mcp.server.fastmcp import FastMCP

from . import store
from .media import MediaError, probe
from .platforms import meta, tiktok, youtube
from .platforms.base import PublishError

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s socialpost %(levelname)s %(message)s")
log = logging.getLogger("socialpost")

mcp = FastMCP("socialpost")

ALIASES = {
    "youtube": "youtube", "yt": "youtube", "shorts": "youtube",
    "instagram": "instagram", "ig": "instagram", "insta": "instagram", "reels": "instagram",
    "facebook": "facebook", "fb": "facebook", "fb-reels": "facebook",
    "tiktok": "tiktok", "tt": "tiktok",
}
DEFAULT_PLATFORMS = ["youtube", "instagram", "facebook", "tiktok"]


def _normalise(platforms) -> list[str]:
    if not platforms:
        return list(DEFAULT_PLATFORMS)
    out: list[str] = []
    for raw in platforms:
        key = str(raw).strip().lower()
        if key not in ALIASES:
            raise ValueError(f"unknown platform '{raw}' (use: {', '.join(sorted(set(ALIASES)))})")
        name = ALIASES[key]
        if name not in out:
            out.append(name)
    return out


@mcp.tool()
def post_short(
    video_path: str,
    caption: str,
    platforms: list[str] | None = None,
    title: str | None = None,
    privacy: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Publish one vertical short to YouTube Shorts, Instagram Reels, Facebook
    Reels and/or TikTok.

    video_path: path the container can read (/opt/projects or /tmp, mounted
    read-only). platforms: any subset of youtube, instagram, facebook, tiktok
    (aliases yt/ig/fb/tt). title: YouTube/Facebook title, defaults to the first
    line of caption. privacy: public|unlisted|private for YouTube;
    SELF_ONLY|PUBLIC_TO_EVERYONE|... for TikTok. dry_run: validate the media
    and print the exact HTTP call without uploading or publishing anything.

    Each platform is attempted independently: one failure does not stop the
    others, and the result tells you exactly which one failed and why.
    """
    selected = _normalise(platforms)
    info = probe(video_path)  # raises MediaError on missing/invalid file

    results: list[dict] = []
    for name in selected:
        started = time.time()
        try:
            if name == "youtube":
                res = youtube.publish(video_path, caption, title=title,
                                      privacy=privacy or "public", dry_run=dry_run)
            elif name == "instagram":
                res = meta.publish_instagram(caption, video_path, dry_run=dry_run)
            elif name == "facebook":
                res = meta.publish_facebook(caption, video_path, title=title, dry_run=dry_run)
            else:
                res = tiktok.publish(video_path, caption, privacy=privacy, dry_run=dry_run)
            payload = res.as_dict()
        except PublishError as exc:
            payload = {"platform": name, "status": "error", "detail": str(exc), "warnings": []}
        except Exception as exc:  # network/DNS/timeout: never lose the other results
            log.exception("%s failed", name)
            payload = {"platform": name, "status": "error",
                       "detail": f"{type(exc).__name__}: {exc}", "warnings": []}

        payload["seconds"] = round(time.time() - started, 1)
        results.append(payload)
        log.info("%s -> %s", name, payload["status"])

    if not dry_run and any(r["status"] == "ok" for r in results):
        store.log_post({
            "file": info["path"],
            "size": info["size"],
            "duration": round(info["duration"], 1),
            "caption": (caption or "")[:200],
            "results": results,
        })

    return {
        "file": {k: info[k] for k in ("path", "size", "duration", "width", "height", "portrait")},
        "dry_run": dry_run,
        "results": results,
        "summary": {status: sum(1 for r in results if r["status"] == status)
                    for status in ("ok", "dry-run", "error")},
    }


@mcp.tool()
def doctor() -> dict:
    """Report configuration and token state for the four platforms, without
    publishing anything. Use it when a post fails or before the first run:
    it says what is missing and when each token expires."""
    now = time.time()
    report: dict = {"data_dir": str(store.DATA_DIR), "api_versions": {"meta": meta.api_version()}}

    def token_state(platform: str) -> dict:
        acc = store.account(platform)
        tok = store.access(platform)
        expires = float(tok.get("expires_at") or 0)
        return {
            "configured": bool(acc),
            "has_access_token": bool(tok.get("access_token")),
            "access_token_expires_in_s": int(expires - now) if expires else None,
            "account_fields": sorted(acc.keys()),
        }

    report["youtube"] = token_state("youtube")
    report["instagram"] = {**token_state("meta"),
                           "ig_user_id": bool(store.account("meta").get("ig_user_id"))}
    report["facebook"] = {**token_state("meta"),
                          "page_id": bool(store.account("meta").get("page_id"))}
    report["tiktok"] = {**token_state("tiktok"),
                        "audited": bool(store.account("tiktok").get("audited")),
                        "privacy_level": store.account("tiktok").get("privacy_level", "SELF_ONLY")}

    try:
        probe_path = os.environ.get("SOCIALPOST_SELFTEST_FILE", "")
        report["media_probe_ok"] = bool(probe(probe_path)) if probe_path else "set SOCIALPOST_SELFTEST_FILE to test"
    except MediaError as exc:
        report["media_probe_ok"] = f"error: {exc}"

    report["data_dir_writable"] = os.access(store.DATA_DIR, os.W_OK)
    report["notes"] = [
        "Instagram requires a Business/Creator account linked to a Page.",
        "YouTube: an OAuth app in 'Testing' expires refresh tokens after 7 days (publish the app).",
        "TikTok: unaudited apps can only publish SELF_ONLY until the Content Posting audit passes.",
    ]
    return report


@mcp.tool()
def post_history(limit: int = 10) -> dict:
    """Return the last published shorts recorded in the local log (platform,
    id, url, warnings). Empty until something is actually published."""
    return {"entries": store.history(limit)}


def main() -> None:
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.info("socialpost MCP starting (data=%s)", store.DATA_DIR)
    mcp.run()


if __name__ == "__main__":
    main()
