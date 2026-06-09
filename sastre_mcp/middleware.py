"""HTTP hardening: bearer auth, body size, optional rate limit, optional CORS."""

import hmac
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_networks(values: Iterable[str]) -> list[_IpNetwork]:
    """Parse trusted-proxy entries (bare IPs or CIDRs) into networks.

    A bare address such as ``10.0.0.1`` becomes a host network (``/32`` or
    ``/128``). Invalid entries are rejected at config-load time, so anything
    that slips through here is logged and skipped rather than crashing a
    request.
    """
    networks: list[_IpNetwork] = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            logger.warning(f"ignoring invalid trusted_proxy entry={value}")
    return networks


def _ip_in_networks(ip_str: str, networks: Sequence[_IpNetwork]) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def _client_host(request: Request) -> str:
    if request.client:
        return request.client.host
    return "unknown"


def _client_ip(request: Request, trusted_proxies: Sequence[_IpNetwork]) -> str:
    """Resolve the client IP used for rate-limiting buckets.

    Without configured trusted proxies the direct peer address is used. When
    trusted proxies are configured and the direct peer is one of them, the
    ``X-Forwarded-For`` chain is consulted: walking right-to-left (closest proxy
    first), the first address that is *not* a trusted proxy is treated as the
    real client. ``X-Forwarded-For`` is only honored for requests that actually
    arrive from a trusted proxy, since the header is trivially spoofable
    otherwise.
    """
    peer = _client_host(request)
    if not trusted_proxies or peer == "unknown":
        return peer
    if not _ip_in_networks(peer, trusted_proxies):
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    for candidate in reversed(chain):
        if not _ip_in_networks(candidate, trusted_proxies):
            return candidate
    # Entire chain is trusted proxies; fall back to the claimed origin.
    return chain[0] if chain else peer


class _BodyTooLarge(Exception):
    """Raised when the streamed request body exceeds the configured cap."""


class MaxBodySizeMiddleware:
    """Enforce a hard cap on request body size.

    Implemented as pure ASGI middleware so the limit is enforced on the actual
    bytes streamed in, not just the advertised ``Content-Length``. A chunked or
    otherwise ``Content-Length``-less request that exceeds the cap is rejected
    while the body is being consumed, closing the header-spoofing bypass. It
    also forwards the response stream untouched, preserving SSE/streaming.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        cl = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                cl = value
                break
        if cl is not None:
            try:
                declared = int(cl)
            except ValueError:
                await self._reject(scope, send, 400, "invalid_content_length")
                return
            if declared > self._max_body_bytes:
                await self._reject(scope, send, 413, "payload_too_large")
                return

        received = 0

        async def capped_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body_bytes:
                    raise _BodyTooLarge
            return message

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, capped_receive, tracking_send)
        except _BodyTooLarge:
            if response_started:
                # Headers already flushed downstream; cannot send a clean 413.
                raise
            logger.warning(f"payload_too_large host={_scope_client_host(scope)}")
            await self._reject(scope, send, 413, "payload_too_large")

    async def _reject(self, scope: Scope, send: Send, status_code: int, error: str) -> None:
        response = JSONResponse({"error": error}, status_code=status_code)
        await response(scope, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.disconnect"}


def _scope_client_host(scope: Scope) -> str:
    client = scope.get("client")
    if client:
        return str(client[0])
    return "unknown"


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require Authorization: Bearer <token> when expected_token is set."""

    def __init__(self, app: Callable[..., Awaitable[None]], expected_token: str) -> None:
        super().__init__(app)
        self._expected_token = expected_token.encode("utf-8")

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        auth = request.headers.get("authorization") or ""
        prefix = "Bearer "
        if not auth.startswith(prefix):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        token = auth[len(prefix):].strip().encode("utf-8")
        if not token or not hmac.compare_digest(token, self._expected_token):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple fixed-window rate limit per client IP.

    Limitation: counters live in process memory, so the limit is enforced
    independently by each worker process. With ``N`` workers the effective
    global limit is roughly ``N * max_requests`` per window, and a client may be
    pinned to different workers across requests. For accurate limiting across a
    multi-worker or multi-instance deployment, terminate rate limiting at the
    reverse proxy / API gateway or use a shared backend (e.g. Redis).

    Empty/quiet IP buckets are pruned periodically so memory does not grow
    unboundedly with the number of distinct client IPs seen over time.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        window_secs: int,
        max_requests: int,
        trusted_proxies: Iterable[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._window_secs = window_secs
        self._max_requests = max_requests
        self._trusted_proxies = _parse_networks(trusted_proxies or [])
        self._hits: dict[str, list[float]] = {}
        self._last_prune = 0.0

    def _prune_stale(self, window_start: float) -> None:
        """Drop buckets whose most recent hit has fallen outside the window."""
        stale = [host for host, hits in self._hits.items() if not hits or hits[-1] <= window_start]
        for host in stale:
            del self._hits[host]

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        now = time.monotonic()
        window_start = now - self._window_secs
        if now - self._last_prune >= self._window_secs:
            self._prune_stale(window_start)
            self._last_prune = now
        host = _client_ip(request, self._trusted_proxies)
        bucket = self._hits.get(host)
        if bucket is None:
            bucket = []
            self._hits[host] = bucket
        bucket[:] = [t for t in bucket if t > window_start]
        if len(bucket) >= self._max_requests:
            logger.warning(f"rate_limit_exceeded host={host}")
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        bucket.append(now)
        return await call_next(request)
