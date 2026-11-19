import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from fpl_server.config import ConfigError, get_settings, is_loopback_host, validate_for_startup
from fpl_server.security import BearerAuthMiddleware, WebHardeningMiddleware

TOKEN = "t" * 40


def _app(middleware, **kw):
    async def ok(request):
        return PlainTextResponse("secret-data")

    app = Starlette(routes=[Route("/", ok)])
    app.add_middleware(middleware, **kw)
    return app


def test_bearer_required_and_constant_time_path():
    client = TestClient(_app(BearerAuthMiddleware, token=TOKEN))
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert client.get("/", headers={"Authorization": f"Bearer {TOKEN}"}).text == "secret-data"
    assert "secret-data" not in client.get("/").text


def test_bearer_middleware_refuses_empty_token():
    with pytest.raises(ValueError):
        BearerAuthMiddleware(lambda *a: None, token="")


def test_host_allowlist_and_security_headers():
    app = _app(WebHardeningMiddleware, allowed_hosts=["127.0.0.1:8020"])
    good = TestClient(app, base_url="http://127.0.0.1:8020").get("/")
    assert good.status_code == 200
    assert good.headers["x-frame-options"] == "DENY"
    assert good.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in good.headers["content-security-policy"]
    evil = TestClient(app, base_url="http://evil.example").get("/")
    assert evil.status_code == 421  # DNS-rebinding style host is refused


@pytest.fixture
def clean_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("FPL_"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_defaults_are_safe(clean_env):
    s = get_settings()
    assert s.allow_writes is False and s.approval_pin is None
    assert s.web_host == "127.0.0.1" and s.mcp_host == "127.0.0.1" and s.transport == "stdio"
    warnings = validate_for_startup(s)
    assert any("DRY RUN" in w for w in warnings)


def test_http_transport_requires_strong_bearer(clean_env):
    clean_env.setenv("FPL_MCP_TRANSPORT", "streamable-http")
    with pytest.raises(ConfigError, match="BEARER"):
        validate_for_startup(get_settings())
    clean_env.setenv("FPL_MCP_BEARER_TOKEN", "short")
    with pytest.raises(ConfigError, match="BEARER"):
        validate_for_startup(get_settings())
    clean_env.setenv("FPL_MCP_BEARER_TOKEN", TOKEN)
    validate_for_startup(get_settings())


def test_refuses_public_bind_unless_opted_in(clean_env):
    clean_env.setenv("FPL_WEB_HOST", "0.0.0.0")  # noqa: S104 - testing the refusal
    with pytest.raises(ConfigError, match="loopback"):
        validate_for_startup(get_settings())
    clean_env.setenv("FPL_ALLOW_NONLOOPBACK_BIND", "true")
    validate_for_startup(get_settings())


def test_writes_require_pin_and_pin_must_be_long(clean_env):
    clean_env.setenv("FPL_ALLOW_WRITES", "true")
    with pytest.raises(ConfigError, match="requires FPL_APPROVAL_PIN"):
        validate_for_startup(get_settings())
    clean_env.setenv("FPL_APPROVAL_PIN", "short")
    with pytest.raises(ConfigError, match="at least"):
        validate_for_startup(get_settings())
    clean_env.setenv("FPL_APPROVAL_PIN", "long-enough-pin")
    validate_for_startup(get_settings())


def test_bad_values_fail_closed(clean_env):
    clean_env.setenv("FPL_ALLOW_WRITES", "maybe")
    with pytest.raises(ConfigError):
        get_settings()
    clean_env.setenv("FPL_ALLOW_WRITES", "false")
    clean_env.setenv("FPL_AUTH_PORT", "99999")
    with pytest.raises(ConfigError):
        get_settings()


def test_loopback_detection():
    assert is_loopback_host("127.0.0.1") and is_loopback_host("::1") and is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0") and not is_loopback_host("10.0.0.5")  # noqa: S104
