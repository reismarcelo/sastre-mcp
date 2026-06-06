"""FastMCP application: `sdwan show` tools over streamable HTTP."""

import asyncio
import logging
from typing import Any, Literal

from cisco_sdwan.tasks.implementation import (
    ShowAlarmsArgs,
    ShowDevicesArgs,
    ShowEventsArgs,
    ShowRealtimeArgs,
    ShowStateArgs,
    ShowStatisticsArgs,
)
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware

from sastre_mcp.config import AppConfig
from sastre_mcp.middleware import (
    BearerTokenMiddleware,
    MaxBodySizeMiddleware,
    RateLimitMiddleware,
)
from sastre_mcp.runner import (
    ShowTaskArgs,
    list_sdwan_managers_info,
    operational_commands_help,
    run_show_args,
)

logger = logging.getLogger(__name__)


def _build[ModelT: BaseModel](model_cls: type[ModelT], /, **fields: Any) -> ModelT:
    """Construct a cisco-sdwan Show*Args model, surfacing a clean error message.

    The Show*Args classes validate their own domain rules (regex/site/command
    syntax and the regex and detail/simple mutual-exclusions). Surface only the
    first error as a single line, which is more useful to an LLM tool caller
    than a full multi-error Pydantic dump.

    ``None`` values are dropped so unset optional fields fall back to their model
    defaults; the SDK's per-field validators (e.g. site-id) run on any value that
    is explicitly supplied and would otherwise reject ``None``.
    """
    provided = {name: value for name, value in fields.items() if value is not None}
    try:
        return model_cls(**provided)
    except ValidationError as exc:
        err = exc.errors()[0]
        msg = err.get("msg", str(exc))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        raise ValueError(msg) from exc


async def _run_show(
    args: ShowTaskArgs, output_format: Literal["text", "json"], manager: str | None
) -> str:
    """Run a blocking show task in a worker thread (the SDK is synchronous)."""
    return await asyncio.to_thread(
        run_show_args, args, output_format=output_format, manager=manager
    )


