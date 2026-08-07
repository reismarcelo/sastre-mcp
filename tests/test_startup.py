"""Startup behavior of the CLI entry point on unusable configuration.

``configure_logging`` is stubbed out in every test: it applies a global ``dictConfig`` that would
outlive the test and interfere with the rest of the session.
"""

from pathlib import Path

import pytest
from sastre_mcp import __main__ as entry


@pytest.fixture(autouse=True)
def _quiet_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry, "configure_logging", lambda _path: None)
    monkeypatch.chdir(tmp_path)


def _run_main_expecting_exit() -> int | str | None:
    def fail_if_served(*args: object, **kwargs: object) -> None:
        raise AssertionError("uvicorn.run must not be reached when the config is unusable")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(entry.uvicorn, "run", fail_if_served)
        with pytest.raises(SystemExit) as excinfo:
            entry.main()
    return excinfo.value.code


def test_missing_config_exits_nonzero() -> None:
    assert _run_main_expecting_exit() == 1


def test_invalid_config_exits_nonzero(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("sdwan_managers: []\n", encoding="utf-8")
    assert _run_main_expecting_exit() == 1


def test_unset_env_var_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_PW", raising=False)
    (tmp_path / "config.yaml").write_text(
        "sdwan_managers:\n"
        '  - name: "primary"\n'
        '    address: "10.0.0.1"\n'
        '    user: "admin"\n'
        '    password: "${MISSING_PW}"\n',
        encoding="utf-8",
    )
    assert _run_main_expecting_exit() == 1


def test_startup_failure_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "config.yaml").write_text("sdwan_managers: []\n", encoding="utf-8")
    with caplog.at_level("ERROR"):
        _run_main_expecting_exit()
    assert "Startup failed" in caplog.text
