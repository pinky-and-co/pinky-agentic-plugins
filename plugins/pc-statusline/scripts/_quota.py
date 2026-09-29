"""Non-Anthropic quota — shared by the status line (reads) and the plugin's hooks (writes).

The harness only sends `rate_limits` for a claude.ai subscription. On another provider (1 session =
1 provider, picked by the host of `ANTHROPIC_BASE_URL`) a hook fetches that provider's own quota
into a local cache, and the status line rebuilds the same `{five_hour, seven_day: {used_percentage,
resets_at}}` shape from it — so the rate-limit projection and the pc-kanban state file stay
provider-agnostic. Adding a provider means one `Provider` entry in `PROVIDERS` (how to fetch, how
to parse); nothing else changes.

Only the active provider is ever fetched, and the token only ever goes to the registry's own https
host — never to whatever `ANTHROPIC_BASE_URL` happens to say. The render path never touches the
network: it reads the cache a hook wrote.

Not an entry point: no `main()`, imported by `statusline.py` and by `hooks/quota_refresh.py`.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

_HTTP_TIMEOUT_S = 8.0


# --- cache — one file per provider and user; the quota belongs to the account, not to a session ---


def cache_path(provider: str) -> str:
    return os.path.join(
        tempfile.gettempdir(), f"pc-statusline-quota-{provider}-{os.getuid()}.json"
    )


def read_cache(provider: str) -> dict | None:
    """The cache as `{"fetched_at": epoch_s, "payload": <raw provider response>}`, or `None` when
    it is missing, unreadable or not that shape — every failure mode is the same answer."""
    try:
        with open(cache_path(provider), encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict) or not isinstance(cached.get("payload"), dict):
        return None
    return cached


def write_cache(provider: str, payload: dict, now: float) -> None:
    """Atomic (temp file + rename in the same directory: a reader never sees a half-written file),
    user-private (`mkstemp` creates it 0600). Errors are swallowed — a hook must never fail."""
    directory = os.path.dirname(cache_path(provider))
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pc-statusline-quota-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"fetched_at": now, "payload": payload}, handle)
            os.replace(tmp, cache_path(provider))
        except OSError:
            os.unlink(tmp)
    except OSError:
        pass


# --- Z.ai ------------------------------------------------------------------------------------

# Above this an epoch is in milliseconds (10^11 s is the year 5138; 10^11 ms is 1973).
_EPOCH_MS_FLOOR = 1e11
# The two windows a status line shows, and how far ahead each one's reset can possibly be (the
# same lengths as `statusline.py`'s own table). Z.ai's classification below is inferred from its
# payload, so a "5-hour" window resetting further out than that is a mislabel, not data.
_WINDOW_S = {"five_hour": 5 * 3600, "seven_day": 7 * 24 * 3600}
_WINDOW_TOLERANCE_S = 300  # clock skew + how the endpoint rounds a reset
# Internal, undocumented plan-usage endpoint (the one Z.ai's own tooling calls) — it can change
# without notice, which is why every failure below degrades to "no data" rather than an error.
_ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"


def zai_reset_epoch(value: object) -> float | None:
    """Epoch seconds for a Z.ai reset marker, or `None` when it can't be read.

    The quota endpoint's `nextResetTime` is an epoch in milliseconds (zone-free), above
    `_EPOCH_MS_FLOOR`; below it the value is taken as already in seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return value / 1000 if value >= _EPOCH_MS_FLOOR else float(value)
    return None


def _zai_limits(payload: object) -> list[object] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    limits = data.get("limits") if isinstance(data, dict) else None
    return limits if isinstance(limits, list) else None


def _zai_window_pair(limits: list[object]) -> list[dict[str, object]] | None:
    """The plan's two quota windows, whichever family it reports them in: `TOKENS_LIMIT` on the
    standard plan, `CREDIT_LIMIT` on the lite one (both then carry the 5-hour and the weekly
    window). The tokens pair wins; a credit pair is only trusted when no token window is present —
    any other mix is a plan shape this doesn't know, so it stays silent rather than guess."""
    tokens = [
        e for e in limits if isinstance(e, dict) and e.get("type") == "TOKENS_LIMIT"
    ]
    credits = [
        e for e in limits if isinstance(e, dict) and e.get("type") == "CREDIT_LIMIT"
    ]
    if len(tokens) == 2:
        return tokens
    if not tokens and len(credits) == 2:
        return credits
    return None


