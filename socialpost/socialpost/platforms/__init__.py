"""Platform adapters. Each exposes ``publish(video_path, caption, ...) -> Result``."""

from . import meta, tiktok, youtube  # noqa: F401

__all__ = ["youtube", "meta", "tiktok"]
