"""Tool-level tests for the FastMCP server (no live vManage).

These call the registered MCP tools end-to-end (through ``call_tool``) with
``run_show_args`` patched out, exercising each tool body, the ``_run_show``
dispatch helper, and the direct construction of the cisco-sdwan ``Show*Args``
models.
"""

import asyncio
from typing import Any

import pytest
from sastre_mcp import server as srv
from sastre_mcp.config import default_test_config


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch run_show_args so tools resolve without contacting a manager."""
    record: dict[str, Any] = {}

    def fake_run_show_args(
        args: Any, *, output_format: str = "text", manager: str | None = None
    ) -> str:
        record["args_type"] = type(args).__name__
        record["output_format"] = output_format
        record["manager"] = manager
        return f"OK:{type(args).__name__}"

    monkeypatch.setattr(srv, "run_show_args", fake_run_show_args)
    return record


def _result_text(call_result: Any) -> str:
    """Extract the string payload from a FastMCP call_tool return value."""
    _blocks, structured = call_result
    return structured["result"]


def _call(tool: str, arguments: dict[str, Any]) -> Any:
    mcp = srv.create_mcp(default_test_config())
    return asyncio.run(mcp.call_tool(tool, arguments))


def test_show_devices_forwards_args(captured: dict[str, Any]) -> None:
    out = _result_text(_call("show_devices", {"reachable": True, "manager": "primary"}))
    assert out == "OK:ShowDevicesArgs"
    assert captured["args_type"] == "ShowDevicesArgs"
    assert captured["output_format"] == "text"
    assert captured["manager"] == "primary"


def test_show_realtime_forwards_cmd_and_json(captured: dict[str, Any]) -> None:
    out = _result_text(
        _call("show_realtime", {"cmd": ["omp", "adv-routes"], "output_format": "json"})
    )
    assert out == "OK:ShowRealtimeArgs"
    assert captured["output_format"] == "json"
    assert captured["manager"] is None


def test_show_state_tool(captured: dict[str, Any]) -> None:
    out = _result_text(_call("show_state", {"cmd": ["bfd", "sessions"]}))
    assert out == "OK:ShowStateArgs"
    assert captured["args_type"] == "ShowStateArgs"


def test_show_statistics_tool(captured: dict[str, Any]) -> None:
    out = _result_text(_call("show_statistics", {"cmd": ["app-route"], "days": 2}))
    assert out == "OK:ShowStatisticsArgs"


def test_show_alarms_tool(captured: dict[str, Any]) -> None:
    out = _result_text(_call("show_alarms", {"max": 10}))
    assert out == "OK:ShowAlarmsArgs"


def test_show_events_tool(captured: dict[str, Any]) -> None:
    out = _result_text(_call("show_events", {"hours": 2}))
    assert out == "OK:ShowEventsArgs"


def test_list_sdwan_managers_tool() -> None:
    out = _result_text(_call("list_sdwan_managers", {}))
    assert "primary" in out
    assert "(default)" in out


def test_list_show_operational_commands_tool() -> None:
    out = _result_text(_call("list_show_operational_commands", {}))
    assert "realtime" in out
    assert "state" in out
    assert "statistics" in out


def test_build_devices_valid() -> None:
    from cisco_sdwan.tasks.implementation import ShowDevicesArgs

    args = srv._build(ShowDevicesArgs, reachable=True)
    assert isinstance(args, ShowDevicesArgs)
    assert args.reachable is True


def test_build_regex_mutex_clean_error() -> None:
    from cisco_sdwan.tasks.implementation import ShowDevicesArgs

    with pytest.raises(ValueError, match="not allowed with"):
        srv._build(ShowDevicesArgs, regex="a", not_regex="b")


def test_build_detail_simple_mutex_clean_error() -> None:
    from cisco_sdwan.tasks.implementation import ShowRealtimeArgs

    with pytest.raises(ValueError, match="not allowed with"):
        srv._build(ShowRealtimeArgs, cmd=["omp", "adv-routes"], detail=True, simple=True)


def test_build_invalid_command_clean_error() -> None:
    from cisco_sdwan.tasks.implementation import ShowRealtimeArgs

    with pytest.raises(ValueError, match="not valid"):
        srv._build(ShowRealtimeArgs, cmd=["not-a-real-command-xyz"])