def create_mcp(config: AppConfig) -> FastMCP:
    mcp = FastMCP(
        "sastre-show",
        instructions=(
            "Tools wrap Cisco Sastre (cisco-sdwan) `sdwan show` for SD-WAN Manager. "
            "SD-WAN Manager credentials come from the server config.yaml file (validated at startup), not from tool arguments. "
            "Multiple managers can be configured, each with a unique name. Use `list_sdwan_managers` to discover them, "
            "then pass the `manager` argument to a show tool to choose one; omit it to use the default."
        ),
        host=config.mcp.host,
        port=config.mcp.port,
        streamable_http_path="/mcp",
        stateless_http=config.mcp.stateless_http,
    )

    @mcp.tool()
    async def show_devices(
        regex: str | None = None,
        not_regex: str | None = None,
        reachable: bool = False,
        site: str | None = None,
        system_ip: list[str] | None = None,
        device_type: str | None = None,
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """List devices from SD-WAN Manager (sdwan show devices)."""
        args = _build(
            ShowDevicesArgs,
            regex=regex,
            not_regex=not_regex,
            reachable=reachable,
            site=site,
            system_ip=system_ip,
            device_type=device_type,
            include=include,
            exclude=exclude,
        )
        return await _run_show(args, output_format, manager)

    @mcp.tool()
    async def show_realtime(
        cmd: list[str],
        regex: str | None = None,
        not_regex: str | None = None,
        reachable: bool = False,
        site: str | None = None,
        system_ip: list[str] | None = None,
        device_type: str | None = None,
        detail: bool = False,
        simple: bool = False,
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """Realtime operational data from devices (sdwan show realtime <cmd>...)."""
        args = _build(
            ShowRealtimeArgs,
            cmd=cmd,
            regex=regex,
            not_regex=not_regex,
            reachable=reachable,
            site=site,
            system_ip=system_ip,
            device_type=device_type,
            detail=detail,
            simple=simple,
            include=include,
            exclude=exclude,
        )
        return await _run_show(args, output_format, manager)

    @mcp.tool()
    async def show_state(
        cmd: list[str],
        regex: str | None = None,
        not_regex: str | None = None,
        reachable: bool = False,
        site: str | None = None,
        system_ip: list[str] | None = None,
        device_type: str | None = None,
        detail: bool = False,
        simple: bool = False,
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """Bulk state operational data (sdwan show state <cmd>...)."""
        args = _build(
            ShowStateArgs,
            cmd=cmd,
            regex=regex,
            not_regex=not_regex,
            reachable=reachable,
            site=site,
            system_ip=system_ip,
            device_type=device_type,
            detail=detail,
            simple=simple,
            include=include,
            exclude=exclude,
        )
        return await _run_show(args, output_format, manager)

    @mcp.tool()
    async def show_statistics(
        cmd: list[str],
        days: int = 0,
        hours: int = 0,
        regex: str | None = None,
        not_regex: str | None = None,
        reachable: bool = False,
        site: str | None = None,
        system_ip: list[str] | None = None,
        device_type: str | None = None,
        detail: bool = False,
        simple: bool = False,
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """Statistics / bulk stats (sdwan show statistics <cmd>...; optional --days / --hours)."""
        args = _build(
            ShowStatisticsArgs,
            cmd=cmd,
            days=days,
            hours=hours,
            regex=regex,
            not_regex=not_regex,
            reachable=reachable,
            site=site,
            system_ip=system_ip,
            device_type=device_type,
            detail=detail,
            simple=simple,
            include=include,
            exclude=exclude,
        )
        return await _run_show(args, output_format, manager)

    @mcp.tool()
    async def show_alarms(
        max: int = 100,
        days: int = 0,
        hours: int = 1,
        detail: bool = False,
        simple: bool = False,
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """SD-WAN Manager alarms (sdwan show alarms)."""
        args = _build(
            ShowAlarmsArgs,
            max=max,
            days=days,
            hours=hours,
            detail=detail,
            simple=simple,
            include=include,
            exclude=exclude,
        )
        return await _run_show(args, output_format, manager)

    @mcp.tool()
    async def show_events(
        max: int = 100,
        days: int = 0,
        hours: int = 1,
        detail: bool = False,
        simple: bool = False,
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """SD-WAN Manager events (sdwan show events)."""
        args = _build(
            ShowEventsArgs,
            max=max,
            days=days,
            hours=hours,
            detail=detail,
            simple=simple,
            include=include,
            exclude=exclude,
        )
        return await _run_show(args, output_format, manager)

    @mcp.tool()
    async def list_sdwan_managers() -> str:
        """List configured SD-WAN Managers and the default. Pass a name as the `manager` argument to a show tool to select one."""
        return await asyncio.to_thread(list_sdwan_managers_info)

    @mcp.tool()
    async def list_show_operational_commands() -> str:
        """List valid command groups and command names for realtime, state, and statistics show subtasks."""
        return await asyncio.to_thread(operational_commands_help)

    return mcp


def build_http_app(config: AppConfig) -> Starlette:
    """Starlette app with streamable HTTP MCP and hardening middleware."""
    mcp = create_mcp(config)
    app: Starlette = mcp.streamable_http_app()

    token_secret = config.mcp.bearer_token
    token = token_secret.get_secret_value() if token_secret else None
    host = config.mcp.host

    if token:
        app.add_middleware(BearerTokenMiddleware, expected_token=token)
    elif host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "mcp.bearer_token is not set and host is not loopback; strongly set a bearer token for remote binds."
        )
    else:
        logger.warning(
            "mcp.bearer_token is not set; only suitable for trusted local development. "
            "Set mcp.bearer_token for any shared or remote access."
        )

    if not config.mcp.disable_rate_limit:
        app.add_middleware(
            RateLimitMiddleware,
            window_secs=config.limits.rate_limit_window_secs,
            max_requests=config.limits.rate_limit_max_requests,
            trusted_proxies=config.limits.rate_limit_trusted_proxies,
        )

    app.add_middleware(MaxBodySizeMiddleware, max_body_bytes=config.limits.max_body_bytes)

    if config.mcp.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.mcp.cors_origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Mcp-Session-Id"],
            expose_headers=["Mcp-Session-Id"],
        )

    return app
