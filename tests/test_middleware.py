"""HTTP middleware behavior."""

import asyncio
import contextlib

import pytest
import sastre_mcp.middleware as middleware_mod
from sastre_mcp.config import default_test_config, set_active_config
from sastre_mcp.middleware import (
    BearerTokenMiddleware,
    MaxBodySizeMiddleware,
    RateLimitMiddleware,
    _client_ip,
    _parse_networks,
)
from sastre_mcp.server import build_http_app
from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient


def _http_client(app, cfg) -> TestClient:
    """Match Host header to MCP v2 DNS rebinding allowlist for the configured bind address."""
    return TestClient(app, base_url=f"http://{cfg.mcp.host}:{cfg.mcp.port}")


def _mcp_config(*, limits: dict | None = None, **mcp_overrides):
    from sastre_mcp.config import AppConfig

    return AppConfig.model_validate(
        {
            "sdwan_managers": [
                {
                    "name": "primary",
                    "address": "127.0.0.1",
                    "user": "test",
                    "password": "testpass",
                }
            ],
            "mcp": {"host": "127.0.0.1", "disable_rate_limit": True} | mcp_overrides,
            "limits": limits or {},
        }
    )


def test_bearer_required_when_configured() -> None:
    cfg = default_test_config(bearer_token="test-secret-token")
    set_active_config(cfg)
    app = build_http_app(cfg)
    client = _http_client(app, cfg)
    r = client.post("/mcp", json={})
    assert r.status_code == 401


def test_bearer_accepts_valid_token() -> None:
    cfg = default_test_config(bearer_token="test-secret-token")
    set_active_config(cfg)
    app = build_http_app(cfg)
    with _http_client(app, cfg) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1},
            headers={"Authorization": "Bearer test-secret-token"},
        )
    assert r.status_code != 401


def test_payload_too_large() -> None:
    cfg = default_test_config()
    set_active_config(cfg)
    app = build_http_app(cfg)
    client = _http_client(app, cfg)
    big = b"x" * (3 * 1024 * 1024)
    r = client.post(
        "/mcp",
        content=big,
        headers={"content-length": str(len(big))},
    )
    assert r.status_code == 413


def test_body_limit_above_transport_default_is_honored() -> None:
    """A max_body_bytes above the transport's own 4 MiB default must not be capped by it."""
    cfg = _mcp_config(limits={"max_body_bytes": 6 * 1024 * 1024})
    set_active_config(cfg)
    with _http_client(build_http_app(cfg), cfg) as client:
        r = client.post(
            "/mcp",
            content=b"x" * (5 * 1024 * 1024),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    assert r.status_code != 413


def test_payload_too_large_chunked_bypass() -> None:
    """A body that exceeds the cap without a Content-Length is still rejected."""
    max_body_bytes = 1024
    app = MaxBodySizeMiddleware(_unreachable_app, max_body_bytes=max_body_bytes)

    async def run() -> dict:
        chunks = [
            {"type": "http.request", "body": b"x" * 800, "more_body": True},
            {"type": "http.request", "body": b"x" * 800, "more_body": False},
        ]
        sent: list[dict] = []

        async def receive():
            return chunks.pop(0)

        async def send(message):
            sent.append(message)

        # No content-length header -> header check cannot catch this.
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"transfer-encoding", b"chunked")],
            "client": ("127.0.0.1", 12345),
        }
        await app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        return start

    start = asyncio.run(run())
    assert start["status"] == 413


async def _unreachable_app(scope, receive, send) -> None:
    """Drain the request body, mimicking an app that reads the full payload."""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return
        if not message.get("more_body"):
            return


# --- SSE / streaming compatibility ----------------------------------------
#
# MCP streamable HTTP returns sse_starlette.EventSourceResponse (text/event-stream).
# These tests guard against a regression where the hardening middleware would
# buffer the SSE body or fail to propagate client disconnects to the generator.
# Both behaviors are required for streaming to work behind our middleware stack.

_SSE_TOKEN = "sse-stream-token"
_TICK_INTERVAL = 0.1


