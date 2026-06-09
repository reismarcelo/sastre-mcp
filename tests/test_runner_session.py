"""run_show_args integration with the session pool (no live SDWAN Manager)."""

from sastre_mcp import runner
from sastre_mcp.session_pool import SessionPoolTimeout


def test_run_show_args_passes_pool_settings(monkeypatch):
    captured = {}

    def fake_run_with_session(**kwargs):
        captured.update(kwargs)
        return ["table-1"]

    monkeypatch.setattr(runner, "run_with_session", fake_run_with_session)

    out = runner.run_show_args(object(), output_format="text", manager="primary")

    assert out == "table-1"
    assert captured["key"] == "primary"
    assert captured["max_size"] >= 1
    assert isinstance(captured["fingerprint"], str) and captured["fingerprint"]
    assert callable(captured["factory"])


def test_run_show_args_handles_pool_timeout(monkeypatch):
    def fake_run_with_session(**kwargs):
        raise SessionPoolTimeout("busy")

    monkeypatch.setattr(runner, "run_with_session", fake_run_with_session)

    out = runner.run_show_args(object(), manager="primary")

    assert out.startswith("Error: ")
    assert "busy handling other requests" in out
    assert "reference id:" in out


def test_run_show_args_unknown_manager_is_user_safe(monkeypatch):
    out = runner.run_show_args(object(), manager="does-not-exist")
    assert out.startswith("Error: ")
    assert "does-not-exist" in out
