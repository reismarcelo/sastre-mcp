"""CLI entry: run streamable HTTP MCP server (uvicorn)."""

import logging
import logging.config
import sys
from pathlib import Path
from typing import Any

import uvicorn

from sastre_mcp.config import load_config, set_active_config
from sastre_mcp.server import build_http_app

BASE_LOGGING_CONFIG: dict[str, Any] = {
    # Console-only logging config; the file handler is added when writable.
    "version": 1,
    # Keep loggers created before configuration (e.g. during import) working.
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "%(levelname)s: %(message)s",
        },
        "detailed": {
            "format": "%(asctime)s: %(name)s: %(levelname)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "WARNING",
            "formatter": "simple",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "DEBUG",
    },
    "loggers": {
        "chardet.charsetprober": {
            "level": "INFO",
        },
    },
}


def configure_logging(log_path: Path) -> None:
    """Configure logging from a static, in-code config with a console-only fallback.

    The console and file handler levels and the log file path are fixed in code;
    the environment is not consulted, so logging behaves identically across
    deployments.

    If the log file's directory cannot be created or the file cannot be opened
    for writing (e.g. a ``--read-only`` container or a non-writable working
    directory), file logging is skipped and the server logs to the console only
    instead of crashing on startup.
    """
    file_error: str | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Confirm the file is actually writable before wiring up the handler,
        # so an unwritable path degrades to console logging instead of an
        # exception when the first record is emitted.
        with log_path.open("a", encoding="utf-8"):
            pass
    except OSError as ex:
        file_error = f"File logging disabled: cannot write log file '{log_path}' ({ex}); logging to console only."
    else:
        BASE_LOGGING_CONFIG["handlers"]["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_path),
            "backupCount": 3,
            "maxBytes": 204800,
            "level": "DEBUG",
            "formatter": "detailed",
        }
        BASE_LOGGING_CONFIG["root"]["handlers"] = ["console", "file"]

    logging.config.dictConfig(BASE_LOGGING_CONFIG)
    if file_error:
        logging.getLogger(__name__).warning(file_error)


def main() -> None:
    configure_logging(Path("logs/sastre-mcp.log").resolve())
    cfg = load_config((Path.cwd() / "config.yaml").resolve())
    set_active_config(cfg)

    if not cfg.mcp.bearer_token:
        logging.getLogger(__name__).warning(
            "mcp.bearer_token unset — use only on trusted localhost or set a token in config."
        )

    app = build_http_app(cfg)
    # build_http_app() may have rebuilt middleware_stack=None on first request; uvicorn handles it
    uvicorn.run(
        app,
        host=cfg.mcp.host,
        port=cfg.mcp.port,
        # log_config=None keeps uvicorn from replacing the dictConfig applied in configure_logging();
        # uvicorn's loggers then propagate to our root handlers.
        log_config=None,
        # HTTP (TLS) should be terminated by a reverse proxy in production; uvicorn can use ssl_* if needed.
    )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
