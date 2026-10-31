"""Shared types for the four adapters."""

from __future__ import annotations

from dataclasses import dataclass, field


class PublishError(Exception):
    """A platform refused the upload; the message is safe to show to the user."""


@dataclass
class Result:
    platform: str
    status: str = "ok"  # ok | skipped | error | dry-run
    id: str | None = None
    url: str | None = None
    detail: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "status": self.status,
            "id": self.id,
            "url": self.url,
            "detail": self.detail,
            "warnings": self.warnings,
        }
