"""Media probe via PyAV — replaces a system ffmpeg install.

PyAV ships its own FFmpeg libraries in the wheel (~30 MB), so the image stays
around 200 MB instead of ~850 MB with Debian's ffmpeg package. We only need
duration, resolution and container: enough for every platform's limits.
"""

from __future__ import annotations

import pathlib

import av


class MediaError(Exception):
    pass


def probe(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.is_file():
        raise MediaError(f"file not found: {path}")

    try:
        with av.open(str(p), mode="r") as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                raise MediaError("no video stream in file")

            duration = float(container.duration / 1_000_000) if container.duration else 0.0
            if duration <= 0 and stream.duration and stream.time_base:
                duration = float(stream.duration * stream.time_base)

            ctx = stream.codec_context
            width = int(ctx.width or 0)
            height = int(ctx.height or 0)
            container_name = container.format.name or ""
            fps = stream.average_rate
    except MediaError:
        raise
    except Exception as exc:  # av.FFmpegError and friends
        raise MediaError(f"cannot read media: {exc}") from exc

    return {
        "path": str(p),
        "size": p.stat().st_size,
        "duration": duration,
        "width": width,
        "height": height,
        "portrait": height > width,
        "aspect": round(width / height, 4) if height else 0,
        "codec": ctx.name,
        "container": container_name,
        "fps": str(fps) if fps else None,
    }
