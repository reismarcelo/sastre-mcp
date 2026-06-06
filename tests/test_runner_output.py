"""Output formatting of run_show_args (no live vManage, mocked session run)."""

import json

from sastre_mcp import runner


class _FakeTable:
    """Stand-in for a cisco_sdwan results table (str + dict serializable)."""

    def __init__(self, name: str, rows: list[dict]) -> None:
        self._name = name
        self._rows = rows

    def __str__(self) -> str:
        return f"<table {self._name}: {len(self._rows)} rows>"

    def dict(self) -> dict:
        return {"name": self._name, "rows": self._rows}


def _patch_session(monkeypatch, tables):
    def fake_run_with_session(**kwargs):
        return tables

    monkeypatch.setattr(runner, "run_with_session", fake_run_with_session)


def test_text_output_joins_tables(monkeypatch):
    tables = [_FakeTable("a", [{"x": 1}]), _FakeTable("b", [{"y": 2}])]
    _patch_session(monkeypatch, tables)

    out = runner.run_show_args(object(), output_format="text", manager="primary")

    assert out == "<table a: 1 rows>\n\n<table b: 1 rows>"


def test_json_output_serializes_table_dicts(monkeypatch):
    tables = [_FakeTable("a", [{"x": 1}]), _FakeTable("b", [{"y": 2}])]
    _patch_session(monkeypatch, tables)

    out = runner.run_show_args(object(), output_format="json", manager="primary")

    assert json.loads(out) == [
        {"name": "a", "rows": [{"x": 1}]},
        {"name": "b", "rows": [{"y": 2}]},
    ]


def test_empty_tables_returns_friendly_message(monkeypatch):
    _patch_session(monkeypatch, [])

    out = runner.run_show_args(object(), output_format="text", manager="primary")

    assert "No tabular output" in out


def test_empty_tables_message_same_for_json(monkeypatch):
    _patch_session(monkeypatch, [])

    out = runner.run_show_args(object(), output_format="json", manager="primary")

    assert "No tabular output" in out


def test_default_manager_used_when_none(monkeypatch):
    captured = {}

    def fake_run_with_session(**kwargs):
        captured.update(kwargs)
        return [_FakeTable("a", [])]

    monkeypatch.setattr(runner, "run_with_session", fake_run_with_session)

    runner.run_show_args(object(), output_format="text", manager=None)

    # default_test_config names the single manager "primary".
    assert captured["key"] == "primary"


def test_sdk_exception_sanitized_with_reference_id(monkeypatch):
    import requests

    def fake_run_with_session(**kwargs):
        raise requests.exceptions.ConnectionError("raw internal detail")

    monkeypatch.setattr(runner, "run_with_session", fake_run_with_session)

    out = runner.run_show_args(object(), manager="primary")

    assert out.startswith("Error: ")
    assert "Could not connect" in out
    assert "reference id:" in out
    # The raw exception text must not leak to the client.
    assert "raw internal detail" not in out


def test_login_failure_sanitized(monkeypatch):
    from cisco_sdwan.base.rest_api import LoginFailedException

    def fake_run_with_session(**kwargs):
        raise LoginFailedException("bad creds internal")

    monkeypatch.setattr(runner, "run_with_session", fake_run_with_session)

    out = runner.run_show_args(object(), manager="primary")

    assert "Authentication to SD-WAN Manager failed" in out
    assert "bad creds internal" not in out


def test_list_sdwan_managers_info_never_leaks_secrets():
    out = runner.list_sdwan_managers_info()
    assert "primary" in out
    assert "(default)" in out
    assert "user/password" in out
    # The configured password must never appear in the listing.
    assert "testpass" not in out


def test_operational_commands_help_lists_categories():
    out = runner.operational_commands_help()
    assert "realtime" in out
    assert "state" in out
    assert "statistics" in out
