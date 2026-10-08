#!/usr/bin/env python3
"""Keep the inference4free 'auto' seats of the model council in step with litellm.

The council config (model-council-data/config.json) holds two kinds of members:

  * hand-written members (openrouter/free, groq/*, ollama-cloud/*, ...) —
    these are owned by the human and are never touched;
  * the inference4free auto-router seats (inference4free/<provider>/auto and
    the bare inference4free/auto) — these are owned by this sync.

New inference4free providers appear on litellm over time, and some disappear;
the council server reads its roster once at startup, so the file has to be
kept current for the council to ever see a new seat.

Every cycle this script:

  1. lists the models litellm currently exposes (GET /v1/models);
  2. adds a seat for every auto router that is missing — optionally only
     after a tiny "ping" chat completion proves the seat actually answers,
     because many of these free upstreams are reverse-engineered and dead;
  3. drops seats whose model litellm no longer exposes, but only after the
     model has been missing for REMOVE_AFTER_MISSES consecutive cycles, so a
     litellm hiccup does not churn the roster (and restart the gateway) for
     nothing;
  4. rewrites config.json atomically (previous copy kept as .bak) only when
     something actually changed, and then restarts the gateway so the council
     server picks the new roster up. The session proxy heals client sessions
     across that restart, so callers never see it.

Credentials come from the config's own providers.litellm block — no new
secrets — and can be overridden with LITELLM_BASE_URL / LITELLM_API_KEY.

Everything is a plain environment knob; defaults suit the compose service.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import time
import urllib.error
import urllib.request

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.json")
STATE_PATH = os.environ.get("STATE_PATH", "/config/.sync-state.json")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "3600"))
RUN_ONCE = os.environ.get("RUN_ONCE", "").lower() in ("1", "true", "yes", "on")
# Bare global router + one router per provider: inference4free/auto,
# inference4free/deepseek/auto, ... (specific models are NOT sync-owned).
I4F_PATTERN = re.compile(os.environ.get("I4F_PATTERN", r"^inference4free/(?:[^/]+/)?auto$"))
PROBE_ENABLED = os.environ.get("PROBE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "25"))
REMOVE_AFTER_MISSES = max(1, int(os.environ.get("REMOVE_AFTER_MISSES", "2")))
RESTART_GATEWAY = os.environ.get("RESTART_GATEWAY", "true").lower() in ("1", "true", "yes", "on")
GATEWAY_CONTAINER = os.environ.get("GATEWAY_CONTAINER", "mcp-gateway")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

_STATE_DEFAULT = {"grace": {}}


def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


# --------------------------------------------------------------------------- #
# Config / state I/O
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"{CONFIG_PATH} is not a JSON object")
    return cfg


def write_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
    os.replace(tmp, CONFIG_PATH)


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) and isinstance(state.get("grace"), dict):
            return state
        log(f"state file {STATE_PATH} has an unexpected shape — starting fresh")
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"state file {STATE_PATH} unreadable ({type(e).__name__}: {e}) — starting fresh")
    return json.loads(json.dumps(_STATE_DEFAULT))


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------- #
# litellm
# --------------------------------------------------------------------------- #
def litellm_endpoint(cfg: dict) -> tuple[str, str]:
    """Connection details for litellm — env overrides, else the config's own."""
    base = os.environ.get("LITELLM_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("LITELLM_API_KEY", "").strip()
    provider = (cfg.get("providers") or {}).get("litellm") or {}
    base = base or str(provider.get("base_url", "")).strip().rstrip("/")
    key = key or str(provider.get("api_key", "")).strip()
    if not base or not key:
        raise ValueError("no LiteLLM base_url/api_key — set providers.litellm in "
                         "the config, or LITELLM_BASE_URL / LITELLM_API_KEY")
    return base, key


def list_models(base: str, key: str) -> set[str]:
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return {m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")}


def _probe_once(base: str, key: str, model: str, body: dict) -> tuple[bool, str]:
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            return resp.status == 200, ""
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:160]
        except Exception:
            pass
        return False, f"HTTP {e.code} {detail}".rstrip()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def probe_alive(base: str, key: str, model: str) -> bool:
    """One-token chat completion; a second try without max_tokens covers
    upstreams that choke on the parameter but would otherwise answer."""
    detail = ""
    for body in ({"model": model, "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 1},
                 {"model": model, "messages": [{"role": "user", "content": "ping"}]}):
        ok, detail = _probe_once(base, key, model, body)
        if ok:
            return True
    log(f"  probe {model} failed: {detail}")
    return False


# --------------------------------------------------------------------------- #
# docker
# --------------------------------------------------------------------------- #
class _UnixHTTP(http.client.HTTPConnection):
    """HTTP over the docker socket, stdlib-only."""

    def __init__(self, sock_path: str):
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(60)
        sock.connect(self._sock_path)
        self.sock = sock


def restart_gateway() -> bool:
    try:
        conn = _UnixHTTP(DOCKER_SOCK)
        conn.request("POST", f"/containers/{GATEWAY_CONTAINER}/restart?t=5")
        resp = conn.getresponse()
        body = resp.read().decode(errors="replace")[:200]
        if resp.status in (200, 204):
            log(f"restarted container {GATEWAY_CONTAINER}")
            return True
        log(f"ERROR restarting {GATEWAY_CONTAINER}: HTTP {resp.status} {body}")
    except Exception as e:
        log(f"ERROR restarting {GATEWAY_CONTAINER}: {type(e).__name__}: {e}")
    return False


# --------------------------------------------------------------------------- #
# One sync cycle
# --------------------------------------------------------------------------- #
def sync_cycle() -> None:
    cfg = load_config()
    base, key = litellm_endpoint(cfg)
    live = list_models(base, key)

    members = cfg.get("members")
    if not isinstance(members, list):
        log(f"{CONFIG_PATH} has no members list — nothing to sync")
        return

    state = load_state()
    grace: dict = state["grace"]

    # Split the roster: sync-owned auto seats vs hand-written members.
    keepers: list = []
    owned: dict[str, dict] = {}
    for entry in members:
        if isinstance(entry, dict) and I4F_PATTERN.match(str(entry.get("model", ""))):
            owned[str(entry["model"])] = entry
        else:
            keepers.append(entry)

    # ADD: every live auto router the config does not have yet. Models outside
    # the pattern (the hundreds of other models litellm exposes, embedding
    # models included) are none of this sync's business.
    added: list[str] = []
    seats: dict[str, dict] = {}
    for model in sorted(live):
        if not I4F_PATTERN.match(model):
            continue
        if model in owned:
            seats[model] = owned[model]
            continue
        if PROBE_ENABLED and not probe_alive(base, key, model):
            continue  # retried next cycle
        seats[model] = {"id": model, "provider": "litellm", "model": model}
        added.append(model)

    # REMOVE: owned seats whose model vanished — after REMOVE_AFTER_MISSES
    # consecutive cycles without it, so transient litellm gaps don't churn.
    removed: list[str] = []
    for model in sorted(owned):
        if model in live:
            grace.pop(model, None)
            continue
        counter = grace.setdefault(model, {"misses": 0})
        counter["misses"] += 1
        counter["last_missing"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if counter["misses"] >= REMOVE_AFTER_MISSES:
            removed.append(model)
            seats.pop(model, None)
            grace.pop(model, None)
        else:
            log(f"{model} missing from litellm ({counter['misses']}/{REMOVE_AFTER_MISSES}) — in grace")
            seats[model] = owned[model]

    if added:
        log("added: " + ", ".join(added))
    if removed:
        log("removed: " + ", ".join(removed))
    save_state(state)

    if not added and not removed:
        log(f"roster in sync ({len(keepers)} hand-written + {len(seats)} "
            f"inference4free auto seats) — no changes")
        return

    cfg["members"] = keepers + [seats[m] for m in sorted(seats)]
    write_config(cfg)
    log(f"config rewritten: {len(keepers)} hand-written + {len(seats)} "
        f"inference4free auto seats (backup: {CONFIG_PATH}.bak)")
    if RESTART_GATEWAY:
        restart_gateway()
    else:
        log("RESTART_GATEWAY=false — restart the gateway manually to apply")


def main() -> None:
    log(f"model-council-sync started (interval={SYNC_INTERVAL}s, probe={PROBE_ENABLED}, "
        f"remove_after={REMOVE_AFTER_MISSES} misses, restart={RESTART_GATEWAY})")
    while True:
        try:
            sync_cycle()
        except Exception as e:
            log(f"ERROR in sync cycle: {type(e).__name__}: {e}")
        if RUN_ONCE:
            break
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
