#!/usr/bin/env python3
"""Entrypoint for the model-council MCP image.

Upstream ``model_council.server`` reads its roster exactly once, at module
import::

    COUNCIL = load_council()                    # server.py:41
    ModelId = Literal[tuple(COUNCIL.ids)] ...   # server.py:52

so ``model-council-sync`` had to restart the whole gateway every time the
LiteLLM roster changed.  Via ``docker-compose@mcp.service``'s
``--abort-on-container-exit`` that restart tore down the *entire* MCP stack,
made the gateway respawn its long-lived stdio children, and (because the
gateway is SIGKILLed mid-teardown) leaked them.

This shim removes the need for that restart, so the gateway process — and
with it the child containers it owns — can stay up indefinitely:

* ``COUNCIL`` becomes a proxy that re-reads the config file whenever its
  mtime changes: the roster is live without a restart;
* the import-time ``ModelId`` enum is dropped (``str``).  An enum baked into
  the tool schema at import goes stale exactly like the roster did, and it is
  what a restart used to refresh.  Unknown ids are still rejected gracefully
  at call time by the server's own ``_unknown()`` / ``Council.resolve()``,
  and ``list_council`` reports the live roster.

``model-council-sync`` is therefore run with ``RESTART_GATEWAY=false``.
"""

from __future__ import annotations

import os
import threading

import model_council.config as _config

DEFAULT_CONFIG = "/config/config.json"


def _mtime(path: str | None):
    if not path:
        return None
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _path_of(council) -> str | None:
    """The file the council actually loaded, if it came from one."""
    source = getattr(council, "source", "") or ""
    if source.startswith("file:"):
        return source[len("file:"):].strip() or None
    return os.environ.get("COUNCIL_CONFIG", DEFAULT_CONFIG).strip() or None


class _Stub:
    """Returned by the patched ``load_council()`` during server import.

    Only line 41 (the assignment) and line 52 (``COUNCIL.ids`` -> empty ->
    ``str``) of server.py ever see it; ``ReloadingCouncil`` replaces the
    module global immediately after the import.
    """

    ids: list = []
    members: dict = {}
    warnings: list = []
    source: str = "(stub)"

    def get(self, _model):
        return None

    def resolve(self, _models):
        return [], []


class ReloadingCouncil:
    """``Council`` façade that re-reads the config file when it changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._council = _config.load_council()
        self._path = _path_of(self._council)
        self._mtime = _mtime(self._path)

    def _current(self):
        if self._path is not None:
            seen = _mtime(self._path)
            if seen != self._mtime:
                with self._lock:
                    if _mtime(self._path) != self._mtime:
                        self._council = _config.load_council()
                        self._mtime = _mtime(self._path)
        return self._council

    def __getattr__(self, name):
        # Only attributes the instance itself does not carry reach here.
        return getattr(self._current(), name)


def _install() -> None:
    real_load = _config.load_council
    # Import-time stub first, so `ModelId = str` instead of a frozen enum.
    _config.load_council = lambda: _Stub()
    import model_council.server as server  # import must follow the patch
    # Restore the real loader and give the server a live roster.
    _config.load_council = real_load
    server.load_council = real_load
    server.COUNCIL = ReloadingCouncil()


def main() -> None:
    _install()
    import model_council.server as server

    server.main()


if __name__ == "__main__":
    main()
