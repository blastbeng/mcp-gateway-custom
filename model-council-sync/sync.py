#!/usr/bin/env python3
"""Keep the sync-owned seats of the model council in step with litellm.

The council config (model-council-data/config.json) holds exactly one kind of
member: rule-owned seats, managed here. Nothing in the roster is static —
every model in it was taken from litellm and probe-verified before being
seated, and a member no rule claims is a leftover that this sync drops.

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
           200 but malformed/error body-> unknown (502 / timeout / garbage)
           429          -> free      (rate-limited: still the free tier;
                                      healthy for a seated model, but an
                                      errored probe — not enough to add)
           402/401/403  -> paid      (needs credits/entitlement)
           404          -> gone      (provider stopped serving it)
           anything else-> unknown

       "unknown" is read differently for candidates and seats: a CANDIDATE
       that answers unknown is simply retried next cycle (no penalty —
       flaky upstreams must not be written off); a SEATED model must keep
       demonstrating it answers, so consecutive unknown probes accumulate
       probe-fail strikes and REMOVE_AFTER_MISSES of them seat the model
       out. A rule whose every seat comes back unknown in one cycle is a
       gateway-wide failure (circuit breaker, like the all-paid one): no
       strikes that cycle. A struck-out model returns as a candidate the
       cycle it answers ok again.

       a rule with free:"always" (inference4free) probes candidates for
       aliveness only; a rule with free:"probe" probes for free vs paid.
       Every rule also re-probes its existing seats every cycle
       (probe_existing, on by default — a rule may opt out to spare the
       provider's daily free quota): a seat that answers paid leaves the
       roster the same cycle — the roster must never hold a paid model —
       and comes back as a candidate the cycle it answers ok again; a
       seat whose probe keeps coming back unknown is struck out after
       REMOVE_AFTER_MISSES consecutive failed probes.
       Admission itself is the same for every rule: only an "ok" probe
       ever adds a candidate;
  3. strikes seats that vanish from litellm or probe as paid/gone, drops
     members no rule claims (there is no such thing as a static seat),
     and probe-fail-strikes seats whose own probe keeps coming back
     unknown (502 / timeout / garbage): REMOVE_AFTER_MISSES consecutive
     failed probes seat a model out — it re-enters as a candidate the
     cycle it answers again. A paid probe removes the seat the same
     cycle (a paid model must never sit in the roster); gone/missing
     take REMOVE_AFTER_MISSES consecutive strikes first, so a transient
     provider hiccup does not churn the roster (and restart the gateway);
     a rule whose seats ALL come back paid — or ALL come back unknown —
     in one cycle is treated as a gateway-wide failure, not as N dead
     models (circuit breaker);
     candidates get the same economy: a model rejected repeatedly with
     the same verdict (paid/gone/unknown) accumulates candidate strikes
     and after CANDIDATE_STRIKES of them stops being probed for
     CANDIDATE_RETRY_HOURS — then it is retried once and re-cooled if
     still bad. A throttled probe (429) is never admitted but never
     counted: the model is retried every cycle and joins the roster the
     moment it answers. Nothing is permanent: every rejection is undone
     the cycle the model answers again;
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
from concurrent.futures import ThreadPoolExecutor

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.json")
STATE_PATH = os.environ.get("STATE_PATH", "/config/.sync-state.json")
RULES_PATH = os.environ.get("RULES_PATH", "/config/sync-rules.json")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "3600"))
RUN_ONCE = os.environ.get("RUN_ONCE", "").lower() in ("1", "true", "yes", "on")
PROBE_ENABLED = os.environ.get("PROBE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "25"))
PROBE_WORKERS = max(1, int(os.environ.get("PROBE_WORKERS", "12")))
REMOVE_AFTER_MISSES = max(1, int(os.environ.get("REMOVE_AFTER_MISSES", "2")))
CANDIDATE_STRIKES = max(1, int(os.environ.get("CANDIDATE_STRIKES", "2")))
CANDIDATE_RETRY_HOURS = float(os.environ.get("CANDIDATE_RETRY_HOURS", "24"))
RESTART_GATEWAY = os.environ.get("RESTART_GATEWAY", "true").lower() in ("1", "true", "yes", "on")
GATEWAY_CONTAINER = os.environ.get("GATEWAY_CONTAINER", "mcp-gateway")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

# Default engine rules — written to RULES_PATH on first run, editable there.
# pattern  : which model ids the rule owns (first matching rule wins);
#            a roster member no rule claims is dropped as a static leftover
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
#            not linger, and one that keeps failing its probe outright is
#            struck out after REMOVE_AFTER_MISSES cycles); set false only
#            when a re-probe would burn the provider's daily free quota
#            and a lingering broken seat is acceptable — candidates are
#            probed once regardless
DEFAULT_RULES = {
    "rules": [
        # every inference4free model litellm exposes — the probe decides
        # which of them actually answer
        {"id": "inference4free", "pattern": r"^inference4free/",
         "max_params_b": None, "free": "always"},
        {"id": "groq", "pattern": r"^groq/", "max_params_b": 32, "free": "probe"},
        {"id": "ollama-cloud", "pattern": r"^ollama-cloud/", "max_params_b": 32, "free": "probe"},
        {"id": "gemini", "pattern": r"^gemini/",
         "max_params_b": 32, "free": "probe",
         "allow": r"^gemini(-[\d.]+)?-flash(-lite)?$"
                  r"|^gemini-flash(-lite)?-latest$"
                  r"|^gemma-\d+-\d+b"},
        # openrouter: only the free models — the ":free" suffix is the
        # cost signal (the probe confirms it), and litellm's plain
        # "openrouter/free" alias counts as free by its name. Seats are
        # re-probed like every rule's: a free seat that starts charging
        # must leave the roster. The hourly 1-token probe costs a sliver
        # of the daily free quota — the price of never seating a paid model.
        {"id": "openrouter", "pattern": r"^openrouter/",
         "max_params_b": None, "free": "probe",
         "allow": r"(?::free$|^free$)"},
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


def probe_many(base: str, key: str, models: list[str]) -> dict[str, tuple[str, str]]:
    """probe() across many models concurrently (PROBE_WORKERS threads) — a
    full-catalog cycle must not serialize minutes of upstream timeouts.
    Results keyed by model id; probe() never raises."""
    if not models:
        return {}
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
        return {m: r for m, r in zip(models, ex.map(lambda m: probe(base, key, m), models))}


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
    """The first rule claiming this id, or None. None means the id can
    never be seated — an unclaimed roster member is a static leftover."""
    for rule in rules:
        if rule["pattern"].match(model_id):
            return rule
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
    """Record one rejection of a candidate (paid/gone/unknown — the same
    verdict repeated is evidence the model does not work). After
    CANDIDATE_STRIKES in a row the model goes on cooldown: no probes for
    CANDIDATE_RETRY_HOURS, then it is retried once and re-cooled if still
    bad. The cycle it answers ok it is seated and the strikes are cleared."""
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
    if not live:
        raise RuntimeError("litellm listed zero models — catalog outage? cycle aborted")
    rules = load_rules()

    members = cfg.get("members")
    if not isinstance(members, list):
        log(f"{CONFIG_PATH} has no members list — nothing to sync")
        return

    state = load_state()
    # Every member must be claimed by a rule — there are no hand-written
    # members. Anything unclaimed is a static leftover and is dropped this
    # cycle: the roster is 100% litellm-sourced and probe-verified.
    owned: dict[str, tuple[dict, dict]] = {}
    pre: set[str] = set()
    static: list[str] = []
    for entry in members:
        model = str(entry.get("model", "")) if isinstance(entry, dict) else ""
        pre.add(model)
        rule = owner_rule(model, rules) if model else None
        if rule is None:
            static.append(model or repr(entry)[:40])
        else:
            owned[model] = (rule, entry)
    if static:
        log("dropped static members (no rule claims them): " + ", ".join(static))

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
    # how a model that turned paid (or died, or stopped answering) gets
    # struck out while it still lists. Paid leaves the same cycle: the
    # roster must never hold a model that charges. A seat whose probe
    # keeps coming back unknown is struck out after REMOVE_AFTER_MISSES
    # consecutive failed probes.
    if PROBE_ENABLED:
        # Phase 1 — probe every rule's seats (concurrently) and defuse a
        # per-rule billing outage: every seat of a rule answering paid is
        # litellm's billing path breaking, not every model starting to
        # charge at once.
        seat_verdicts: dict[str, dict[str, str]] = {}
        for rule in rules:
            if not rule["probe_existing"]:
                continue
            targets = [m for m, (r, _) in owned.items() if r["id"] == rule["id"]]
            verdicts = {m: v for m, (v, _) in probe_many(base, key, targets).items()}
            paid = [m for m, v in verdicts.items() if v == "paid"]
            if len(paid) >= 2 and len(paid) == len(verdicts):
                log(f"rule {rule['id']}: every seat probed paid — treating as a "
                    f"gateway-wide failure, not {len(paid)} dead models")
                verdicts = {m: "unknown" for m in verdicts}
            seat_verdicts[rule["id"]] = verdicts
        # Phase 2 — a probe blackout is only excused when it is GATEWAY-WIDE
        # (every owned seat of every rule failed at once: litellm itself is
        # having a moment). A single family going dark while every other
        # rule answers — e.g. all of inference4free unknown, groq/gemini/
        # openrouter fine — is evidence that family's models are broken:
        # probe-fail strikes proceed and the dead seats leave the roster.
        total_seats = sum(len(v) for v in seat_verdicts.values())
        total_unknown = sum(sum(1 for v in vs.values() if v == "unknown")
                            for vs in seat_verdicts.values())
        gateway_wide = total_seats >= 2 and total_unknown == total_seats
        if gateway_wide:
            log(f"every owned seat probed unknown ({total_seats}) — treating as a "
                f"gateway-wide failure, no probe-fail strikes this cycle")
        for rule in rules:
            verdicts = seat_verdicts.get(rule["id"], {})
            if not verdicts:
                continue
            dead = [] if gateway_wide else [m for m, v in verdicts.items()
                                            if v == "unknown"]
            for m, v in verdicts.items():
                if v in ("ok", "free"):
                    # A healthy probe clears a missing/paid/probe-fail strike
                    # — the seat recovered — but never an oversize one: no
                    # probe makes a big model small.
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
                elif v == "unknown" and m in dead:
                    # The seat no longer answers through litellm. A sitting
                    # model must keep demonstrating it works: consecutive
                    # failed probes accumulate a probe-fail strike, and after
                    # REMOVE_AFTER_MISSES of them the seat leaves the roster
                    # (it re-enters as a candidate the cycle it answers).
                    if strike(state, m, "probe-fail"):
                        log(f"struck out: {m} (probe failed "
                            f"{REMOVE_AFTER_MISSES} cycles in a row)")
                        del owned[m]
                    struck_now.add(m)
            ok_n = sum(1 for v in verdicts.values() if v in ("ok", "free"))
            log(f"rule {rule['id']}: {ok_n}/{len(verdicts)} owned seats healthy")

    # Candidates: live models a rule claims, not seated yet. Size first
    # (free), then the allow filter, then the probe (concurrent, bounded).
    todo: list[str] = []
    cand_rule: dict[str, dict] = {}
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
        # Candidate strikes: a model that keeps failing with the same
        # verdict stops burning probe quota for CANDIDATE_RETRY_HOURS.
        entry = state["candidate_strikes"].get(model)
        if entry and time.time() < entry.get("until", 0):
            log(f"  candidate {model} on cooldown ({entry['reason']} x{entry['n']}) — skip")
            continue
        todo.append(model)
        cand_rule[model] = rule
    added: list[str] = []
    probed = probe_many(base, key, todo)
    for model in todo:
        verdict, detail = probed[model]
        # Admission is strict and uniform: the model must actually answer
        # with a valid completion. An errored probe is never admitted, and
        # a repeated verdict feeds the cooldown above. A 429 ("free") is
        # an errored probe but is never counted — the next cycle retries
        # it, so a throttled model joins the roster as soon as it answers.
        if verdict == "ok":
            state["candidate_strikes"].pop(model, None)
            owned[model] = (cand_rule[model],
                            {"id": model, "provider": "litellm", "model": model})
            added.append(model)
        elif verdict == "free":
            log(f"  probe {model} throttled (429) — not seated, retried next cycle")
        else:
            log(f"  probe {model} rejected ({verdict}: {detail})")
            candidate_strike(state, model, verdict)

    # Candidate-strike entries for models litellm no longer lists are dead
    # weight — the model cannot be probed anyway and would start fresh.
    state["candidate_strikes"] = {m: e for m, e in state["candidate_strikes"].items()
                                  if m in live}
    if added:
        log("added: " + ", ".join(added))
    save_state(state)

    removed = sorted(pre - set(owned))
    if not added and not removed:
        log(f"roster in sync ({len(owned)} seats) — no changes")
        return
    if removed:
        log("removed: " + ", ".join(removed))
    cfg["members"] = [owned[m][1] for m in sorted(owned)]
    write_config(cfg)
    log(f"config rewritten: {len(owned)} seats (backup: {CONFIG_PATH}.bak)")
    if RESTART_GATEWAY:
        restart_gateway()
    else:
        log("RESTART_GATEWAY=false — restart the gateway manually to apply")


def main() -> None:
    log(f"model-council-sync started (interval={SYNC_INTERVAL}s, probe={PROBE_ENABLED}, "
        f"workers={PROBE_WORKERS}, remove_after={REMOVE_AFTER_MISSES} strikes, "
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
