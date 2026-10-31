"""Instagram Reels + Facebook Reels — Meta Graph API, resumable uploads.

Both use one long-lived Page token (instagram_content_publish for IG,
pages_manage_posts for the Page). No public video URL is needed: the file is
uploaded with the rupload protocol, so the Pi is never exposed.

accounts.json["meta"]:
    app_id, app_secret, page_id, ig_user_id, access_token
"""

from __future__ import annotations

import os
import time

import requests

from .. import store
from ..media import probe
from .base import PublishError, Result

GRAPH = "https://graph.facebook.com"
SESSION = requests.Session()


def api_version() -> str:
    return os.environ.get("META_API_VERSION", "v23.0")


def _base() -> str:
    return f"{GRAPH}/{api_version()}"


def _token() -> str:
    acc = store.account("meta")
    token = acc.get("access_token")
    if not token:
        raise PublishError(
            "meta not configured: run `python -m socialpost.auth meta --user-token <token>` "
            "(or fill access_token/page_id/ig_user_id in accounts.json)"
        )
    return token


def _json(resp: requests.Response, what: str) -> dict:
    if resp.status_code >= 400:
        raise PublishError(f"{what} failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _upload_binary(upload_url: str, video_path: str, token: str) -> None:
    size = os.path.getsize(video_path)
    with open(video_path, "rb") as fh:
        resp = SESSION.post(
            upload_url,
            data=fh,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
                "Content-Type": "video/mp4",
            },
            timeout=(60, 1800),
        )
    if resp.status_code >= 400:
        raise PublishError(f"meta binary upload failed ({resp.status_code}): {resp.text[:300]}")


def _poll_container(container_id: str, token: str, timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = SESSION.get(
            f"{_base()}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=30,
        )
        data = _json(resp, "meta container status")
        code = data.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise PublishError(f"meta processing failed: {data.get('status')}")
        time.sleep(5)
    raise PublishError("meta container did not finish processing in time")


def publish_instagram(caption: str, video_path: str, dry_run: bool = False) -> Result:
    acc = store.account("meta")
    ig_user = acc.get("ig_user_id")

    info = probe(video_path)
    warnings: list[str] = []
    if not info["portrait"]:
        warnings.append("not vertical: Instagram Reels expect 9:16")
    if info["duration"] > 900:
        warnings.append("over 15 minutes: Instagram may reject the Reel")

    if dry_run:
        return Result("instagram", "dry-run",
                      detail=f"POST {_base()}/{ig_user or '<ig_user_id missing>'}/media (REELS, {info['size']} bytes)",
                      warnings=warnings)

    if not ig_user:
        raise PublishError("meta.ig_user_id missing (run the auth helper)")

    token = _token()
    create = SESSION.post(
        f"{_base()}/{ig_user}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption or "",
            "share_to_feed": "true",
            "access_token": token,
        },
        timeout=60,
    )
    created = _json(create, "instagram container create")
    container_id, upload_url = created.get("id"), created.get("uri")
    if not container_id or not upload_url:
        raise PublishError(f"instagram did not return a container: {created}")

    _upload_binary(upload_url, video_path, token)
    _poll_container(container_id, token)

    published = SESSION.post(
        f"{_base()}/{ig_user}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=60,
    )
    media_id = _json(published, "instagram publish").get("id")
    return Result("instagram", id=media_id,
                  url="https://www.instagram.com/reel/" if media_id else None,
                  warnings=warnings)


def publish_facebook(caption: str, video_path: str, title: str | None = None, dry_run: bool = False) -> Result:
    acc = store.account("meta")
    page_id = acc.get("page_id")

    info = probe(video_path)
    warnings: list[str] = []
    if not info["portrait"]:
        warnings.append("not vertical: Facebook Reels expect 9:16")
    if info["duration"] > 90:
        warnings.append("longer than 90s: Facebook may reject it as a Reel")

    if dry_run:
        return Result("facebook", "dry-run",
                      detail=f"POST {_base()}/{page_id or '<page_id missing>'}/video_reels (start/upload/finish, {info['size']} bytes)",
                      warnings=warnings)

    if not page_id:
        raise PublishError("meta.page_id missing (run the auth helper)")

    token = _token()
    start = SESSION.post(
        f"{_base()}/{page_id}/video_reels",
        data={"upload_phase": "start", "access_token": token},
        timeout=60,
    )
    started = _json(start, "facebook reel start")
    video_id, upload_url = started.get("video_id"), started.get("upload_url")
    if not video_id or not upload_url:
        raise PublishError(f"facebook did not return an upload URL: {started}")

    _upload_binary(upload_url, video_path, token)

    finish = SESSION.post(
        f"{_base()}/{page_id}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption or "",
            "title": (title or caption or "Reel").strip().splitlines()[0][:95],
            "access_token": token,
        },
        timeout=60,
    )
    _json(finish, "facebook reel finish")
    return Result("facebook", id=video_id, url=f"https://www.facebook.com/reel/{video_id}", warnings=warnings)


def refresh_long_lived_token() -> dict:
    """Exchange the stored long-lived token for a fresh 60-day one."""
    acc = store.account("meta")
    if not (acc.get("app_id") and acc.get("app_secret") and acc.get("access_token")):
        raise PublishError("meta app_id/app_secret/access_token missing")
    resp = SESSION.get(
        f"{_base()}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": acc["app_id"],
            "client_secret": acc["app_secret"],
            "fb_exchange_token": acc["access_token"],
        },
        timeout=30,
    )
    data = _json(resp, "meta token refresh")
    store.save_account("meta", access_token=data["access_token"])
    return data
