"""FastMCP application: `sdwan show`, `sdwan list` and `sdwan show-template` tools."""

import asyncio
import logging
from typing import Any, Literal

from cisco_sdwan.tasks.implementation import (
    ListCertificateArgs,
    ListConfigArgs,
    ShowAlarmsArgs,
    ShowDevicesArgs,
    ShowEventsArgs,
    ShowRealtimeArgs,
    ShowStateArgs,
    ShowStatisticsArgs,
    ShowTemplateRefArgs,
    ShowTemplateValuesArgs,
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
    ListTaskArgs,
    ShowTaskArgs,
    ShowTemplateTaskArgs,
    configuration_tags_help,
    list_sdwan_managers_info,
    operational_commands_help,
    run_list_args,
    run_show_args,
    run_show_template_args,
)

logger = logging.getLogger(__name__)


def _build[ModelT: BaseModel](model_cls: type[ModelT], /, **fields: Any) -> ModelT:
    """Construct a cisco-sdwan Show*Args model, surfacing a clean error message.

    The Show Args classes validate their own domain rules (regex/site/command syntax and the regex and detail/simple
    mutual-exclusions). Surface only the first error as a single line, which is more useful to an LLM tool caller
    than a full multi-error Pydantic dump.

    ``None`` values are dropped so unset optional fields fall back to their model defaults; the SDK's per-field
    validators (e.g. site-id) run on any value that is explicitly supplied and would otherwise reject ``None``.
    """
    provided = {name: value for name, value in fields.items() if value is not None}
    try:
        return model_cls(**provided)
    except ValidationError as exc:
        err = exc.errors()[0]
        msg = err.get("msg", str(exc))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        raise ValueError(msg) from exc


async def _run_show(args: ShowTaskArgs, output_format: Literal["text", "json"], manager: str | None) -> str:
    """Run a blocking show task in a worker thread (the SDK is synchronous)."""
    return await asyncio.to_thread(
        run_show_args, args, output_format=output_format, manager=manager
    )


async def _run_list(args: ListTaskArgs, output_format: Literal["text", "json"], manager: str | None) -> str:
    """Run a blocking list task in a worker thread (the SDK is synchronous)."""
    return await asyncio.to_thread(
        run_list_args, args, output_format=output_format, manager=manager
    )


async def _run_show_template(
    args: ShowTemplateTaskArgs, output_format: Literal["text", "json"], manager: str | None
) -> str:
    """Run a blocking show-template task in a worker thread (the SDK is synchronous)."""
    return await asyncio.to_thread(
        run_show_template_args, args, output_format=output_format, manager=manager
    )


