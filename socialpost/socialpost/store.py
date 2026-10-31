"""Config, token and post-log persistence.

Everything lives in SOCIALPOST_DATA_DIR (bind mount, files chmod 600):

    accounts.json  app credentials + long-lived/refresh tokens. Written once
                   by ``python -m socialpost.auth`` or by hand.
    tokens.json    short-lived access tokens; the adapters refresh and rewrite
                   them in place (TikTok rotates its refresh token, so this
                   file can also hold a rotated value).
    posts.jsonl    append-only history, one JSON object per published short.

Keeping rotating secrets here instead of .env is deliberate: .env is mounted
read-only into the gateway and cannot be updated by a running container.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time

DATA_DIR = pathlib.Path(os.environ.get("SOCIALPOST_DATA_DIR", "/data"))
ACCOUNTS_PATH = DATA_DIR / "accounts.json"
TOKENS_PATH = DATA_DIR / "tokens.json"
LOG_PATH = DATA_DIR / "posts.jsonl"


def _read(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc


def _write(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --- accounts (static app credentials, long-lived tokens) -------------------


def accounts() -> dict:
    return _read(ACCOUNTS_PATH, {})


def account(platform: str) -> dict:
    return accounts().get(platform, {})


def save_account(platform: str, **values) -> None:
    data = accounts()
    data[platform] = {**data.get(platform, {}), **values}
    _write(ACCOUNTS_PATH, data)


# --- access tokens (short-lived, refreshed at runtime) ----------------------


def tokens() -> dict:
    return _read(TOKENS_PATH, {})


def access(platform: str) -> dict:
    return tokens().get(platform, {})


def save_access(platform: str, **values) -> None:
    data = tokens()
    data[platform] = {**data.get(platform, {}), **values}
    _write(TOKENS_PATH, data)


# --- history ----------------------------------------------------------------


def log_post(record: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as fh:
        fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}) + "\n")
    os.chmod(LOG_PATH, 0o600)


def history(limit: int = 10) -> list[dict]:
    try:
        lines = LOG_PATH.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines[-max(1, limit):]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
