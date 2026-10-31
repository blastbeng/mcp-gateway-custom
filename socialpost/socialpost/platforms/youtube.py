"""YouTube Shorts — Data API v3 videos.insert (resumable upload).

There is no "Shorts" flag: a vertical video up to 3 minutes is classified as
a Short automatically. The adapter only warns when the file won't qualify.
"""

from __future__ import annotations

import os
import time

import requests

from .. import store
from ..media import probe
from .base import PublishError, Result

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
AUTH_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

SESSION = requests.Session()


def access_token(force: bool = False) -> str:
    acc = store.account("youtube")
    if not (acc.get("client_id") and acc.get("client_secret") and acc.get("refresh_token")):
        raise PublishError(
            "youtube not configured: run `python -m socialpost.auth youtube` "
            "(or fill client_id/client_secret/refresh_token in accounts.json)"
        )

    tok = store.access("youtube")
    if not force and tok.get("access_token") and float(tok.get("expires_at") or 0) > time.time() + 120:
        return tok["access_token"]

    resp = SESSION.post(
        TOKEN_URL,
        data={
            "client_id": acc["client_id"],
            "client_secret": acc["client_secret"],
            "refresh_token": acc["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise PublishError(f"youtube token refresh failed ({resp.status_code}): {resp.text[:200]}")

    payload = resp.json()
    store.save_access(
        "youtube",
        access_token=payload["access_token"],
        expires_at=time.time() + int(payload.get("expires_in", 3600)),
    )
    return payload["access_token"]


def publish(
    video_path: str,
    caption: str,
    title: str | None = None,
    privacy: str = "public",
    dry_run: bool = False,
) -> Result:
    info = probe(video_path)
    warnings: list[str] = []
    if info["duration"] > 180:
        warnings.append("longer than 180s: YouTube will treat it as a normal video, not a Short")
    if not info["portrait"]:
        warnings.append("not vertical (9:16): it will not render as a Short")

    first_line = (title or caption or "Short").strip().splitlines()[0]
    body = {
        "snippet": {
            "title": first_line[:95],
            "description": caption or "",
            "categoryId": os.environ.get("YOUTUBE_CATEGORY_ID", "22"),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    if dry_run:
        return Result(
            "youtube",
            "dry-run",
            detail=f"resumable POST {UPLOAD_URL} — {info['size']} bytes, {info['duration']:.1f}s",
            warnings=warnings,
        )

    token = access_token()
    start = SESSION.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(info["size"]),
            "X-Upload-Content-Type": "video/mp4",
        },
        json=body,
        timeout=60,
    )
    if start.status_code != 200:
        raise PublishError(f"youtube upload start failed ({start.status_code}): {start.text[:300]}")

    location = start.headers.get("Location")
    if not location:
        raise PublishError("youtube did not return a resumable upload URL")

    with open(video_path, "rb") as fh:
        put = SESSION.put(
            location,
            data=fh,
            headers={"Content-Type": "video/mp4", "Content-Length": str(info["size"])},
            timeout=(60, 1800),
        )
    if put.status_code not in (200, 201):
        raise PublishError(f"youtube upload failed ({put.status_code}): {put.text[:300]}")

    video_id = put.json().get("id")
    url = f"https://youtube.com/shorts/{video_id}" if info["portrait"] and info["duration"] <= 180 else f"https://youtu.be/{video_id}"
    return Result("youtube", id=video_id, url=url, warnings=warnings)
