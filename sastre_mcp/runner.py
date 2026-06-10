"""Run Sastre TaskShow / TaskList against SDWAN Manager using cisco_sdwan in-process."""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Literal

import requests
from cisco_sdwan.base.rest_api import (
    BadTenantException,
    LoginFailedException,
    Rest,
    RestAPIException,
    ServerRateLimitException,
)
from cisco_sdwan.tasks.common import Task
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
    TaskList,
    TaskShow,
    TaskShowTemplate,
)

from sastre_mcp.config import SdwanManagerConfig, get_config, manager_base_url
from sastre_mcp.session_pool import SessionPoolTimeout, run_with_session

logger = logging.getLogger(__name__)

Format = Literal["text", "json"]


def _clean_error_message(exc: Exception) -> str:
    """Map an SDK/transport exception to a client-safe message (no internals)."""
    if isinstance(exc, SessionPoolTimeout):
        return "SD-WAN Manager is busy handling other requests; please retry in a few moments."
    if isinstance(exc, LoginFailedException):
        return "Authentication to SD-WAN Manager failed; check the configured credentials or API key."
    if isinstance(exc, BadTenantException):
        return "The configured tenant is invalid for this SD-WAN Manager."
    if isinstance(exc, ServerRateLimitException):
        return "SD-WAN Manager is rate-limiting requests; please retry later."
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS/SSL error while connecting to SD-WAN Manager."
    if isinstance(exc, requests.exceptions.Timeout):
        return "Timed out while communicating with SD-WAN Manager."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "Could not connect to SD-WAN Manager; the host may be unreachable."
    if isinstance(exc, RestAPIException):
        return "SD-WAN Manager returned an error while processing the request."
    if isinstance(exc, requests.exceptions.RequestException):
        return "A network error occurred while communicating with SD-WAN Manager."
    return "An unexpected error occurred while executing the task."


type ShowTaskArgs = (ShowDevicesArgs | ShowRealtimeArgs | ShowStateArgs | ShowStatisticsArgs | ShowAlarmsArgs
                     | ShowEventsArgs)

type ListTaskArgs = ListConfigArgs | ListCertificateArgs

type ShowTemplateTaskArgs = ShowTemplateValuesArgs | ShowTemplateRefArgs


