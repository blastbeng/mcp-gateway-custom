"""ffprobe wrapper: the few facts every platform needs before an upload."""

from __future__ import annotations

import json
import pathlib
import subprocess


class MediaError(Exception):
    pass


def probe(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.is_file():
        raise MediaError(f"file not found: {path}")

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(p)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - image always has it
        raise MediaError("ffprobe not installed") from exc

    if out.returncode != 0:
        raise MediaError(f"ffprobe failed: {out.stderr.strip()[:300]}")

    data = json.loads(out.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("no video stream in file")

    fmt = data.get("format", {})
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    return {
        "path": str(p),
        "size": p.stat().st_size,
        "duration": float(fmt.get("duration") or 0),
        "width": width,
        "height": height,
        "portrait": height > width,
        "aspect": round(width / height, 4) if height else 0,
        "codec": video.get("codec_name"),
        "container": fmt.get("format_name", ""),
        "fps": video.get("avg_frame_rate"),
    }
