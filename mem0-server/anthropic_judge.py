"""anthropic_judge.py: the Claude API transport behind codex_shim_client.judge.

The stack's LLM work (session extraction, the dream's consolidation and promotion, the
contradiction and supersession judges, the write gate) runs on one of two providers, chosen per
install:

  MEM0_JUDGE_PROVIDER=codex      the Codex CLI on a ChatGPT login (the default: no key, no API spend)
  MEM0_JUDGE_PROVIDER=anthropic  the Claude API on an operator-supplied API key (this module)

Every setting here is read from the process environment first and from ~/.mem0/stack.env second,
so a unit needs no new Environment= line and a hand run on the same box resolves the same values.

Contract: every public call returns a dict and never raises. Failures use the Codex transports'
vocabulary where one exists (`client_timeout`, `unreachable`, `no_key`) and add the API's own:
`rate_limited`, `auth`, `refusal`, `truncated`, `budget_exceeded`, `no_sdk`, `http_<status>`.

Key custody: the key is read from a file at call time and handed to the SDK client as an argument.
It is never put in an environment variable. An ANTHROPIC_API_KEY in the environment of a coding
agent replaces that agent's own login, and every child process inherits the environment.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from job_liveness import read_stack_env

PROVIDER_ENV = "MEM0_JUDGE_PROVIDER"
CREDENTIAL_NAME = "ams-anthropic-key"       # the systemd credential a native authority's units load
KEY_FILE_NAME = "anthropic-key"             # ~/.mem0/anthropic-key (mode 600) on a WSL-hosted brain
DEFAULT_MODEL_SYNTHESIS = "claude-opus-5-5"  # open-ended synthesis and consequential judgment
DEFAULT_MODEL_CLASSIFY = "claude-haiku-5-5"  # bounded extraction, classification and yes/no judging
DEFAULT_MONTHLY_BUDGET_USD = 20.0
# Thinking counts toward max_tokens on every current model, so a cap sized for a one-word verdict
# can be used up before the answer starts. This is a ceiling, not a spend.
MAX_TOKENS = 16000
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# A declined request is re-served by the model the provider recommends for that refusal category,
# inside the same call. Only these families accept the parameter; on the others a refusal is final.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_FAMILIES = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")
# The one host the key and the prompts go to. It is passed to the SDK explicitly: left unset, the
# SDK takes ANTHROPIC_BASE_URL from the environment, and a job started from a coding agent's hook
# inherits that session's environment, so a gateway or proxy named there would receive this key and
# every prompt.
API_BASE_URL = "https://api.anthropic.com"
# One retry, on a rate limit, a server error or a connection failure, and only while this much of
# the caller's time bound is left. A timeout is never retried: the bound is the caller's.
_RETRY_PAUSE_S = 2.0
_RETRY_MIN_REMAINING_S = 10.0
# USD per million tokens (input, output), most specific prefix first. Used only to estimate spend
# for the monthly cap and the spend ledger; the provider's console is the bill. A model no row
# matches is priced at _UNKNOWN_PRICE, the highest here, so it can trip the cap early, never late.
_PRICES = (
    ("claude-haiku-5-5", 0.10, 0.50),
    ("claude-sonnet-5", 2.00, 10.00),
    ("claude-opus-5-5", 4.00, 20.00),
    ("claude-opus-", 5.00, 25.00),
    ("claude-fable-", 10.00, 50.00),
    ("claude-haiku-", 1.00, 5.00),
    ("claude-sonnet-", 3.00, 15.00),
)
_UNKNOWN_PRICE = (10.00, 50.00)
# Above this prompt size the small model bills at five times its short-prompt rate.
_HAIKU_LONG_PROMPT_TOKENS = 100_000
_HAIKU_LONG_PROMPT_FACTOR = 5.0


def _setting(key: str, default: str = "") -> str:
    v = (os.environ.get(key) or "").strip()
    if v:
        return v
    return (read_stack_env().get(key) or "").strip() or default


def provider() -> str:
    """'anthropic' when this install selected the Claude API, else 'codex'."""
    return "anthropic" if _setting(PROVIDER_ENV).lower() == "anthropic" else "codex"


def model_for(job_class: str) -> str:
    """The model of a job class: 'synthesis' (the dream's consolidation, promotion and its gate) or
    'classify' (everything bounded: extraction, gather, the store judge, the yes/no judges)."""
    if job_class == "synthesis":
        return _setting("MEM0_JUDGE_MODEL_SYNTHESIS", DEFAULT_MODEL_SYNTHESIS)
    return _setting("MEM0_JUDGE_MODEL_CLASSIFY", DEFAULT_MODEL_CLASSIFY)


def workspace_id() -> str:
    """Sent as `anthropic-workspace-id` when set. A key that is not scoped to one workspace is
    refused without it; a workspace-scoped key needs none."""
    return _setting("MEM0_ANTHROPIC_WORKSPACE_ID")


def key_path() -> tuple:
    """(path, source) of the first non-empty key file, or ('', 'none'). Order: the credential the
    unit loaded, the file the operator named, the plain file of a WSL-hosted brain."""
    cands = []
    cred_dir = (os.environ.get("CREDENTIALS_DIRECTORY") or "").strip()
    if cred_dir:
        cands.append((os.path.join(cred_dir, CREDENTIAL_NAME), "credential"))
    named = (os.environ.get("MEM0_ANTHROPIC_KEY_FILE") or "").strip()
    if named:
        cands.append((named, "file"))
    cands.append((str(Path.home() / ".mem0" / KEY_FILE_NAME), "file"))
    for path, source in cands:
        try:
            if os.path.getsize(path) > 0:
                return path, source
        except OSError:
            continue
    return "", "none"


def _read_key() -> str:
    path, _ = key_path()
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Spend: one ledger row per API call, one file per month, and a monthly cap read from it.
# The transport writes the row itself because not every caller keeps a usage ledger (the write
# gate and the sweeps do not), and a cap that misses a caller is not a cap.
# ---------------------------------------------------------------------------
def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = _UNKNOWN_PRICE
    for prefix, p_in, p_out in _PRICES:
        if str(model).startswith(prefix):
            rate_in, rate_out = p_in, p_out
            break
    if str(model).startswith("claude-haiku-5-5") and int(input_tokens or 0) > _HAIKU_LONG_PROMPT_TOKENS:
        rate_in, rate_out = rate_in * _HAIKU_LONG_PROMPT_FACTOR, rate_out * _HAIKU_LONG_PROMPT_FACTOR
    return (int(input_tokens or 0) * rate_in + int(output_tokens or 0) * rate_out) / 1_000_000.0


def _warn(message: str) -> None:
    """One line on stderr (the journal, under a unit). Never raises."""
    try:
        print(f"[anthropic_judge] {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 (a warning that cannot be printed must not fail a call)
        pass


def spend_ledger_path(now: Optional[_dt.datetime] = None) -> Path:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return Path.home() / ".mem0" / "maintenance" / f"llm-spend-{now:%Y-%m}.jsonl"


def record_spend(model: str, input_tokens: int, output_tokens: int,
                 now: Optional[_dt.datetime] = None, estimated: bool = False) -> float:
    """Append this call to the month's ledger and return its estimated cost. A ledger that cannot
    be written does not fail the call that was already paid for; the row goes to stderr instead, so
    the spend is still on record. `estimated` marks a row whose token counts did not come from the
    provider (a call that timed out on this side): `"est": true`."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    cost = estimate_cost_usd(model, input_tokens, output_tokens)
    row = {"ts": now.isoformat(), "provider": "anthropic", "model": model,
           "input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0),
           "cost_usd": round(cost, 6)}
    if estimated:
        row["est"] = True
    try:
        path = spend_ledger_path(now)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as e:
        _warn(f"spend ledger row not written ({e}): {json.dumps(row)}")
    return cost


def month_spend_usd(now: Optional[_dt.datetime] = None) -> float:
    """This month's estimated spend on this box. No ledger yet reads as 0; a ledger that exists and
    cannot be read also reads as 0, and says so, because the cap is then not enforced."""
    total = 0.0
    try:
        with open(spend_ledger_path(now), "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    total += float(json.loads(line).get("cost_usd") or 0.0)
                except (ValueError, TypeError, AttributeError):
                    continue
    except FileNotFoundError:
        pass
    except OSError as e:
        _warn(f"spend ledger unreadable, so the monthly cap is not enforced for this call: {e}")
    return total


def budget_usd() -> float:
    """The monthly cap in USD. 0 turns the cap off; a value that does not parse keeps the default,
    because a typo must never remove the cap."""
    raw = _setting("MEM0_JUDGE_MONTHLY_BUDGET_USD")
    if not raw:
        return DEFAULT_MONTHLY_BUDGET_USD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MONTHLY_BUDGET_USD
    return value if value >= 0 else DEFAULT_MONTHLY_BUDGET_USD


def budget_gate(now: Optional[_dt.datetime] = None) -> dict:
    """{allow, spent_usd, budget_usd, reason}: refuse once this month's estimated spend on this box
    has reached the cap. The cap is per box, since each box keeps its own ledger."""
    cap, spent = budget_usd(), month_spend_usd(now)
    out = {"spent_usd": round(spent, 4), "budget_usd": cap}
    if cap <= 0:
        return {**out, "allow": True, "reason": "no monthly cap (MEM0_JUDGE_MONTHLY_BUDGET_USD=0)"}
    if spent >= cap:
        return {**out, "allow": False,
                "reason": f"estimated spend ${spent:.2f} this month has reached the ${cap:.2f} cap "
                          "(MEM0_JUDGE_MONTHLY_BUDGET_USD in ~/.mem0/stack.env)"}
    return {**out, "allow": True, "reason": f"estimated spend ${spent:.2f} of ${cap:.2f} this month"}


def usage_summary(now: Optional[_dt.datetime] = None) -> dict:
    """The `usage` row of /health/maintenance on this provider, in the shape the Codex plan window
    uses there: `used_percent` is this month's estimated spend against the cap (None with no cap)
    and `resets_in_days` counts to the first day of next month (UTC), when a new ledger starts."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    gate = budget_gate(now)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start + _dt.timedelta(days=32)).replace(day=1)
    cap = gate["budget_usd"]
    return {"used_percent": round(100.0 * gate["spent_usd"] / cap, 1) if cap > 0 else None,
            "resets_in_days": round((next_month - now).total_seconds() / 86400.0, 1),
            "probed_at": now.isoformat(), "note": gate["reason"], "provider": "anthropic",
            "spent_usd": gate["spent_usd"], "budget_usd": cap}


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------
_SDK = {"loaded": False, "module": None}


def _sdk():
    """The `anthropic` package, imported on first use so a Codex install never needs it."""
    if not _SDK["loaded"]:
        _SDK["loaded"] = True
        try:
            import anthropic
            _SDK["module"] = anthropic
        except Exception:  # noqa: BLE001 (a broken install must read as no_sdk, not crash a job)
            _SDK["module"] = None
    return _SDK["module"]


def _error_type(exc: Exception) -> str:
    """The SDK's typed errors, most specific first (a timeout is also a connection error there)."""
    sdk = _sdk()
    if sdk is not None:
        if isinstance(exc, sdk.APITimeoutError):
            return "client_timeout"
        if isinstance(exc, sdk.RateLimitError):
            return "rate_limited"
        if isinstance(exc, (sdk.AuthenticationError, sdk.PermissionDeniedError)):
            return "auth"
        if isinstance(exc, sdk.APIStatusError):
            return f"http_{exc.status_code}"
    return "unreachable"


def _fail(error_type: str, error: str, **extra) -> dict:
    return {"ok": False, "error_type": error_type, "error": str(error)[:400], "transport": "anthropic", **extra}


def _client(key: str, timeout_s: float):
    sdk = _sdk()
    if sdk is None:
        return None
    ws = workspace_id()
    # No SDK retries: judge_once retries once itself, inside the caller's time bound. The SDK's own
    # retry also repeats a timed-out call, which doubles the wall clock and can bill twice.
    return sdk.Anthropic(api_key=key, base_url=API_BASE_URL, timeout=float(timeout_s), max_retries=0,
                         default_headers={"anthropic-workspace-id": ws} if ws else None)


def judge_once(prompt: str, effort: str, timeout_s: int, model: str, _client_factory=None) -> dict:
    """One Messages API call. Fail-soft dict, never raises. `_client_factory(key, timeout_s)` is the
    test seam; it returns an object with the SDK client's `messages` and `beta.messages`."""
    key = _read_key()
    if not key:
        return _fail("no_key", "no Claude API key: the unit loaded no ams-anthropic-key credential and "
                               "there is no ~/.mem0/anthropic-key")
    gate = budget_gate()
    if not gate["allow"]:
        return _fail("budget_exceeded", gate["reason"])
    model = model if str(model or "").startswith("claude-") else model_for("classify")
    effort = effort if effort in _EFFORTS else "medium"
    request = {"model": model, "max_tokens": MAX_TOKENS, "output_config": {"effort": effort},
               "messages": [{"role": "user", "content": prompt}]}
    t0 = time.monotonic()
    deadline = t0 + float(timeout_s)
    attempt = 0
    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        if remaining < 1:
            return _fail("client_timeout", f"no answer from the Claude API within {timeout_s}s",
                         duration_ms=int((time.monotonic() - t0) * 1000))
        client = (_client_factory or _client)(key, remaining)
        if client is None:
            return _fail("no_sdk", "the anthropic Python package is not installed in this environment "
                                   "(re-run the installer, or: pip install anthropic)")
        try:
            if model.startswith(_FALLBACK_FAMILIES):
                resp = client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **request)
            else:
                resp = client.messages.create(**request)
            break
        except Exception as e:  # noqa: BLE001 (fail-soft by contract; _error_type keeps the SDK's type)
            error_type = _error_type(e)
            if error_type == "client_timeout":
                # The provider may still finish and bill a call this side gave up on, and no usage
                # came back. The prompt is counted at one token per three characters (a high
                # estimate) and the answer at nothing, so a run of timeouts cannot spend past the
                # cap unseen.
                record_spend(model, len(prompt) // 3 + 1, 0, estimated=True)
            retryable = error_type in ("rate_limited", "unreachable") or error_type.startswith("http_5")
            if retryable and attempt < 2 and deadline - time.monotonic() > _RETRY_MIN_REMAINING_S:
                time.sleep(_RETRY_PAUSE_S)
                continue
            return _fail(error_type, str(e), duration_ms=int((time.monotonic() - t0) * 1000))
    duration_ms = int((time.monotonic() - t0) * 1000)
    usage = getattr(resp, "usage", None)
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    served_by = str(getattr(resp, "model", "") or model)
    cost = record_spend(served_by, tokens_in, tokens_out)
    measured = {"tokens_used": tokens_in + tokens_out, "input_tokens": tokens_in, "output_tokens": tokens_out,
                "cost_usd": round(cost, 6), "model_resolved": served_by, "effort_resolved": effort,
                "duration_ms": duration_ms}
    stop = getattr(resp, "stop_reason", None)
    if stop == "refusal":
        category = getattr(getattr(resp, "stop_details", None), "category", None)
        return _fail("refusal", f"the model declined the request (category: {category or 'unspecified'})", **measured)
    # Blocks are read by type: a response can open with thinking blocks.
    text = "".join(getattr(b, "text", "") or "" for b in (getattr(resp, "content", None) or [])
                   if getattr(b, "type", "") == "text").strip()
    if not text:
        return _fail("truncated" if stop == "max_tokens" else "empty",
                     f"no text in the response (stop_reason: {stop})", **measured)
    return {"ok": True, "response": text, "transport": "anthropic", **measured}


def health() -> dict:
    """Whether this box can judge on the Claude API, from local state only: no network call, so a
    health probe never spends and never waits on the provider. `ok` needs the SDK, a key and room
    under the monthly cap."""
    _, source = key_path()
    gate = budget_gate()
    sdk_ok = _sdk() is not None
    ok = sdk_ok and source != "none" and gate["allow"]
    out = {"ok": ok, "transport": "anthropic", "key_present": source != "none", "key_source": source,
           "sdk": sdk_ok, "workspace_set": bool(workspace_id()),
           "model_synthesis": model_for("synthesis"), "model_classify": model_for("classify"),
           "budget": gate}
    if not ok:
        out["error_type"] = "no_sdk" if not sdk_ok else ("no_key" if source == "none" else "budget_exceeded")
    return out
