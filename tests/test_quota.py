"""Non-Anthropic quota (`scripts/_quota.py`) — tests.

Same oracle strategy as test_statusline.py: each pure helper gets a direct equality oracle. The
network is never touched — `urlopen` is faked, and the cache lives under `tmp_path`.
"""

import json
import os
import stat
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "plugins" / "pc-statusline" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _quota as q  # noqa: E402

# A 2026-ish "now": with a toy epoch (1_000_000 s) the millisecond values would fall under the
# ms/s threshold and be read as seconds — real epochs are nowhere near it.
NOW = 1_790_000_000


def _window(percentage, reset_s, number=None):
    """One `TOKENS_LIMIT` entry — `nextResetTime` is epoch milliseconds, like the real endpoint."""
    window = {
        "type": "TOKENS_LIMIT",
        "percentage": percentage,
        "nextResetTime": reset_s * 1000,
    }
    if number is not None:
        window["number"] = number
    return window


def _payload(*limits):
    return {"data": {"limits": list(limits)}}


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect the cache (a tempdir file) into `tmp_path`."""
    monkeypatch.setattr(q.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def zai_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-123")


# --- zai_reset_epoch ----------------------------------------------------------------------------


def test_zai_reset_epoch_reads_the_endpoint_epoch_in_milliseconds():
    assert q.zai_reset_epoch(1_790_624_911_000) == 1_790_624_911.0


def test_zai_reset_epoch_passes_epoch_seconds_through():
    assert q.zai_reset_epoch(1_790_624_911) == 1_790_624_911.0


@pytest.mark.parametrize(
    "value", [None, True, 0, -5, "soon", "2026-09-29T03:48:31", "", [], {}]
)
def test_zai_reset_epoch_none_on_unreadable_values(value):
    assert q.zai_reset_epoch(value) is None


# --- zai_rate_limits ----------------------------------------------------------------------------


def test_zai_rate_limits_oracle_maps_the_two_token_windows():
    payload = _payload(
        _window(42, NOW + 3600, number=5),
        _window(77, NOW + 300_000, number=7),
        {"type": "CREDIT_LIMIT", "percentage": 9, "nextResetTime": 1},
    )
    assert q.zai_rate_limits(payload, NOW) == {
        "five_hour": {"used_percentage": 42, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 77, "resets_at": NOW + 300_000},
    }


def test_zai_rate_limits_labels_by_number_when_reset_order_would_swap():
    """Near the weekly reset the 7-day window resets *before* the 5-hour one — the `number` field
    (5 vs 7) is what keeps the labels right."""
    payload = _payload(
        _window(10, NOW + 3600, number=7), _window(90, NOW + 3 * 3600, number=5)
    )
    assert q.zai_rate_limits(payload, NOW) == {
        "five_hour": {"used_percentage": 90, "resets_at": NOW + 3 * 3600},
        "seven_day": {"used_percentage": 10, "resets_at": NOW + 3600},
    }


def test_zai_rate_limits_falls_back_to_reset_order_without_numbers():
    payload = _payload(_window(77, NOW + 300_000), _window(42, NOW + 3600))
    assert q.zai_rate_limits(payload, NOW) == {
        "five_hour": {"used_percentage": 42, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 77, "resets_at": NOW + 300_000},
    }


# The payload of a real `lite` plan (fetched 2026-09-29): both windows are `CREDIT_LIMIT` — unit 3
# / number 5 is the 5-hour window, unit 6 / number 1 the weekly one — and `percentage` is the
# floored 0-100 share of `usage` (87 / 2000 = 4.35% -> 4).
_LITE_FETCHED_AT = 1790666825.085985
_LITE_PAYLOAD = {
    "code": 200,
    "msg": "Operation successful",
    "data": {
        "limits": [
            {
                "type": "CREDIT_LIMIT",
                "unit": 3,
                "number": 5,
                "usage": 2000,
                "currentValue": 87,
                "remaining": 1912,
                "percentage": 4,
                "nextResetTime": 1790684642670,
            },
            {
                "type": "CREDIT_LIMIT",
                "unit": 6,
                "number": 1,
                "usage": 10000,
                "currentValue": 3425,
                "remaining": 6574,
                "percentage": 34,
                "nextResetTime": 1791206784969,
            },
        ],
        "level": "lite",
    },
    "success": True,
}


def _credit(percentage, reset_s, number):
    return {
        "type": "CREDIT_LIMIT",
        "percentage": percentage,
        "number": number,
        "nextResetTime": reset_s * 1000,
    }


def test_zai_rate_limits_reads_a_real_lite_plan_payload():
    assert q.zai_rate_limits(_LITE_PAYLOAD, _LITE_FETCHED_AT) == {
        "five_hour": {"used_percentage": 4, "resets_at": 1790684642670 / 1000},
        "seven_day": {"used_percentage": 34, "resets_at": 1791206784969 / 1000},
    }


def test_zai_rate_limits_labels_a_credit_pair_by_number_when_reset_order_would_swap():
    payload = _payload(
        _credit(10, NOW + 3600, number=1), _credit(90, NOW + 3 * 3600, number=5)
    )
    assert q.zai_rate_limits(payload, NOW) == {
        "five_hour": {"used_percentage": 90, "resets_at": NOW + 3 * 3600},
        "seven_day": {"used_percentage": 10, "resets_at": NOW + 3600},
    }


def test_zai_rate_limits_prefers_the_token_pair_over_a_credit_pair():
    payload = _payload(
        _credit(1, NOW + 3600, number=5),
        _credit(2, NOW + 300_000, number=1),
        _window(42, NOW + 3600, number=5),
        _window(77, NOW + 300_000, number=7),
    )
    assert q.zai_rate_limits(payload, NOW) == {
        "five_hour": {"used_percentage": 42, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 77, "resets_at": NOW + 300_000},
    }


def test_zai_rate_limits_drops_a_five_hour_window_resetting_beyond_five_hours():
    """The labelling is inferred: a "5-hour" window that resets days away is a mislabel, and showing
    it would feed a wrong pace to the projection."""
    payload = _payload(_window(90, NOW + 3 * 86_400), _window(30, NOW + 5 * 86_400))
    assert q.zai_rate_limits(payload, NOW) == {
        "seven_day": {"used_percentage": 30, "resets_at": NOW + 5 * 86_400},
    }


def test_zai_rate_limits_drops_a_weekly_window_resetting_beyond_seven_days():
    payload = _payload(_window(10, NOW + 3600), _window(30, NOW + 8 * 86_400))
    assert q.zai_rate_limits(payload, NOW) == {
        "five_hour": {"used_percentage": 10, "resets_at": NOW + 3600},
    }


def test_zai_rate_limits_keeps_a_window_at_its_length_within_the_tolerance():
    payload = _payload(
        _window(10, NOW + 5 * 3600 + 299), _window(30, NOW + 7 * 86_400 + 299)
    )
    assert set(q.zai_rate_limits(payload, NOW)) == {"five_hour", "seven_day"}


def test_zai_rate_limits_drops_a_window_whose_reset_is_past():
    """The cache is only refreshed on hook events: after a rollover it still holds the old 5h
    window — kept, it would read as 100% of a limit that no longer applies."""
    payload = _payload(
        _window(100, NOW - 60, number=5), _window(30, NOW + 300_000, number=7)
    )
    assert q.zai_rate_limits(payload, NOW) == {
        "seven_day": {"used_percentage": 30, "resets_at": NOW + 300_000},
    }


def test_zai_rate_limits_none_when_every_window_is_past():
    payload = _payload(_window(100, NOW - 60), _window(99, NOW - 30))
    assert q.zai_rate_limits(payload, NOW) is None


@pytest.mark.parametrize(
    "payload",
    [
        _payload(),
        _payload({"type": "CREDIT_LIMIT", "percentage": 13, "nextResetTime": 99}),
        _payload(
            _window(10, NOW + 3600)
        ),  # a single token window is not the standard plan
        _payload(*[_window(10, NOW + 3600)] * 3),
        _payload(*[_credit(10, NOW + 3600, 5)] * 3),
        # one token window next to a credit pair: a mix this doesn't know, so it stays silent
        _payload(
            _window(10, NOW + 3600),
            _credit(1, NOW + 3600, 5),
            _credit(2, NOW + 9000, 1),
        ),
        _payload("not-a-dict", None, 3),
        _payload(_window("oops", NOW + 3600), _window(None, NOW + 7200)),
        {"data": {"limits": "x"}},
        {"data": 1},
        {},
        [],
        None,
        "text",
    ],
)
def test_zai_rate_limits_none_on_a_plan_shape_it_cannot_read(payload):
    assert q.zai_rate_limits(payload, NOW) is None


# --- cache --------------------------------------------------------------------------------------


def test_cache_path_is_per_provider_and_per_user(tmp_cache):
    assert q.cache_path("zai") == q.cache_path("zai")
    assert q.cache_path("zai") != q.cache_path("mimo")
    assert q.cache_path("zai") == str(
        tmp_cache / f"pc-statusline-quota-zai-{os.getuid()}.json"
    )


def test_cache_round_trips_the_payload_with_its_fetch_time(tmp_cache):
    payload = _payload(_window(42, NOW + 3600, number=5))
    q.write_cache("zai", payload, NOW)
    assert q.read_cache("zai") == {"fetched_at": NOW, "payload": payload}


def test_write_cache_is_user_private_and_leaves_no_temp_file(tmp_cache):
    q.write_cache("zai", _payload(), NOW)
    mode = stat.S_IMODE(os.stat(q.cache_path("zai")).st_mode)
    assert mode == 0o600
    assert [p.name for p in tmp_cache.iterdir()] == [Path(q.cache_path("zai")).name]


def test_write_cache_replaces_the_previous_snapshot(tmp_cache):
    q.write_cache("zai", {"n": 1}, NOW)
    q.write_cache("zai", {"n": 2}, NOW + 5)
    assert q.read_cache("zai") == {"fetched_at": NOW + 5, "payload": {"n": 2}}


def test_write_cache_swallows_an_unwritable_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(q.tempfile, "gettempdir", lambda: str(tmp_path / "missing"))
    q.write_cache("zai", {"n": 1}, NOW)  # must not raise


@pytest.mark.parametrize(
    "content",
    [None, "", "not json {{{", "[]", '{"payload": 3}', '{"fetched_at": 1}'],
)
def test_read_cache_none_on_a_missing_or_malformed_file(tmp_cache, content):
    if content is not None:
        Path(q.cache_path("zai")).write_text(content, encoding="utf-8")
    assert q.read_cache("zai") is None


# --- registry / dispatch ------------------------------------------------------------------------


def test_active_provider_dispatches_on_the_base_url_host(monkeypatch):
    for base_url in (
        "https://api.z.ai/api/anthropic",
        "https://API.Z.AI:443/other/path",
    ):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
        assert q.active_provider() is q.PROVIDERS["api.z.ai"]


@pytest.mark.parametrize(
    "base_url",
    [
        None,
        "",
        "https://api.anthropic.com",
        "https://elsewhere.example",
        "::::",
        "http://[bad",
    ],
)
def test_active_provider_none_for_anthropic_or_an_unknown_host(monkeypatch, base_url):
    if base_url is None:
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
    assert q.active_provider() is None


def test_provider_rate_limits_reads_the_active_providers_cache(tmp_cache, zai_env):
    q.write_cache(
        "zai",
        _payload(
            _window(42, NOW + 3600, number=5), _window(77, NOW + 300_000, number=7)
        ),
        NOW,
    )
    assert q.provider_rate_limits(now=NOW) == {
        "five_hour": {"used_percentage": 42, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 77, "resets_at": NOW + 300_000},
    }


def test_provider_rate_limits_none_on_anthropic_even_with_a_provider_cache(
    tmp_cache, monkeypatch
):
    q.write_cache(
        "zai", _payload(_window(100, NOW + 3600), _window(100, NOW + 9000)), NOW
    )
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert q.provider_rate_limits(now=NOW) is None


def test_provider_rate_limits_none_without_a_cache(tmp_cache, zai_env):
    assert q.provider_rate_limits(now=NOW) is None


def test_provider_rate_limits_swallows_a_parser_failure(
    tmp_cache, zai_env, monkeypatch
):
    def _boom(payload, now):
        raise RuntimeError("parser blew up")

    q.write_cache("zai", _payload(), NOW)
    monkeypatch.setitem(
        q.PROVIDERS, "api.z.ai", q.Provider("zai", lambda t: None, _boom)
    )
    assert q.provider_rate_limits(now=NOW) is None


# --- zai_fetch ----------------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(monkeypatch, body=None, error=None):
    """Stand in for `urlopen`, recording the request and the timeout."""
    seen = {}

    def _urlopen(request, timeout=None):
        seen["request"] = request
        seen["timeout"] = timeout
        if error is not None:
            raise error
        return _FakeResponse(body)

    monkeypatch.setattr(q.urllib.request, "urlopen", _urlopen)
    return seen


def test_zai_fetch_sends_the_token_only_to_the_registry_https_host(monkeypatch):
    payload = _payload(_window(42, NOW + 3600, number=5))
    seen = _fake_urlopen(monkeypatch, body=json.dumps(payload).encode())
    assert q.zai_fetch("tok-123") == payload
    request = seen["request"]
    assert request.full_url == "https://api.z.ai/api/monitor/usage/quota/limit"
    assert (
        request.get_header("Authorization") == "tok-123"
    )  # raw token, as Z.ai's tooling sends it
    assert seen["timeout"] == q._HTTP_TIMEOUT_S


@pytest.mark.parametrize(
    "body",
    [
        b"<html>splash page</html>",
        b"{}",
        b"[]",
        b'{"data": {"limits": []}}',  # transient empty snapshot — must not replace a good cache
        b'{"data": {"limits": "x"}}',
        b"\xff\xfe",
    ],
)
def test_zai_fetch_none_on_a_200_that_is_not_usable_data(monkeypatch, body):
    _fake_urlopen(monkeypatch, body=body)
    assert q.zai_fetch("tok") is None


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("down"), TimeoutError(), OSError("reset"), ValueError("x")],
)
def test_zai_fetch_none_on_a_network_failure(monkeypatch, error):
    _fake_urlopen(monkeypatch, error=error)
    assert q.zai_fetch("tok") is None


# --- refresh ------------------------------------------------------------------------------------


def _stub_provider(monkeypatch, fetch):
    monkeypatch.setitem(
        q.PROVIDERS, "api.z.ai", q.Provider("zai", fetch, q.zai_rate_limits)
    )


def test_refresh_no_provider_on_anthropic(tmp_cache, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    calls = []
    _stub_provider(monkeypatch, lambda t: calls.append(t))
    assert q.refresh(0, now=NOW) == "no-provider"
    assert calls == [] and not Path(q.cache_path("zai")).exists()


def test_refresh_updates_a_missing_cache(tmp_cache, zai_env, monkeypatch):
    payload = _payload(_window(42, NOW + 3600, number=5))
    tokens = []
    _stub_provider(monkeypatch, lambda t: tokens.append(t) or payload)
    assert q.refresh(10, now=NOW) == "updated"
    assert tokens == ["tok-123"]
    assert q.read_cache("zai") == {"fetched_at": NOW, "payload": payload}


def test_refresh_skips_a_cache_younger_than_the_throttle(
    tmp_cache, zai_env, monkeypatch
):
    q.write_cache("zai", {"old": 1}, NOW)
    calls = []
    _stub_provider(monkeypatch, lambda t: calls.append(t))
    assert q.refresh(120, now=NOW + 119) == "fresh"
    assert calls == []


def test_refresh_refetches_once_the_throttle_has_elapsed(
    tmp_cache, zai_env, monkeypatch
):
    q.write_cache("zai", {"old": 1}, NOW)
    _stub_provider(monkeypatch, lambda t: {"new": 1})
    assert q.refresh(120, now=NOW + 120) == "updated"
    assert q.read_cache("zai") == {"fetched_at": NOW + 120, "payload": {"new": 1}}


def test_refresh_zero_always_forces_a_fetch(tmp_cache, zai_env, monkeypatch):
    """A session start (`min_age_s == 0`) fetches even when the cache was written this second."""
    q.write_cache("zai", {"old": 1}, NOW)
    _stub_provider(monkeypatch, lambda t: {"new": 1})
    assert q.refresh(0, now=NOW) == "updated"


def test_refresh_treats_a_cache_from_the_future_as_stale(
    tmp_cache, zai_env, monkeypatch
):
    """Clock skew: a `fetched_at` ahead of now would otherwise look fresh until the clock caught
    up — and block every refresh in between."""
    q.write_cache("zai", {"old": 1}, NOW + 10_000)
    _stub_provider(monkeypatch, lambda t: {"new": 1})
    assert q.refresh(120, now=NOW) == "updated"


def test_refresh_no_token_never_calls_the_provider(tmp_cache, zai_env, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    calls = []
    _stub_provider(monkeypatch, lambda t: calls.append(t))
    assert q.refresh(0, now=NOW) == "no-token"
    assert calls == []


def test_refresh_failed_fetch_leaves_the_previous_cache_alone(
    tmp_cache, zai_env, monkeypatch
):
    q.write_cache("zai", {"old": 1}, NOW)
    _stub_provider(monkeypatch, lambda t: None)
    assert q.refresh(0, now=NOW + 500) == "failed"
    assert q.read_cache("zai") == {"fetched_at": NOW, "payload": {"old": 1}}


def test_refresh_never_raises_when_the_provider_does(tmp_cache, zai_env, monkeypatch):
    def _boom(token):
        raise RuntimeError("IncompleteRead-ish")

    _stub_provider(monkeypatch, _boom)
    assert q.refresh(0, now=NOW) == "failed"
