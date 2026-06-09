"""Validation rules for AppConfig and its sub-models."""

import pytest
from pydantic import ValidationError
from sastre_mcp.config import (
    AppConfig,
    McpConfig,
    SdwanManagerConfig,
)


def _manager(**overrides) -> dict:
    base = {
        "name": "primary",
        "address": "10.0.0.1",
        "user": "admin",
        "password": "secret",
    }
    base.update(overrides)
    return base


def test_duplicate_manager_names_rejected() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        AppConfig.model_validate(
            {
                "sdwan_managers": [
                    _manager(name="dup"),
                    _manager(name="dup", address="10.0.0.2"),
                ]
            }
        )


def test_default_manager_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="does not match any sdwan_managers"):
        AppConfig.model_validate(
            {
                "sdwan_managers": [_manager(name="primary")],
                "default_manager": "nope",
            }
        )


def test_default_manager_defaults_to_first() -> None:
    cfg = AppConfig.model_validate(
        {
            "sdwan_managers": [
                _manager(name="first"),
                _manager(name="second", address="10.0.0.2"),
            ]
        }
    )
    assert cfg.default_manager == "first"


def test_default_manager_explicit_match_kept() -> None:
    cfg = AppConfig.model_validate(
        {
            "sdwan_managers": [
                _manager(name="first"),
                _manager(name="second", address="10.0.0.2"),
            ],
            "default_manager": "second",
        }
    )
    assert cfg.default_manager == "second"


def test_apikey_alone_is_valid() -> None:
    mgr = SdwanManagerConfig.model_validate(
        {"name": "p", "address": "10.0.0.1", "apikey": "abc123"}
    )
    assert mgr.apikey is not None
    assert mgr.user is None


def test_missing_user_password_and_apikey_rejected() -> None:
    with pytest.raises(ValidationError, match="user and password are required"):
        SdwanManagerConfig.model_validate({"name": "p", "address": "10.0.0.1"})


def test_user_without_password_rejected() -> None:
    with pytest.raises(ValidationError, match="user and password are required"):
        SdwanManagerConfig.model_validate({"name": "p", "address": "10.0.0.1", "user": "admin"})


def test_empty_password_rejected() -> None:
    with pytest.raises(ValidationError, match="password must be non-empty"):
        SdwanManagerConfig.model_validate(
            {"name": "p", "address": "10.0.0.1", "user": "admin", "password": "   "}
        )


def test_blank_apikey_falls_back_to_user_password_rule() -> None:
    # A whitespace-only apikey is treated as unset, so user/password become required.
    with pytest.raises(ValidationError, match="user and password are required"):
        SdwanManagerConfig.model_validate({"name": "p", "address": "10.0.0.1", "apikey": "   "})


def test_port_coerced_from_str_to_int() -> None:
    mgr = SdwanManagerConfig.model_validate(_manager(port="8443"))
    assert mgr.port == 8443


def test_port_whitespace_stripped() -> None:
    mgr = SdwanManagerConfig.model_validate(_manager(port="  443  "))
    assert mgr.port == 443


def test_port_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        SdwanManagerConfig.model_validate(_manager(port=0))
    with pytest.raises(ValidationError):
        SdwanManagerConfig.model_validate(_manager(port=70000))


def test_port_non_numeric_rejected() -> None:
    with pytest.raises(ValidationError):
        SdwanManagerConfig.model_validate(_manager(port="https"))


def test_empty_bearer_token_normalized_to_none() -> None:
    mcp = McpConfig.model_validate({"bearer_token": "   "})
    assert mcp.bearer_token is None


def test_nonempty_bearer_token_kept() -> None:
    mcp = McpConfig.model_validate({"bearer_token": "tok"})
    assert mcp.bearer_token is not None
    assert mcp.bearer_token.get_secret_value() == "tok"


def test_wan_bind_requires_bearer() -> None:
    with pytest.raises(ValidationError, match="bearer_token is required"):
        AppConfig.model_validate(
            {
                "sdwan_managers": [_manager()],
                "mcp": {"host": "0.0.0.0"},
            }
        )


def test_ipv6_any_bind_requires_bearer() -> None:
    with pytest.raises(ValidationError, match="bearer_token is required"):
        AppConfig.model_validate(
            {
                "sdwan_managers": [_manager()],
                "mcp": {"host": "::"},
            }
        )


def test_wan_bind_with_bearer_is_valid() -> None:
    cfg = AppConfig.model_validate(
        {
            "sdwan_managers": [_manager()],
            "mcp": {"host": "0.0.0.0", "bearer_token": "tok"},
        }
    )
    assert cfg.mcp.host == "0.0.0.0"


def test_extra_keys_forbidden() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"sdwan_managers": [_manager()], "unexpected": 1})


def test_empty_managers_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"sdwan_managers": []})


def test_get_manager_unknown_raises() -> None:
    cfg = AppConfig.model_validate({"sdwan_managers": [_manager(name="primary")]})
    with pytest.raises(ValueError, match="Unknown sdwan_manager"):
        cfg.get_manager("ghost")


def test_get_manager_defaults_to_default_manager() -> None:
    cfg = AppConfig.model_validate(
        {
            "sdwan_managers": [
                _manager(name="first"),
                _manager(name="second", address="10.0.0.2"),
            ],
            "default_manager": "second",
        }
    )
    assert cfg.get_manager().name == "second"
