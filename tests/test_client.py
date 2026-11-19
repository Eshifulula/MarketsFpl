"""HTTP client behaviour that the safety story relies on: writes are never retried, errors are
sanitised, and server-clock freshness is captured from FPL's own headers."""

import asyncio

import httpx
import pytest

from fpl_server.client import FPLAPIError, FPLAuthExpired, FPLClient


def _client(handler, token=None):
    client = FPLClient()
    if token:
        client.set_api_token(token)
    client.session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._session_loop = asyncio.get_running_loop()
    return client


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr("fpl_server.client.asyncio.sleep", instant)


async def test_post_is_never_retried_even_on_5xx():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(503, text="unavailable")

    with pytest.raises(FPLAPIError) as caught:
        await _client(handler, "tok")._request("POST", "transfers/", {"x": 1})
    assert calls == ["POST"] and caught.value.status == 503


async def test_get_is_retried_on_5xx_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    assert await _client(handler)._request("GET", "bootstrap-static/") == {"ok": True}
    assert len(calls) == 3


async def test_get_gives_up_after_three_attempts():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(FPLAPIError):
        await _client(handler)._request("GET", "fixtures/")
    assert len(calls) == 3


async def test_accepted_post_with_empty_body_is_success_not_failure():
    client = _client(lambda request: httpx.Response(200, content=b""), "tok")
    result = await client._request("POST", "my-team/1/", {"picks": []})
    assert result["status_code"] == 200


async def test_rejected_credentials_surface_as_auth_expired_without_leaking_the_token():
    client = _client(lambda request: httpx.Response(401, text="nope"), "super-secret-token")
    with pytest.raises(FPLAuthExpired) as caught:
        await client._request("GET", "me/")
    assert "super-secret-token" not in str(caught.value)


async def test_error_text_is_truncated():
    client = _client(lambda request: httpx.Response(400, text="x" * 5000), "tok")
    with pytest.raises(FPLAPIError) as caught:
        await client._request("POST", "transfers/", {})
    assert len(caught.value.body) <= 300


async def test_bootstrap_clock_comes_from_fpl_headers():
    def handler(request):
        return httpx.Response(
            200,
            json={"events": []},
            headers={"date": "Sat, 21 Nov 2026 10:45:00 GMT", "age": "42"},
        )

    client = _client(handler)
    await client.get_bootstrap_data()
    assert client.bootstrap_clock.age_seconds == 42
    assert client.bootstrap_clock.server_time.year == 2026
    assert client.bootstrap_clock.server_time.utcoffset().total_seconds() == 0


async def test_garbled_freshness_headers_fail_closed():
    def handler(request):
        return httpx.Response(200, json={}, headers={"date": "not a date", "age": "soon"})

    client = _client(handler)
    await client.get_bootstrap_data()
    assert client.bootstrap_clock.server_time is None
    assert client.bootstrap_clock.age_seconds >= 10**6