def _build_sse_app(generator) -> Starlette:
    """Wrap an SSE endpoint in the full project middleware stack."""

    async def endpoint(_request):
        return EventSourceResponse(generator())

    app = Starlette(routes=[Route("/sse", endpoint)])
    # add_middleware wraps outermost-last, mirroring build_http_app ordering.
    app.add_middleware(BearerTokenMiddleware, expected_token=_SSE_TOKEN)
    app.add_middleware(RateLimitMiddleware, window_secs=60, max_requests=1000)
    app.add_middleware(MaxBodySizeMiddleware, max_body_bytes=2 * 1024 * 1024)
    return app


def _sse_scope() -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/sse",
        "raw_path": b"/sse",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [
            (b"accept", b"text/event-stream"),
            (b"authorization", f"Bearer {_SSE_TOKEN}".encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }


def test_sse_streams_incrementally_through_middleware_stack() -> None:
    """Events must arrive spread over time, not buffered until the generator ends."""

    async def gen():
        for i in range(3):
            yield {"data": f"event-{i}"}
            await asyncio.sleep(_TICK_INTERVAL)

    app = _build_sse_app(gen)

    async def run() -> list[float]:
        loop = asyncio.get_event_loop()
        start = loop.time()
        body_times: list[float] = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(2)
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                body_times.append(loop.time() - start)

        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(app(_sse_scope(), receive, send), timeout=2)
        return body_times

    body_times = asyncio.run(run())

    assert len(body_times) >= 3, body_times
    # If buffered, all chunks land at roughly the same (final) instant.
    # Streaming means later events arrive at least one tick interval later.
    assert body_times[-1] - body_times[0] >= _TICK_INTERVAL


def test_sse_disconnect_propagates_through_middleware_stack() -> None:
    """The SSE generator must stop once the client disconnects (no leak)."""
    ticks = {"n": 0}

    async def gen():
        while True:
            ticks["n"] += 1
            yield {"data": f"tick-{ticks['n']}"}
            await asyncio.sleep(_TICK_INTERVAL)

    app = _build_sse_app(gen)

    async def run() -> tuple[int, int]:
        phase = {"v": "request"}

        async def receive():
            if phase["v"] == "request":
                phase["v"] = "open"
                return {"type": "http.request", "body": b"", "more_body": False}
            if phase["v"] == "disconnect":
                return {"type": "http.disconnect"}
            await asyncio.sleep(0.02)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            pass

        task = asyncio.create_task(app(_sse_scope(), receive, send))
        await asyncio.sleep(0.35)
        at_disconnect = ticks["n"]
        phase["v"] = "disconnect"
        await asyncio.sleep(0.6)
        after = ticks["n"]
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        return at_disconnect, after

    at_disconnect, after = asyncio.run(run())
    # Allow at most one extra in-flight tick after the disconnect signal.
    assert after - at_disconnect <= 1, (at_disconnect, after)


# --- Rate limiting --------------------------------------------------------


def _make_request(host: str, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": raw_headers,
        "client": (host, 12345),
    }
    return Request(scope)


async def _ok(_request: Request) -> Response:
    return Response("ok")


def _dispatch(mw: RateLimitMiddleware, request: Request) -> Response:
    return asyncio.run(mw.dispatch(request, _ok))


def test_rate_limit_blocks_after_max() -> None:
    mw = RateLimitMiddleware(_unreachable_app, window_secs=60, max_requests=2)
    assert _dispatch(mw, _make_request("10.0.0.5")).status_code == 200
    assert _dispatch(mw, _make_request("10.0.0.5")).status_code == 200
    assert _dispatch(mw, _make_request("10.0.0.5")).status_code == 429


def test_rate_limit_window_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(middleware_mod.time, "monotonic", lambda: clock["t"])
    mw = RateLimitMiddleware(_unreachable_app, window_secs=60, max_requests=1)
    assert _dispatch(mw, _make_request("10.0.0.5")).status_code == 200
    assert _dispatch(mw, _make_request("10.0.0.5")).status_code == 429
    # Advance past the window; the old hit ages out and the client is allowed.
    clock["t"] += 61
    assert _dispatch(mw, _make_request("10.0.0.5")).status_code == 200


def test_rate_limit_prunes_quiet_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(middleware_mod.time, "monotonic", lambda: clock["t"])
    mw = RateLimitMiddleware(_unreachable_app, window_secs=60, max_requests=10)
    _dispatch(mw, _make_request("10.0.0.5"))
    assert "10.0.0.5" in mw._hits
    # A later request from a different IP triggers the periodic sweep, which
    # evicts the now-stale bucket for the first IP.
    clock["t"] += 61
    _dispatch(mw, _make_request("10.0.0.6"))
    assert "10.0.0.5" not in mw._hits
    assert "10.0.0.6" in mw._hits


def test_client_ip_ignores_xff_without_trusted_proxies() -> None:
    request = _make_request("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})
    assert _client_ip(request, []) == "203.0.113.9"


def test_client_ip_ignores_xff_from_untrusted_peer() -> None:
    trusted = _parse_networks(["10.0.0.0/8"])
    request = _make_request("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})
    assert _client_ip(request, trusted) == "203.0.113.9"


def test_client_ip_uses_xff_from_trusted_proxy() -> None:
    trusted = _parse_networks(["10.0.0.1"])
    request = _make_request("10.0.0.1", {"x-forwarded-for": "1.2.3.4, 10.0.0.1"})
    assert _client_ip(request, trusted) == "1.2.3.4"


def test_client_ip_skips_chained_trusted_proxies() -> None:
    trusted = _parse_networks(["10.0.0.0/8"])
    request = _make_request("10.0.0.1", {"x-forwarded-for": "1.2.3.4, 10.9.9.9, 10.0.0.1"})
    assert _client_ip(request, trusted) == "1.2.3.4"


def test_rate_limit_keys_on_forwarded_client() -> None:
    mw = RateLimitMiddleware(
        _unreachable_app,
        window_secs=60,
        max_requests=1,
        trusted_proxies=["10.0.0.1"],
    )
    proxy_headers = {"x-forwarded-for": "1.2.3.4"}
    other_headers = {"x-forwarded-for": "5.6.7.8"}
    # Distinct forwarded clients get distinct buckets even via the same proxy.
    assert _dispatch(mw, _make_request("10.0.0.1", proxy_headers)).status_code == 200
    assert _dispatch(mw, _make_request("10.0.0.1", other_headers)).status_code == 200
    # The first forwarded client is now over its own limit.
    assert _dispatch(mw, _make_request("10.0.0.1", proxy_headers)).status_code == 429


def test_invalid_trusted_proxy_rejected_at_config_load() -> None:
    from pydantic import ValidationError
    from sastre_mcp.config import LimitsConfig

    with pytest.raises(ValidationError, match="not a valid IP"):
        LimitsConfig(rate_limit_trusted_proxies=["not-an-ip"])


# --- CORS ------------------------------------------------------------------


def _cors_config(origins: list[str]):
    return _mcp_config(cors_origins=origins)


def test_cors_preflight_allows_configured_origin() -> None:
    cfg = _cors_config(["https://app.example.com"])
    set_active_config(cfg)
    app = build_http_app(cfg)
    client = _http_client(app, cfg)
    r = client.options(
        "/mcp",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://app.example.com"


def test_cors_preflight_rejects_unconfigured_origin() -> None:
    cfg = _cors_config(["https://app.example.com"])
    set_active_config(cfg)
    app = build_http_app(cfg)
    client = _http_client(app, cfg)
    r = client.options(
        "/mcp",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    # Starlette responds 400 to a disallowed preflight and omits the allow header.
    assert "access-control-allow-origin" not in r.headers


def test_no_cors_headers_when_origins_unset() -> None:
    cfg = default_test_config()
    set_active_config(cfg)
    app = build_http_app(cfg)
    # No CORS middleware is installed, so the preflight reaches the MCP app;
    # the context manager runs the lifespan so its task group is initialized.
    with _http_client(app, cfg) as client:
        r = client.options(
            "/mcp",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert "access-control-allow-origin" not in r.headers


# --- Host / Origin validation (DNS rebinding) -------------------------------

# 421 is returned for a rejected Host, 403 for a rejected Origin.
_REBIND_REJECTED = (403, 421)


def _initialize(client: TestClient, base_url: str, **headers: str):
    """POST an initialize request, which is enough to exercise Host/Origin validation."""
    return client.post(
        f"{base_url}/mcp",
        json={"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1},
        headers={"Accept": "application/json, text/event-stream"} | headers,
    )


def test_configured_cors_origin_passes_origin_check() -> None:
    """A cross-origin POST from an allowed origin must survive the transport check, not just CORS."""
    cfg = _cors_config(["https://app.example.com"])
    set_active_config(cfg)
    with _http_client(build_http_app(cfg), cfg) as client:
        r = _initialize(client, "", Origin="https://app.example.com")
    assert r.status_code not in _REBIND_REJECTED


def test_unconfigured_origin_rejected() -> None:
    cfg = _cors_config(["https://app.example.com"])
    set_active_config(cfg)
    with _http_client(build_http_app(cfg), cfg) as client:
        r = _initialize(client, "", Origin="https://evil.example.com")
    assert r.status_code == 403


def test_loopback_origin_allowed_without_cors_config() -> None:
    cfg = default_test_config()
    set_active_config(cfg)
    with _http_client(build_http_app(cfg), cfg) as client:
        r = _initialize(client, "", Origin="http://localhost:3000")
    assert r.status_code not in _REBIND_REJECTED


def test_foreign_host_rejected_on_wildcard_bind() -> None:
    cfg = _mcp_config(host="0.0.0.0", bearer_token="tok", allowed_hosts=["mcp.example.com"])
    set_active_config(cfg)
    with TestClient(build_http_app(cfg)) as client:
        r = _initialize(client, "http://evil.example.com", Authorization="Bearer tok")
    assert r.status_code == 421


def test_configured_host_accepted_on_wildcard_bind() -> None:
    cfg = _mcp_config(host="0.0.0.0", bearer_token="tok", allowed_hosts=["mcp.example.com"])
    set_active_config(cfg)
    with TestClient(build_http_app(cfg)) as client:
        r = _initialize(client, "http://mcp.example.com", Authorization="Bearer tok")
    assert r.status_code not in _REBIND_REJECTED


def test_allowed_hosts_wildcard_disables_check() -> None:
    cfg = _mcp_config(host="0.0.0.0", bearer_token="tok", allowed_hosts=["*"])
    set_active_config(cfg)
    with TestClient(build_http_app(cfg)) as client:
        r = _initialize(client, "http://evil.example.com", Authorization="Bearer tok")
    assert r.status_code not in _REBIND_REJECTED


def test_ipv6_bind_host_pattern_is_bracketed() -> None:
    from sastre_mcp.server import _transport_security

    settings = _transport_security(_mcp_config(host="2001:db8::1").mcp)
    assert settings.allowed_hosts == ["[2001:db8::1]:*"]


def test_hostname_bind_pattern_is_not_bracketed() -> None:
    from sastre_mcp.server import _transport_security

    settings = _transport_security(_mcp_config(host="mcp.example.com").mcp)
    assert settings.allowed_hosts == ["mcp.example.com:*"]


def test_wildcard_cors_origin_warns(caplog: pytest.LogCaptureFixture) -> None:
    from sastre_mcp.server import _transport_security

    with caplog.at_level("WARNING"):
        _transport_security(_mcp_config(cors_origins=["*"]).mcp)
    assert "cors_origins contains '*'" in caplog.text


def test_specific_address_bind_accepts_its_own_address() -> None:
    cfg = _mcp_config(host="10.0.0.5", port=9000)
    set_active_config(cfg)
    app = build_http_app(cfg)
    with TestClient(app) as client:
        assert _initialize(client, "http://10.0.0.5:9000").status_code not in _REBIND_REJECTED
        assert _initialize(client, "http://evil.example.com").status_code == 421
