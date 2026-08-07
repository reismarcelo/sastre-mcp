"""Tests for environment-variable indirection in config loading."""

from pathlib import Path

import pytest
from sastre_mcp.config import load_config


def _write_config(tmp_path: Path, password: str, bearer: str = "tok") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "sdwan_managers:\n"
        '  - name: "primary"\n'
        '    address: "10.0.0.1"\n'
        '    user: "admin"\n'
        f'    password: "{password}"\n'
        "mcp:\n"
        f'  bearer_token: "{bearer}"\n',
        encoding="utf-8",
    )
    return path


def test_env_var_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIMARY_PW", "s3cret-from-env")
    cfg = load_config(_write_config(tmp_path, "${PRIMARY_PW}"))
    assert cfg.sdwan_managers[0].password.get_secret_value() == "s3cret-from-env"


def test_missing_env_var_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_PW", raising=False)
    with pytest.raises(RuntimeError, match="MISSING_PW"):
        load_config(_write_config(tmp_path, "${MISSING_PW}"))


def test_missing_env_var_error_names_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISSING_PW", raising=False)
    path = _write_config(tmp_path, "${MISSING_PW}")
    with pytest.raises(RuntimeError, match=str(path)):
        load_config(path)


def test_env_var_default_used_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPT_PW", raising=False)
    cfg = load_config(_write_config(tmp_path, "${OPT_PW:-fallback-pw}"))
    assert cfg.sdwan_managers[0].password.get_secret_value() == "fallback-pw"


def test_env_var_default_overridden_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPT_PW", "real-pw")
    cfg = load_config(_write_config(tmp_path, "${OPT_PW:-fallback-pw}"))
    assert cfg.sdwan_managers[0].password.get_secret_value() == "real-pw"


def test_literal_dollar_brace_escaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(_write_config(tmp_path, "literal-$${NOT_EXPANDED}"))
    assert cfg.sdwan_managers[0].password.get_secret_value() == "literal-${NOT_EXPANDED}"


def test_non_string_values_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PW", "p")
    path = tmp_path / "config.yaml"
    path.write_text(
        "sdwan_managers:\n"
        '  - name: "primary"\n'
        '    address: "10.0.0.1"\n'
        '    user: "admin"\n'
        '    password: "${PW}"\n'
        "    timeout: 300\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.sdwan_managers[0].timeout == 300