def zai_fetch(token: str) -> dict | None:  # pragma: allowlist secret
    """One plain GET to the plan-usage endpoint; the raw response, or `None` on any failure.

    A 200 is only trusted when it has the expected shape, and an empty `limits` list is a
    transient snapshot (the API answers it mid-window-swap), not data — either would replace a
    good cache with nothing."""
    request = urllib.request.Request(
        _ZAI_QUOTA_URL,
        headers={
            "Authorization": token,
            "Accept-Language": "en-US,en",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(  # nosec B310 — hardcoded https:// URL, no file:/ or custom scheme reachable
            request, timeout=_HTTP_TIMEOUT_S
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    limits = _zai_limits(payload)
    return payload if limits else None


def zai_rate_limits(payload: object, now: float | None = None) -> dict | None:
    """The plan's 5-hour and weekly windows as a harness-shaped `rate_limits`, or `None` (silent)
    on anything unusable: no `data.limits` list, or a plan shape `_zai_window_pair` doesn't know.

    Labelling follows Z.ai's own tooling: by `number` (5 for the 5-hour window, another value —
    7 or 1 depending on the plan — for the weekly one) when both are present and distinct, since
    near the weekly reset the reset-time order would swap the two; else by reset order, the
    earliest being the 5-hour window. A window is dropped, not shown, when its reset is already
    past (the cache is only refreshed on hook events, so after a rollover it still holds the old
    window and would read as 100% of a limit that no longer applies) or further out than its label
    allows (see `_WINDOW_S`)."""
    limits = _zai_limits(payload)
    pair = _zai_window_pair(limits) if limits is not None else None
    if pair is None:
        return None
    pair.sort(key=lambda entry: zai_reset_epoch(entry.get("nextResetTime")) or 0)
    first, second = pair[0].get("number"), pair[1].get("number")
    if first is not None and second is not None and first != second and second == 5:
        pair.reverse()
    if now is None:
        now = time.time()
    rate_limits = {}
    for key, entry in zip(("five_hour", "seven_day"), pair):
        used = entry.get("percentage")
        resets_at = zai_reset_epoch(entry.get("nextResetTime"))
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        if resets_at is None or resets_at <= now:
            continue
        if resets_at - now > _WINDOW_S[key] + _WINDOW_TOLERANCE_S:
            continue
        rate_limits[key] = {"used_percentage": used, "resets_at": resets_at}
    return rate_limits or None


# --- registry — keyed on the host of `ANTHROPIC_BASE_URL` ------------------------------------


@dataclass(frozen=True)
class Provider:
    name: str
    # token -> raw provider response, `None` on any failure
    fetch: Callable[[str], dict | None]
    # raw provider response, now -> harness-shaped `rate_limits`, `None` when unusable
    parse: Callable[[dict, float], dict | None]


PROVIDERS: dict[str, Provider] = {
    "api.z.ai": Provider("zai", zai_fetch, zai_rate_limits),
}


def active_provider() -> Provider | None:
    """The registered provider this session talks to, or `None` for Anthropic (no base URL, or its
    own host) and for any host the registry doesn't know."""
    try:
        host = urllib.parse.urlparse(os.environ.get("ANTHROPIC_BASE_URL", "")).hostname
    except ValueError:
        return None
    return PROVIDERS.get(host) if host else None


def provider_rate_limits(now: float | None = None) -> dict | None:
    """The active provider's `rate_limits` from its cache — what `statusline.main()` injects when
    the harness sent none. `None` (silent) for Anthropic, no cache, or anything unusable."""
    try:
        provider = active_provider()
        if provider is None:
            return None
        cached = read_cache(provider.name)
        if cached is None:
            return None
        return provider.parse(cached["payload"], time.time() if now is None else now)
    except Exception:
        # a status line has nothing useful to say about a quota it can't read
        return None


def refresh(min_age_s: float, now: float | None = None) -> str:
    """Refresh the active provider's cache unless it is younger than `min_age_s` (0 forces it).

    Returns why it did or didn't fetch — `no-provider`, `fresh`, `no-token`, `failed`, `updated` —
    for the caller's log and for tests; it never raises. A failed fetch leaves the cache alone."""
    provider = active_provider()
    if provider is None:
        return "no-provider"
    if now is None:
        now = time.time()
    cached = read_cache(provider.name)
    fetched_at = cached.get("fetched_at") if cached else None
    if isinstance(fetched_at, (int, float)) and 0 <= now - fetched_at < min_age_s:
        return "fresh"
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        return "no-token"
    try:
        payload = provider.fetch(token)
    except (
        Exception
    ):  # e.g. http.client.IncompleteRead is not an OSError; a hook must not raise
        payload = None
    if payload is None:
        return "failed"
    write_cache(provider.name, payload, now)
    return "updated"
