"""TikTok — Content Posting API, direct post with FILE_UPLOAD (chunked).

An unaudited TikTok app can only publish SELF_ONLY (private). That is a
platform rule, not a bug: accounts.json["tiktok"]["audited"] = true unlocks
the public privacy levels, and until then requests for public are downgraded
with a warning instead of failing.

accounts.json["tiktok"]:
    client_key, client_secret, refresh_token, audited (bool), privacy_level
"""

from __future__ import annotations

import math
import os
import time

import requests

from .. import store
from ..media import probe
from .base import PublishError, Result

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024

SESSION = requests.Session()


def _creds() -> dict:
    acc = store.account("tiktok")
    if not (acc.get("client_key") and acc.get("client_secret") and acc.get("refresh_token")):
        raise PublishError(
            "tiktok not configured: run `python -m socialpost.auth tiktok --refresh-token <token>`"
        )
    return acc


def access_token(force: bool = False) -> str:
    acc = _creds()
    tok = store.access("tiktok")
    if not force and tok.get("access_token") and float(tok.get("expires_at") or 0) > time.time() + 120:
        return tok["access_token"]

    resp = SESSION.post(
        TOKEN_URL,
        data={
            "client_key": acc["client_key"],
            "client_secret": acc["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": acc["refresh_token"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise PublishError(f"tiktok token refresh failed ({resp.status_code}): {resp.text[:200]}")

    data = resp.json()
    if data.get("error"):
        raise PublishError(f"tiktok token refresh error: {data['error']} {data.get('error_description','')[:200]}")

    # TikTok rotates the refresh token: persist it or the next refresh fails.
    if data.get("refresh_token"):
        store.save_account("tiktok", refresh_token=data["refresh_token"])
    store.save_access(
        "tiktok",
        access_token=data["access_token"],
        expires_at=time.time() + int(data.get("expires_in", 86400)),
    )
    return data["access_token"]


def _privacy(requested: str | None) -> tuple[str, list[str]]:
    acc = store.account("tiktok")
    allowed = {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}
    wanted = (requested or acc.get("privacy_level") or "SELF_ONLY").upper()
    if wanted not in allowed:
        raise PublishError(f"tiktok privacy_level must be one of {sorted(allowed)}")
    if wanted != "SELF_ONLY" and not acc.get("audited"):
        return "SELF_ONLY", [f"app not audited by TikTok: {wanted} downgraded to SELF_ONLY (private)"]
    return wanted, []


def publish(
    video_path: str,
    caption: str,
    privacy: str | None = None,
    dry_run: bool = False,
) -> Result:
    info = probe(video_path)
    warnings: list[str] = []
    level, privacy_warnings = _privacy(privacy)
    warnings.extend(privacy_warnings)

    if info["duration"] < 3:
        raise PublishError("tiktok requires at least 3 seconds of video")
    if info["duration"] > 600:
        warnings.append("longer than 10 minutes: TikTok may shorten the limit for this account")

    size = info["size"]
    chunk = size if size <= MIN_CHUNK else min(MAX_CHUNK, size)
    chunks = max(1, math.ceil(size / chunk))

    if dry_run:
        return Result(
            "tiktok",
            "dry-run",
            detail=f"POST {INIT_URL} — {size} bytes in {chunks} chunk(s), privacy={level}",
            warnings=warnings,
        )

    token = access_token()
    init = SESSION.post(
        INIT_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "post_info": {
                "title": (caption or "")[:2200],
                "privacy_level": level,
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk,
                "total_chunk_count": chunks,
            },
        },
        timeout=60,
    )
    if init.status_code >= 400:
        raise PublishError(f"tiktok publish init failed ({init.status_code}): {init.text[:300]}")
    body = init.json()
    if body.get("error", {}).get("code") not in (None, "ok"):
        raise PublishError(f"tiktok publish init error: {body['error']}")

    publish_id = body.get("data", {}).get("publish_id")
    upload_url = body.get("data", {}).get("upload_url")
    if not publish_id or not upload_url:
        raise PublishError(f"tiktok did not return a publish_id: {body}")

    with open(video_path, "rb") as fh:
        for index in range(chunks):
            start = index * chunk
            payload = fh.read(chunk)
            end = start + len(payload) - 1
            resp = SESSION.put(
                upload_url,
                data=payload,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(payload)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
                timeout=(60, 1800),
            )
            if resp.status_code >= 400:
                raise PublishError(f"tiktok chunk {index + 1}/{chunks} failed ({resp.status_code}): {resp.text[:200]}")

    deadline = time.time() + 600
    while time.time() < deadline:
        status = SESSION.post(
            STATUS_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"publish_id": publish_id},
            timeout=30,
        )
        data = status.json().get("data", {}) if status.status_code < 400 else {}
        state = data.get("status")
        if state == "PUBLISH_COMPLETE":
            return Result(
                "tiktok",
                id=publish_id,
                url=f"https://www.tiktok.com/@{store.account('tiktok').get('username','me')}" if level == "PUBLIC_TO_EVERYONE" else None,
                detail=f"published with privacy={level}",
                warnings=warnings,
            )
        if state in ("FAILED", "PUBLISH_FAILED"):
            raise PublishError(f"tiktok publish failed: {data.get('fail_reason') or data}")
        time.sleep(5)
    raise PublishError("tiktok publish still processing after 10 minutes (publish_id kept in history)")