def _session_fingerprint(manager: SdwanManagerConfig) -> str:
    """Stable hash of connection params so the pool rebuilds when they change.

    Secrets are hashed (never stored or logged in the clear) only to detect configuration changes between reloads.
    """
    parts = [
        manager_base_url(manager),
        manager.user or "",
        manager.password.get_secret_value() if manager.password else "",
        manager.apikey.get_secret_value() if manager.apikey else "",
        manager.tenant or "",
        str(manager.timeout),
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _make_rest_factory(manager: SdwanManagerConfig) -> Callable[[], Rest]:
    def factory() -> Rest:
        return Rest(
            manager_base_url(manager),
            manager.user,
            manager.password.get_secret_value() if manager.password else None,
            apikey=manager.apikey.get_secret_value() if manager.apikey else None,
            tenant_name=manager.tenant,
            timeout=manager.timeout,
        )

    return factory


def _format_task_tables(tables: Sequence[Any] | None, *, output_format: Format) -> str:
    if not tables:
        return "No tabular output (empty result, or data exported only to files)."

    if output_format == "json":
        # Serialize in-memory (mirrors cisco_sdwan.tasks.common.export_json)
        # to avoid round-tripping through a temp file on disk.
        return json.dumps([table.dict() for table in tables], indent=2)

    return "\n\n".join(str(entry) for entry in tables)


def _run_task_args(
    args: ShowTaskArgs | ListTaskArgs | ShowTemplateTaskArgs,
    task_cls: type[Task],
    *,
    output_format: Format = "text",
    manager: str | None = None,
    task_label: str,
) -> str:
    """
    Execute a Sastre task via ``Task*.runner`` with validated Args (Sastre SDK pattern).

    ``manager`` selects which configured SD-WAN Manager to connect; when None, the configured default_manager is used.
    """
    cfg = get_config()
    try:
        sdwan_manager = cfg.get_manager(manager)
    except ValueError as exc:
        # Manager-selection errors are user-actionable and already safe to surface.
        return f"Error: {exc}"

    correlation_id = uuid.uuid4().hex[:12]
    try:
        task = task_cls()
        tables = run_with_session(
            key=sdwan_manager.name,
            fingerprint=_session_fingerprint(sdwan_manager),
            factory=_make_rest_factory(sdwan_manager),
            max_size=cfg.limits.max_sessions_per_manager,
            max_idle_secs=cfg.limits.session_max_idle_secs,
            acquire_timeout_secs=cfg.limits.session_acquire_timeout_secs,
            operation=lambda api: task.runner(args, api),
        )
        return _format_task_tables(tables, output_format=output_format)
    except Exception as exc:
        # Full detail (incl. traceback) is logged server-side only; the client
        # receives a sanitized message plus a correlation id for support.
        logger.exception(
            f"{task_label} failed [ref={correlation_id}] manager={sdwan_manager.name} output_format={output_format}"
        )
        return f"Error: {_clean_error_message(exc)} (reference id: {correlation_id})"


def run_show_args(args: ShowTaskArgs, *, output_format: Format = "text", manager: str | None = None) -> str:
    """Execute ``sdwan show ...`` via TaskShow.runner with validated Show*Args."""
    return _run_task_args(
        args, TaskShow, output_format=output_format, manager=manager, task_label="show task"
    )


def run_list_args(args: ListTaskArgs, *, output_format: Format = "text", manager: str | None = None) -> str:
    """Execute ``sdwan list ...`` via TaskList.runner with validated List*Args."""
    return _run_task_args(
        args, TaskList, output_format=output_format, manager=manager, task_label="list task"
    )


def run_show_template_args(
    args: ShowTemplateTaskArgs, *, output_format: Format = "text", manager: str | None = None
) -> str:
    """Execute ``sdwan show-template ...`` via TaskShowTemplate.runner with validated ShowTemplate*Args."""
    return _run_task_args(
        args, TaskShowTemplate, output_format=output_format, manager=manager, task_label="show-template task"
    )


def list_sdwan_managers_info() -> str:
    """Return configured SD-WAN Managers (names, addresses; never secrets)."""
    cfg = get_config()
    lines = ["Configured SD-WAN Managers:"]
    for manager in cfg.sdwan_managers:
        is_default = " (default)" if manager.name == cfg.default_manager else ""
        auth = "apikey" if manager.apikey else "user/password"
        lines.append(
            f"- {manager.name}{is_default}: {manager.address}:{manager.port} "
            f"(auth: {auth}, tenant: {manager.tenant or 'none'})"
        )
    lines.append("")
    lines.append(
        "Pass the chosen name as the `manager` argument to a show, show-template, or list tool; "
        "omit it to use the default."
    )
    return "\n".join(lines)


def operational_commands_help() -> str:
    """List show command groups and commands for RT / STATE / STATS (static catalog text)."""
    from cisco_sdwan.base.catalog import CATALOG_TAG_ALL, OpType
    from cisco_sdwan.tasks.utils import OpCmdOptions

    lines = [
        "Operational command groups and commands for `show realtime|state|statistics`.",
        f'Use group "{CATALOG_TAG_ALL}" to select all commands in a category.',
        "",
    ]
    for op_type, label in (
        (OpType.RT, "realtime (OpType.RT)"),
        (OpType.STATE, "state (OpType.STATE)"),
        (OpType.STATS, "statistics (OpType.STATS)"),
    ):
        lines.append(f"## {label}")
        lines.append(f"Groups: {OpCmdOptions.tags(op_type)}")
        lines.append(f"Commands: {OpCmdOptions.commands(op_type)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def configuration_tags_help() -> str:
    """List configuration catalog tags for ``sdwan list configuration`` (static catalog text)."""
    from cisco_sdwan.base.catalog import CATALOG_TAG_ALL
    from cisco_sdwan.tasks.utils import TagOptions

    return "\n".join(
        [
            "Configuration catalog tags for `list configuration`.",
            f'Use tag "{CATALOG_TAG_ALL}" to select all configuration items.',
            "",
            f"Tags: {TagOptions.options()}",
        ]
    )