def create_mcp(config: AppConfig) -> FastMCP:
    mcp = FastMCP(
        "sastre-show",
        instructions=(
            "Tools wrap Cisco Sastre (cisco-sdwan) `sdwan show`, `sdwan show-template`, and `sdwan list` for SD-WAN Manager. "
            "SD-WAN Manager credentials come from the server config.yaml file (validated at startup), not from tool arguments. "
            "Multiple managers can be configured, each with a unique name. Use `list_sdwan_managers` to discover them, "
            "then pass the `manager` argument to a show or list tool to choose one; omit it to use the default."
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
        """List the SD-WAN device inventory from SD-WAN Manager (`sdwan show devices`).

        Returns one table with columns: Name, System IP, Site ID, Reachability, Type, Model.
        This is the best first call to discover devices and to obtain the `system_ip`, `site`, and
        `device_type` values used to scope the other show tools.

        Device-selection filters (all optional; combined with logical AND):
            regex: Select only devices whose hostname OR model matches this Python regular
                expression (substring search). Mutually exclusive with `not_regex`.
            not_regex: Select only devices whose hostname AND model do NOT match this regex.
                Mutually exclusive with `regex`.
            reachable: When true, return only devices currently in the "reachable" state.
            site: Select only devices at this site ID (a numeric site identifier, e.g. "100").
            system_ip: Select only devices whose system IP is in this list of IPv4 addresses
                (e.g. ["10.0.0.1", "10.0.0.2"]).
            device_type: Select only devices of this type. One of:
                "vsmart", "vmanage", "vbond", "vedge" (SD-WAN/Viptela OS), "cedge" (IOS-XE SD-WAN).

        Output row filters (applied to the rendered table rows, not device selection):
            include: Keep only table rows matching this regex; all other rows are dropped.
            exclude: Drop table rows matching this regex.

        output_format: "text" for human-readable tables (default) or "json" for structured output.
        manager: Name of the configured SD-WAN Manager to query. Omit to use the default.
            Use `list_sdwan_managers` to see the available names.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
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
        """Retrieve realtime operational data from devices (`sdwan show realtime <cmd>...`).

        SD-WAN Manager polls each selected device live, so this returns the most up-to-date data but
        is the SLOWEST and most expensive show tool (one request per device, per command). Prefer
        `show_state` (synced, near-real-time) or `show_statistics` (historical) when they expose an
        equivalent command. Narrow the device set with the filters below to keep queries fast.

        cmd (required): One or more realtime operational commands to run, e.g. ["app-route", "stats"]
            or a command group, or the group "all" to run every command in a category. Command and
            group names are validated; invalid values are rejected. Call
            `list_show_operational_commands` first to get the exact valid realtime groups/commands.
            Commands not applicable to a selected device's model are skipped automatically.

        Device-selection filters (all optional; combined with logical AND):
            regex: Select only devices whose hostname OR model matches this Python regex (substring
                search). Mutually exclusive with `not_regex`.
            not_regex: Select only devices whose hostname AND model do NOT match. Mutually exclusive
                with `regex`.
            reachable: When true, query only devices currently in the "reachable" state.
            site: Select only devices at this site ID (numeric site identifier, e.g. "100").
            system_ip: Select only devices whose system IP is in this list of IPv4 addresses.
            device_type: One of "vsmart", "vmanage", "vbond", "vedge", "cedge".

        Column / output controls:
            detail: Return additional columns (more detailed output). Mutually exclusive with `simple`.
            simple: Return fewer columns (condensed output). Mutually exclusive with `detail`.
            include: Keep only table rows matching this regex; drop all others.
            exclude: Drop table rows matching this regex.
            output_format: "text" (default) or "json".
            manager: Configured SD-WAN Manager name; omit for the default. See `list_sdwan_managers`.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
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
        """Retrieve synced state operational data from devices (`sdwan show state <cmd>...`).

        State data is collected and kept synced by SD-WAN Manager, so it is up-to-date AND fast
        (retrieved in bulk from the Manager rather than polling each device). Prefer this over
        `show_realtime` whenever an equivalent state command exists.

        cmd (required): One or more state operational commands, or a command group, or the group
            "all" for every command in a category. Names are validated and invalid values are
            rejected; call `list_show_operational_commands` for the exact valid state groups/commands.

        Device-selection filters (all optional; combined with logical AND):
            regex: Select devices whose hostname OR model matches this regex. Mutually exclusive
                with `not_regex`.
            not_regex: Select devices whose hostname AND model do NOT match. Mutually exclusive
                with `regex`.
            reachable: When true, include only devices currently "reachable".
            site: Select only devices at this site ID (numeric, e.g. "100").
            system_ip: Select only devices whose system IP is in this list of IPv4 addresses.
            device_type: One of "vsmart", "vmanage", "vbond", "vedge", "cedge".

        Column / output controls:
            detail: Return additional columns. Mutually exclusive with `simple`.
            simple: Return fewer columns. Mutually exclusive with `detail`.
            include: Keep only table rows matching this regex; drop all others.
            exclude: Drop table rows matching this regex.
            output_format: "text" (default) or "json".
            manager: Configured SD-WAN Manager name; omit for the default. See `list_sdwan_managers`.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
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
        """Retrieve statistics from the SD-WAN Manager analytics store (`sdwan show statistics <cmd>...`).

        Statistics come from the Manager's database, so this is fast but the data is typically 30+
        minutes old. Unlike `show_realtime`/`show_state`, it supports HISTORICAL queries via `days`
        and `hours`. Values are averaged over a 5-minute window within a 2-hour query range that ends
        at the requested point in time; with `days=0` and `hours=0` the range is the most recent 2 hours.

        cmd (required): One or more statistics commands, or a command group, or the group "all" for
            every command in a category. Names are validated and invalid values are rejected; call
            `list_show_operational_commands` for the exact valid statistics groups/commands.

        Time window (for historical queries; both default to 0 = now):
            days: Query statistics from this many days ago (0-9999).
            hours: Query statistics from this many hours ago (0-9999). Combined with `days`
                (e.g. days=1, hours=12 = 36 hours ago).

        Device-selection filters (all optional; combined with logical AND):
            regex: Select devices whose hostname OR model matches this regex. Mutually exclusive
                with `not_regex`.
            not_regex: Select devices whose hostname AND model do NOT match. Mutually exclusive
                with `regex`.
            reachable: When true, include only devices currently "reachable".
            site: Select only devices at this site ID (numeric, e.g. "100").
            system_ip: Select only devices whose system IP is in this list of IPv4 addresses.
            device_type: One of "vsmart", "vmanage", "vbond", "vedge", "cedge".

        Column / output controls:
            detail: Return additional columns. Mutually exclusive with `simple`.
            simple: Return fewer columns. Mutually exclusive with `detail`.
            include: Keep only table rows matching this regex; drop all others.
            exclude: Drop table rows matching this regex.
            output_format: "text" (default) or "json".
            manager: Configured SD-WAN Manager name; omit for the default. See `list_sdwan_managers`.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
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
        """Retrieve alarms recorded by SD-WAN Manager (`sdwan show alarms`).

        Returns alarm records over a recent time window. By default, this is the last 1 hour; widen it
        with `days` and/or `hours`. Alarms are fabric-wide Manager records (not per-device polling),
        so there are no device-selection filters here.

        Time window (the window starts this far in the past and ends now):
            days: Include alarms from this many days ago (0-9999, default 0).
            hours: Include alarms from this many hours ago (0-9999, default 1). Combined with `days`.

        Other controls:
            max: Maximum number of records to retrieve (1-999999, default 100).
            detail: Return additional columns. Mutually exclusive with `simple`.
            simple: Return fewer columns. Mutually exclusive with `detail`.
            include: Keep only table rows matching this regex; drop all others.
            exclude: Drop table rows matching this regex.
            output_format: "text" (default) or "json".
            manager: Configured SD-WAN Manager name; omit for the default. See `list_sdwan_managers`.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
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
        """Retrieve events recorded by SD-WAN Manager (`sdwan show events`).

        Returns event records over a recent time window. By default, this is the last 1 hour; widen it
        with `days` and/or `hours`. Events are fabric-wide Manager records (not per-device polling),
        so there are no device-selection filters here.

        Time window (the window starts this far in the past and ends now):
            days: Include events from this many days ago (0-9999, default 0).
            hours: Include events from this many hours ago (0-9999, default 1). Combined with `days`.

        Other controls:
            max: Maximum number of records to retrieve (1-999999, default 100).
            detail: Return additional columns. Mutually exclusive with `simple`.
            simple: Return fewer columns. Mutually exclusive with `detail`.
            include: Keep only table rows matching this regex; drop all others.
            exclude: Drop table rows matching this regex.
            output_format: "text" (default) or "json".
            manager: Configured SD-WAN Manager name; omit for the default. See `list_sdwan_managers`.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
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
    async def show_template_values(
        templates: str | None = None,
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """Show the variable values from device-template attachments (`sdwan show-template values`).

        For every device template that has devices attached, this returns one table per attached
        device with columns: Name, Value, Variable. "Name" is the human-readable variable title,
        "Variable" is the underlying variable key, and "Value" is the value supplied for that device.
        Use this to inspect the per-device input values bound to attached device templates.

        Selection filter (optional):
            templates: Python regular expression selecting which device templates to inspect.
                Matches on the template name OR template ID (substring search). Omit to inspect
                every attached device template.

        Output row filters (applied to the rendered table rows, not template selection):
            include: Keep only table rows matching this regex; all other rows are dropped.
            exclude: Drop table rows matching this regex.

        output_format: "text" for human-readable tables (default) or "json" for structured output.
        manager: Name of the configured SD-WAN Manager to query. Omit to use the default.
            Use `list_sdwan_managers` to see the available names.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
        args = _build(
            ShowTemplateValuesArgs,
            templates=templates,
            include=include,
            exclude=exclude,
        )
        return await _run_show_template(args, output_format, manager)

    @mcp.tool()
    async def show_template_references(
        templates: str | None = None,
        with_refs: bool = False,
        filled_rows: bool = False,
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """Show how device templates reference feature templates (`sdwan show-template references`).

        Returns up to two tables:
            - Feature Template References: columns Feature Template, Type, Devices Attached,
              Device Templates (which device templates reference each feature template).
            - Device Template References: columns Device Template, Device Type, Feature Templates
              (which feature templates each device template is built from). CLI device templates are
              not applicable and are omitted.
        Use this to trace dependencies between feature templates and device templates before edits.

        Selection / display controls (all optional):
            templates: Python regular expression selecting templates to include. Matches feature
                template names for the feature-template table and device template names for the
                device-template table. Omit to include all.
            with_refs: When true, include only feature templates that have at least one
                device-template reference (hides unreferenced feature templates).
            filled_rows: When true, repeat the leading cell values on every row instead of
                collapsing them for visualization (useful for CSV/JSON-style consumption).
                Forced on automatically when output_format is "json" so every row carries its
                full leading cell values.

        Output row filters (applied to the rendered table rows):
            include: Keep only table rows matching this regex; all other rows are dropped.
            exclude: Drop table rows matching this regex.

        output_format: "text" for human-readable tables (default) or "json" for structured output.
        manager: Name of the configured SD-WAN Manager to query. Omit to use the default.
            Use `list_sdwan_managers` to see the available names.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
        # JSON consumers need complete rows; the collapsed (visualization) layout leaves blank
        # leading cells on continuation rows, so force filled_rows for structured output.
        if output_format == "json":
            filled_rows = True
        args = _build(
            ShowTemplateRefArgs,
            templates=templates,
            with_refs=with_refs,
            filled_rows=filled_rows,
            include=include,
            exclude=exclude,
        )
        return await _run_show_template(args, output_format, manager)

    @mcp.tool()
    async def list_configuration(
        tags: list[str],
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """List configuration items stored on SD-WAN Manager (`sdwan list configuration <tag>...`).

        Returns a table with columns: Name, ID, Tag, Type. Use this to discover templates, policies,
        config groups, and other Manager-stored configuration objects before backup, attach, or transform
        operations.

        tags (required): One or more catalog tags selecting groups of items (e.g. ["template_device"],
            ["policy_definition", "policy_list"], or ["all"] for every item). Must not be empty. Tag
            names are validated; invalid values are rejected. Call `list_configuration_tags` for the
            exact valid tag list.

        Output row filters (applied to the rendered table rows, not item selection):
            include: Keep only table rows matching this regex; all other rows are dropped.
            exclude: Drop table rows matching this regex.

        output_format: "text" for human-readable tables (default) or "json" for structured output.
        manager: Name of the configured SD-WAN Manager to query. Omit to use the default.
            Use `list_sdwan_managers` to see the available names.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
        if not tags:
            raise ValueError("At least one tag is required; call `list_configuration_tags` for valid values.")
        args = _build(
            ListConfigArgs,
            tags=tags,
            include=include,
            exclude=exclude,
        )
        return await _run_list(args, output_format, manager)

    @mcp.tool()
    async def list_certificates(
        include: str | None = None,
        exclude: str | None = None,
        output_format: Literal["text", "json"] = "text",
        manager: str | None = None,
    ) -> str:
        """List WAN edge device certificate information from SD-WAN Manager (`sdwan list certificate`).

        Returns a table with columns: Hostname, Chassis, Serial, State, Status. Use this to review
        certificate enrollment and status across the fabric before certificate maintenance tasks.

        Certificate states:
            - invalid: Device is NOT allowed to connect to SDWAN Manager or SDWAN Controllers.
            - staging: Device is allowed to connect to SDWAN Manager and SDWAN Controllers. However, no TLOCs are
            advertised to this device. And TLOCs from this device are not advertised to anyone else.
            In summary: Data plane stays down (mo BFD sessions are established) but control plane (OMP) stays up.
            - valid: Device is allowed to connect to SDWAN Manager and SDWAN Controllers.

        Output row filters (applied to the rendered table rows):
            include: Keep only table rows matching this regex; all other rows are dropped.
            exclude: Drop table rows matching this regex.

        output_format: "text" for human-readable tables (default) or "json" for structured output.
        manager: Name of the configured SD-WAN Manager to query. Omit to use the default.
            Use `list_sdwan_managers` to see the available names.

        Returns the formatted result, or an "Error: ..." string on failure (with a reference id).
        """
        args = _build(
            ListCertificateArgs,
            include=include,
            exclude=exclude,
        )
        return await _run_list(args, output_format, manager)

    @mcp.tool()
    async def list_sdwan_managers() -> str:
        """List the SD-WAN Managers configured on this server, and which one is the default.

        Takes no arguments. Returns each Manager's name, address:port, auth method, and tenant
        (credentials are never exposed). Use this to discover the valid values for the `manager`
        argument accepted by every show, show-template, and list tool. Pass a returned name as `manager` to target that
        Manager; omit `manager` to use the default. Call this when a request involves multiple
        Managers or when a show tool reports an unknown/invalid manager name.
        """
        return await asyncio.to_thread(list_sdwan_managers_info)

    @mcp.tool()
    async def list_show_operational_commands() -> str:
        """List the valid `cmd` values for `show_realtime`, `show_state`, and `show_statistics`.

        Takes no arguments. Returns, for each category (realtime, state, statistics), the available
        command GROUPS and individual COMMAND names, plus the special group "all" that selects every
        command in a category. Call this BEFORE `show_realtime`/`show_state`/`show_statistics` to pick
        valid `cmd` values, since those tools reject unknown command or group names. Note the catalogs
        differ per category: a command valid for one category may not exist in another.
        """
        return await asyncio.to_thread(operational_commands_help)

    @mcp.tool()
    async def list_configuration_tags() -> str:
        """List the valid `tags` values for `list_configuration`.

        Takes no arguments. Returns the catalog tags that select groups of configuration items on
        SD-WAN Manager, plus the special tag "all" that selects every item. Call this BEFORE
        `list_configuration` to pick valid `tags` values, since that tool rejects unknown tag names.
        """
        return await asyncio.to_thread(configuration_tags_help)

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
