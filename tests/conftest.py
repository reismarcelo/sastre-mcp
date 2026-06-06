"""Shared pytest fixtures."""

import pytest
from sastre_mcp.config import default_test_config, set_active_config


@pytest.fixture(autouse=True)
def active_test_config() -> None:
    """runner and other modules call get_config() at runtime."""
    set_active_config(default_test_config())
    yield
