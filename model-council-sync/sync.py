#!/usr/bin/env python3
"""Keep the sync-owned seats of the model council in step with litellm.

The council config (model-council-data/config.json) holds two kinds of members:

  * hand-written members — owned by the human, never touched by this sync
    (anything no rule claims, plus every id a rule explicitly excludes);
  * rule-owned seats — managed here, added and removed dynamically.

Every cycle this script:

  1. lists the models litellm currently exposes (GET /v1/models);
  2. for every rule (see model-council-data/sync-rules.json, auto-created
     with defaults on first run), offers each live model the rule claims:

       - the size engine classifies the model from its name — an explicit
         parameter count (8b, 27b, 675b...) against the rule's max_params_b,
         else the rule's small_keywords (flash, mini, nano, instant...);
         unknown size means a model is never added;
       - the free engine decides whether the model is actually usable
         without credits (we never top these providers up), by probing it.
         Only a probe that really answers adds a model: "ok" demands a
         well-formed completion body, not just HTTP 200 — an errored or
         throttled candidate is left out this cycle and simply retried
         (nothing is written off: a model that starts answering later is
         picked up on a later cycle):

           200 + valid completion body -> ok    (works — the only verdict that adds)
           200 but malformed/error body-> unknown (transient: skipped, no penalty)
           429          -> free      (rate-limited: still the free tier;
                                      healthy for a seated model, but an
                                      errored probe — not enough to add)
           402/401/403  -> paid      (needs credits/entitlement)
           404          -> gone      (provider stopped serving it)
           anything else-> unknown   (transient: skipped, no penalty)

       a rule with free:"always" (inference4free) probes candidates for
       aliveness only; a rule with free:"probe" probes for free vs paid.
       Every rule also re-probes its existing seats every cycle
       (probe_existing, on by default — a rule may opt out to spare the
       provider's daily free quota): a seat that answers paid leaves the
       roster the same cycle — the roster must never hold a paid model —
       and comes back as a candidate the cycle it answers ok again.
       Admission itself is the same for every rule: only an "ok" probe
       ever adds a candidate;
  3. strikes seats that vanish from litellm or probe as paid/gone. A paid
     probe removes the seat the same cycle (a paid model must never sit
     in the roster); gone/missing take REMOVE_AFTER_MISSES consecutive
     strikes first, so a transient provider hiccup does not churn the
     roster (and restart the gateway);
     a rule whose seats ALL come back paid in one cycle is treated as a
     gateway-wide failure, not as N dead models (circuit breaker);
     candidates get the same economy: a model rejected with a stable
     verdict (paid/gone) accumulates candidate strikes and after
     CANDIDATE_STRIKES of them stops being probed for
     CANDIDATE_RETRY_HOURS — then it is retried once and re-cooled if
     still bad; unknown verdicts never count, so genuinely transient
     failures keep being retried every cycle. Nothing is permanent:
     every rejection is undone the cycle the model answers again;
  4. rewrites config.json atomically (previous copy kept as .bak) only when
     something actually changed, and then restarts the gateway so the
     council server — which reads its roster once at startup — picks the
     new roster up. The session proxy heals client sessions across that
     restart, so callers never see it.

Credentials come from the config's own providers.litellm block — no new
secrets — and can be overridden with LITELLM_BASE_URL / LITELLM_API_KEY.
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
RULES_PATH = os.environ.get("RULES_PATH", "/config/sync-rules.json")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "3600"))
RUN_ONCE = os.environ.get("RUN_ONCE", "").lower() in ("1", "true", "yes", "on")
PROBE_ENABLED = os.environ.get("PROBE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "25"))
REMOVE_AFTER_MISSES = max(1, int(os.environ.get("REMOVE_AFTER_MISSES", "2")))
CANDIDATE_STRIKES = max(1, int(os.environ.get("CANDIDATE_STRIKES", "2")))
CANDIDATE_RETRY_HOURS = float(os.environ.get("CANDIDATE_RETRY_HOURS", "24"))
RESTART_GATEWAY = os.environ.get("RESTART_GATEWAY", "true").lower() in ("1", "true", "yes", "on")
GATEWAY_CONTAINER = os.environ.get("GATEWAY_CONTAINER", "mcp-gateway")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

# Default engine rules — written to RULES_PATH on first run, editable there.
# pattern  : which model ids the rule owns (first matching rule wins)
# exclude  : ids that stay hand-written even if the pattern matches
# max_params_b: candidate size ceiling from the name ("20b", "675b"...);
#           null disables the size filter
# small_keywords: size fallback when the name carries no parameter count
# allow    : regex (searched) on the model's last segment, narrowing
#            candidates further — e.g. ":free$" for openrouter
# free     : "always" (free by construction, probe = aliveness only) or
#            "probe" (probe decides free vs paid); either way a candidate
#            is seated only on a probe that really answers ("ok")
# probe_existing: re-probe seated models every cycle (default true for
#            every rule — a seat that turns paid must leave the roster,
#            not linger); set false only when a re-probe would burn the
#            provider's daily free quota and a lingering paid seat is
#            acceptable — candidates are probed once regardless
DEFAULT_RULES = {
    "rules": [
        {"id": "inference4free", "pattern": r"^inference4free/(?:[^/]+/)?auto$",
         "max_params_b": None, "free": "always"},
        {"id": "groq", "pattern": r"^groq/", "max_params_b": 32, "free": "probe"},
        {"id": "ollama-cloud", "pattern": r"^ollama-cloud/", "max_params_b": 32, "free": "probe"},
        {"id": "gemini", "pattern": r"^gemini/",
         "max_params_b": 32, "free": "probe",
         "allow": r"^gemini(-[\d.]+)?-flash(-lite)?$"
                  r"|^gemini-flash(-lite)?-latest$"
                  r"|^gemma-\d+-\d+b"},
        # openrouter: the ":free" suffix is the cost signal (the probe
        # confirms it); openrouter/free stays hand-written. Seats are
        # re-probed like every rule's: a :free seat that starts charging
        # must leave the roster. The hourly 1-token probe costs a sliver
        # of the daily free quota — the price of never seating a paid model.
        {"id": "openrouter", "pattern": r"^openrouter/",
         "exclude": ["openrouter/free"],
         "max_params_b": None, "free": "probe",
         "allow": r":free$"},
    ]
}

_SMALL_KEYWORDS = ("flash", "lite", "mini", "nano", "instant", "small", "haiku")
# "120b" / "675b" / "31b" — an explicit parameter count in the model name.
_PARAM_RE = re.compile(r"(?:^|[^a-z\d])(\d{1,3}(?:\.\d+)?)b(?![a-z0-9])", re.I)
# Error text that means "this seat costs money we do not have".
_PAID_HINTS = ("insufficient", "credit", "billing", "payment", "purchase",
               "subscription", "top up", "top-up", "upgrade", "requires a paid",
               "not entitled", "no active")


def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


# --------------------------------------------------------------------------- #
# Config / state / rules I/O
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
        if isinstance(state, dict) and isinstance(state.get("strikes"), dict):
            state.setdefault("candidate_strikes", {})
            return state
        if isinstance(state, dict) and isinstance(state.get("grace"), dict):
            # pre-engine state: grace counters were all "model went missing"
            return {"strikes": {m: {"n": int(v.get("misses", 0)),
                                    "reason": "missing", "last": v.get("last_missing", "")}
                                for m, v in state["grace"].items()},
                    "candidate_strikes": {}}
        log(f"state file {STATE_PATH} has an unexpected shape — starting fresh")
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"state file {STATE_PATH} unreadable ({type(e).__name__}: {e}) — starting fresh")
    return {"strikes": {}, "candidate_strikes": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


def load_rules() -> list[dict]:
    """Rules from RULES_PATH; written there with defaults on first run. A file
    that exists but cannot be used aborts the cycle — rules drive removals,
    so silently falling back to defaults could strike the wrong seats."""
    if not os.path.exists(RULES_PATH):
        with open(RULES_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_RULES, f, indent=2)
            f.write("\n")
        log(f"no rules file — wrote defaults to {RULES_PATH}")
        raw = json.loads(json.dumps(DEFAULT_RULES))
    else:
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            raise ValueError(f"rules file {RULES_PATH} unreadable "
                             f"({type(e).__name__}: {e}) — cycle aborted") from e
    rules = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"rules file {RULES_PATH} has no rules list — cycle aborted")
    parsed = []
    for r in rules:
        try:
            pattern = re.compile(r["pattern"])
            free = r.get("free", "probe")
            if free not in ("always", "probe"):
                raise ValueError(f"free must be 'always' or 'probe', got {free!r}")
            parsed.append({
                "id": str(r.get("id", pattern.pattern)),
                "pattern": pattern,
                "exclude": set(r.get("exclude") or []),
                "max_params_b": None if r.get("max_params_b") is None else float(r["max_params_b"]),
                "small_keywords": tuple(r.get("small_keywords") or _SMALL_KEYWORDS),
                "allow": re.compile(r["allow"]) if r.get("allow") else None,
                "free": free,
                "probe_existing": bool(r.get("probe_existing", True)),
            })
        except Exception as e:
            raise ValueError(f"bad rule {r.get('id', '?')}: {e} — cycle aborted") from e
    return parsed


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


# --------------------------------------------------------------------------- #
# The free engine — one probe, classified
# --------------------------------------------------------------------------- #
def _post_chat(base: str, key: str, model: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:
            pass
        return e.code, detail
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def classify(status: int, detail: str) -> str:
    """One probe answer -> ok | free | paid | gone | unknown. A 200 maps
    to "ok" here; probe() additionally validates the body before
    believing it — a malformed one downgrades to unknown."""
    if status == 200:
        return "ok"
    if status == 429:
        return "free"                     # throttled, not paid: the free tier
    if status == 404:
        return "gone"
    if status in (401, 402, 403):
        return "paid"
    if status == 400 and any(h in detail.lower() for h in _PAID_HINTS):
        return "paid"
    return "unknown"


def _valid_completion(body: str) -> bool:
    """HTTP 200 alone is not proof of life: the body must be a well-formed
    chat completion — no error object smuggled in (an explicit null counts
    as none), at least one choice. Content may legitimately be empty
    (reasoning models spend their first token on reasoning), so only the
    shape is checked."""
    try:
        data = json.loads(body)
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error") is not None:
        return False
    return isinstance(data.get("choices"), list) and len(data["choices"]) > 0


def probe(base: str, key: str, model: str) -> tuple[str, str]:
    """One-token chat completion; a second try without max_tokens covers
    upstreams that choke on the parameter but would otherwise answer.
    "ok" means a well-formed completion body actually came back — an
    HTTP 200 carrying garbage is downgraded to unknown and retried next
    cycle, so a model is added only once it demonstrably answers."""
    detail = ""
    for body in ({"model": model, "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 1},
                 {"model": model, "messages": [{"role": "user", "content": "ping"}]}):
        status, detail = _post_chat(base, key, model, body)
        if status == 200:
            if _valid_completion(detail):
                return "ok", ""
            continue                      # 200 but garbage — try the other body
        verdict = classify(status, detail)
        if verdict != "unknown":
            return verdict, detail
    return "unknown", detail[:200]


# --------------------------------------------------------------------------- #
# The size engine — what the name says about the model
# --------------------------------------------------------------------------- #
def size_class(model_id: str, rule: dict) -> str:
    """small | big | unknown. Unknown is never added; existing seats are not
    size-filtered (they were a human choice)."""
    if rule["max_params_b"] is None:
        return "small"                    # size filter disabled for this rule
    name = model_id.rsplit("/", 1)[-1].lower()
    m = _PARAM_RE.search(name)
    if m:
        return "small" if float(m.group(1)) <= rule["max_params_b"] else "big"
    # Whole words only: "mini" must catch qwen-mini, not minimax (230B).
    if any(re.search(rf"\b{re.escape(k)}\b", name) for k in rule["small_keywords"]):
        return "small"
    return "unknown"


def owner_rule(model_id: str, rules: list[dict]) -> dict | None:
    """The first rule claiming this id, or None if it stays hand-written
    (no rule matches, or the matching rule excludes it)."""
    for rule in rules:
        if rule["pattern"].match(model_id):
            return None if model_id in rule["exclude"] else rule
    return None


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
# Strikes — consecutive cycles of evidence that a seat does not belong
# --------------------------------------------------------------------------- #
def strike(state: dict, model: str, reason: str) -> bool:
    """Record one strike; True when the seat has struck out (remove it)."""
    entry = state["strikes"].setdefault(model, {"n": 0, "reason": reason, "last": ""})
    entry["n"] += 1
    entry["reason"] = reason
    entry["last"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if entry["n"] >= REMOVE_AFTER_MISSES:
        state["strikes"].pop(model, None)
        return True
    log(f"{model} strike {entry['n']}/{REMOVE_AFTER_MISSES} ({reason})")
    return False


def candidate_strike(state: dict, model: str, reason: str) -> None:
    """Record one stable rejection of a candidate (paid/gone only — unknown
    verdicts are transient and never count). After CANDIDATE_STRIKES in a
    row the model goes on cooldown: no probes for CANDIDATE_RETRY_HOURS,
    then it is retried once and re-cooled if still bad."""
    cache = state["candidate_strikes"]
    entry = cache.get(model)
    if entry is None or entry.get("reason") != reason:
        entry = {"n": 0, "reason": reason, "until": 0}
        cache[model] = entry
    entry["n"] += 1
    if entry["n"] >= CANDIDATE_STRIKES:
        entry["until"] = time.time() + CANDIDATE_RETRY_HOURS * 3600
        log(f"  candidate {model}: {entry['n']}x {reason} — on cooldown "
            f"for {CANDIDATE_RETRY_HOURS:g}h")
    else:
        log(f"  candidate {model} strike {entry['n']}/{CANDIDATE_STRIKES} ({reason})")


# --------------------------------------------------------------------------- #
# One sync cycle
# --------------------------------------------------------------------------- #
def sync_cycle() -> None:
    cfg = load_config()
    base, key = litellm_endpoint(cfg)
    live = list_models(base, key)
    rules = load_rules()

    members = cfg.get("members")
    if not isinstance(members, list):
        log(f"{CONFIG_PATH} has no members list — nothing to sync")
        return

    state = load_state()
    # Split the roster: hand-written members (untouched) vs rule-owned seats.
    hand: list = []
    owned: dict[str, tuple[dict, dict]] = {}
    orig_owned: set[str] = set()
    for entry in members:
        rule = owner_rule(str(entry.get("model", "")), rules) if isinstance(entry, dict) else None
        if rule is None:
            hand.append(entry)
        else:
            owned[str(entry["model"])] = (rule, entry)
            orig_owned.add(str(entry["model"]))

    struck_now: set[str] = set()

    # Vanished models: every owned seat litellm no longer lists.
    for model in sorted(owned):
        if model not in live:
            if strike(state, model, "missing"):
                log(f"struck out: {model} (gone from litellm)")
                del owned[model]
            struck_now.add(model)

    # Size applies to owned seats too: the engine owns these models, and a
    # seat that no longer meets its rule's idea of "small" is on its way out.
    # Grace-damped like every other strike, so an engine bug cannot wipe the
    # roster in one cycle.
    for model in sorted(owned):
        if model in struck_now:           # one strike reason per cycle
            continue
        rule = owned[model][0]
        if rule["max_params_b"] is None or size_class(model, rule) == "small":
            continue
        if strike(state, model, "oversize"):
            log(f"struck out: {model} (no longer small per rule {rule['id']})")
            del owned[model]
        struck_now.add(model)

    # Re-probe existing seats of every rule (opt-out per rule) — this is
    # how a model that turned paid (or died) gets struck out while it
    # still lists. Paid leaves the same cycle: the roster must never
    # hold a model that charges.
    if PROBE_ENABLED:
        for rule in rules:
            if not rule["probe_existing"]:
                continue
            targets = [m for m, (r, _) in owned.items() if r["id"] == rule["id"]]
            verdicts = {m: probe(base, key, m)[0] for m in targets}
            paid = [m for m, v in verdicts.items() if v == "paid"]
            if len(paid) >= 2 and len(paid) == len(verdicts):
                log(f"rule {rule['id']}: every seat probed paid — treating as a "
                    f"gateway-wide failure, not {len(paid)} dead models")
                verdicts = {m: "unknown" for m in verdicts}
            for m, v in verdicts.items():
                if v in ("ok", "free"):
                    # A healthy probe clears a missing/paid strike — the seat
                    # recovered — but never an oversize one: no probe makes a
                    # big model small.
                    entry = state["strikes"].get(m)
                    if entry is None or entry.get("reason") != "oversize":
                        state["strikes"].pop(m, None)
                elif v == "paid":
                    # A paid verdict is stable evidence (401/402/403 + billing
                    # text): the seat leaves the roster the same cycle — the
                    # roster must never hold a paid model. It re-enters as a
                    # candidate the cycle it stops charging.
                    log(f"struck out: {m} (paid on probe — removed immediately)")
                    state["strikes"].pop(m, None)
                    del owned[m]
                    struck_now.add(m)
                elif v == "gone":
                    if strike(state, m, v):
                        log(f"struck out: {m} (gone on probe)")
                        del owned[m]
                    struck_now.add(m)
            ok_n = sum(1 for v in verdicts.values() if v in ("ok", "free"))
            if targets:
                log(f"rule {rule['id']}: {ok_n}/{len(targets)} owned seats healthy")

    # Candidates: live models a rule claims, not seated yet. Size first
    # (free), then the allow filter, then the probe (costly, bounded).
    added: list[str] = []
    for model in sorted(live):
        if model in owned:
            continue
        rule = owner_rule(model, rules)
        if rule is None:
            continue
        if size_class(model, rule) != "small":
            continue
        if rule["allow"] and not rule["allow"].search(model.rsplit("/", 1)[-1]):
            continue
        if not PROBE_ENABLED:
            log(f"candidate {model} skipped: probing disabled")
            continue
        # Candidate strikes: a model that keeps failing with a stable
        # verdict stops burning probe quota for CANDIDATE_RETRY_HOURS.
        entry = state["candidate_strikes"].get(model)
        if entry and time.time() < entry.get("until", 0):
            log(f"  candidate {model} on cooldown ({entry['reason']} x{entry['n']}) — skip")
            continue
        verdict, detail = probe(base, key, model)
        # Admission is strict and uniform: the model must actually answer
        # with a valid completion. A 429 ("free") is an errored probe —
        # never admitted and never struck; the next cycle retries it, so
        # a throttled model joins the roster as soon as it answers.
        if verdict != "ok":
            log(f"  probe {model} rejected ({verdict}: {detail})")
            if verdict in ("paid", "gone"):
                candidate_strike(state, model, verdict)
            continue
        state["candidate_strikes"].pop(model, None)
        owned[model] = (rule, {"id": model, "provider": "litellm", "model": model})
        added.append(model)

    # Candidate-strike entries for models litellm no longer lists are dead
    # weight — the model cannot be probed anyway and would start fresh.
    state["candidate_strikes"] = {m: e for m, e in state["candidate_strikes"].items()
                                  if m in live}
    if added:
        log("added: " + ", ".join(added))
    save_state(state)

    removed = sorted(orig_owned - set(owned))
    if not added and not removed:
        log(f"roster in sync ({len(hand)} hand-written + {len(owned)} rule-owned "
            f"seats) — no changes")
        return
    if removed:
        log("removed: " + ", ".join(removed))
    cfg["members"] = hand + [owned[m][1] for m in sorted(owned)]
    write_config(cfg)
    log(f"config rewritten: {len(hand)} hand-written + {len(owned)} rule-owned "
        f"seats (backup: {CONFIG_PATH}.bak)")
    if RESTART_GATEWAY:
        restart_gateway()
    else:
        log("RESTART_GATEWAY=false — restart the gateway manually to apply")


def main() -> None:
    log(f"model-council-sync started (interval={SYNC_INTERVAL}s, probe={PROBE_ENABLED}, "
        f"remove_after={REMOVE_AFTER_MISSES} strikes, "
        f"candidates={CANDIDATE_STRIKES}x/{CANDIDATE_RETRY_HOURS:g}h, restart={RESTART_GATEWAY})")
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
