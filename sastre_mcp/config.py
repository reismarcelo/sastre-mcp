"""YAML-backed configuration validated with Pydantic."""

import ipaddress
import os
import re
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

_active: AppConfig | None = None


def _expand_env_in_str(value: str) -> str:
    """Expand ${VAR} / ${VAR:-default} references using os.environ.

    A literal ``$${`` sequence is treated as an escape and collapses to ``${`` without expansion, so values that
    genuinely need ``${...}`` are still representable. References to unset variables without a default raise a
    clear error so misconfiguration fails fast instead of silently injecting an empty secret.
    """
    placeholder = "\x00SASTRE_DOLLAR\x00"
    escaped = value.replace("$${", placeholder)

    def replace(match: re.Match[str]) -> str:
        if (env_val := os.environ.get(match.group('name'))) is not None:
            return env_val
        if (default := match.group('default')) is not None:
            return default
        raise RuntimeError(
            f"Config references environment variable '{match.group('name')}' (via ${{{match.group('name')}}}) "
            f"which is not set. Set the variable or provide a default with ${{{match.group('name')}:-default}}."
        )

    # Matches ${VAR} or ${VAR:-default} for environment-variable indirection. VAR must be a typical shell-style
    # identifier so that real string values containing other "${...}" patterns are not accidentally treated as refs.
    pattern = r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?}"

    expanded = re.sub(pattern, replace, escaped)
    return expanded.replace(placeholder, "${")


def _expand_env_vars(data: Any) -> Any:
    """Recursively expand environment-variable references in config data."""
    if isinstance(data, dict):
        return {key: _expand_env_vars(val) for key, val in data.items()}
    if isinstance(data, list):
        return [_expand_env_vars(item) for item in data]
    if isinstance(data, str):
        return _expand_env_in_str(data)
    return data


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_body_bytes: Annotated[int, Field(ge=1)] = 2 * 1024 * 1024
    rate_limit_window_secs: Annotated[int, Field(ge=1)] = 60
    rate_limit_max_requests: Annotated[int, Field(ge=1)] = 120
    rate_limit_trusted_proxies: list[str] = Field(default_factory=list)
    # Per-manager pooling of logged-in SDWAN Manager sessions. max_sessions_per_manager also bounds the number of
    # concurrent connections opened against a single SD-WAN Manager (acts as a connection semaphore).
    max_sessions_per_manager: Annotated[int, Field(ge=1)] = 4
    # Drop an idle pooled session that has not been used for this many seconds, so it is re-created instead of risking
    # a server-side session timeout. 0 disables idle-based eviction.
    session_max_idle_secs: Annotated[int, Field(ge=0)] = 600
    # How long a request waits for a free session before giving up when all max_sessions_per_manager sessions are busy.
    session_acquire_timeout_secs: Annotated[int, Field(ge=1)] = 30

    @model_validator(mode="after")
    def validate_trusted_proxies(self) -> Self:
        for entry in self.rate_limit_trusted_proxies:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                raise ValueError(
                    f"rate_limit_trusted_proxies entry '{entry}' is not a valid IP address or CIDR network"
                ) from None
        return self


class SdwanManagerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    port: Annotated[int, Field(ge=1, le=65535)] = 443
    user: str | None = None
    password: SecretStr | None = None
    apikey: SecretStr | None = None
    tenant: str | None = None
    timeout: Annotated[int, Field(ge=1, le=3600)] = 300

    @model_validator(mode="after")
    def apikey_or_user_password(self) -> Self:
        if self.apikey is not None and self.apikey.get_secret_value().strip() != "":
            return self
        if not self.user or not self.password:
            raise ValueError(f"sdwan_manager {self.name}: user and password are required when apikey is not set")
        if self.password.get_secret_value().strip() == "":
            raise ValueError(f"sdwan_manager {self.name}: password must be non-empty when using user/password auth")
        return self


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8765
    bearer_token: SecretStr | None = None
    stateless_http: bool = True
    cors_origins: list[str] = Field(default_factory=list)
    disable_rate_limit: bool = False

    @model_validator(mode="after")
    def empty_bearer_as_none(self) -> Self:
        if self.bearer_token is not None and self.bearer_token.get_secret_value().strip() == "":
            return self.model_copy(update={"bearer_token": None})
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdwan_managers: list[SdwanManagerConfig] = Field(min_length=1)
    default_manager: str | None = None
    mcp: McpConfig = Field(default_factory=McpConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @model_validator(mode="after")
    def validate_managers(self) -> Self:
        names = [m.name for m in self.sdwan_managers]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"sdwan_managers names must be unique; duplicates: {', '.join(duplicates)}")
        if self.default_manager is None:
            return self.model_copy(update={"default_manager": names[0]})
        if self.default_manager not in names:
            raise ValueError(
                f"default_manager '{self.default_manager}' does not match any sdwan_managers name; "
                f"available: {', '.join(names)}"
            )
        return self

    @model_validator(mode="after")
    def bind_requires_bearer_when_wan(self) -> Self:
        if self.mcp.host in ("0.0.0.0", "::") and self.mcp.bearer_token is None:
            raise ValueError("mcp.bearer_token is required when mcp.host is 0.0.0.0 or ::")
        return self

    def manager_names(self) -> list[str]:
        return [m.name for m in self.sdwan_managers]

    def get_manager(self, name: str | None = None) -> SdwanManagerConfig:
        target = name if name is not None else self.default_manager
        for manager in self.sdwan_managers:
            if manager.name == target:
                return manager

        raise ValueError(f"Unknown sdwan_manager '{name}'. Available managers: {", ".join(self.manager_names())}")


def load_config(config_path: Path) -> AppConfig:
    if not config_path.is_file():
        raise RuntimeError(f"Config file not found: {config_path}")
    raw = config_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid YAML in {config_path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Config root must be a mapping, got {type(data).__name__}")
    data = _expand_env_vars(data)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise RuntimeError(f"Invalid configuration in {config_path}:\n{exc}") from exc


def set_active_config(cfg: AppConfig) -> None:
    global _active
    _active = cfg


def get_config() -> AppConfig:
    if _active is None:
        raise RuntimeError("Configuration not loaded; call set_active_config(load_config(...)) first")
    return _active


def manager_base_url(manager: SdwanManagerConfig) -> str:
    if manager.port == 443:
        return f"https://{manager.address}"
    return f"https://{manager.address}:{manager.port}"


def default_test_config(
    *, bearer_token: str | None = None, disable_rate_limit: bool = True
) -> AppConfig:
    """Minimal valid AppConfig for unit tests."""
    mcp: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8765,
        "stateless_http": True,
        "cors_origins": [],
        "disable_rate_limit": disable_rate_limit,
    }
    if bearer_token is not None:
        mcp["bearer_token"] = bearer_token
    return AppConfig.model_validate(
        {
            "sdwan_managers": [
                {
                    "name": "primary",
                    "address": "127.0.0.1",
                    "user": "test",
                    "password": "testpass",
                }
            ],
            "mcp": mcp,
        }
    )
