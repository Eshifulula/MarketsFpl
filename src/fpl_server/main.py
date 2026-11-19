"""Entry point: local web app (login + approvals) on one port, MCP server on stdio or HTTP."""

from __future__ import annotations

import logging
import sys
import threading

import uvicorn

from .config import ConfigError, get_settings, validate_for_startup
from .security import BearerAuthMiddleware


def _configure_logging() -> None:
    # stderr only: with the stdio transport, stdout is the MCP protocol channel.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Never let HTTP client debug logging leak auth headers.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run_web_server(settings) -> None:
    from .web import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.web_host,
        port=settings.web_port,
        log_level="warning",
        access_log=False,
    )


def main() -> None:
    _configure_logging()
    log = logging.getLogger("fpl_main")
    settings = get_settings()
    try:
        warnings = validate_for_startup(settings)
    except ConfigError as exc:
        log.error("Refusing to start: %s", exc)
        sys.exit(2)
    for warning in warnings:
        log.warning(warning)

    # Importing registers every tool / resource / prompt on the shared FastMCP instance.
    from . import mcp_prompts, mcp_resources, mcp_write_tools  # noqa: F401
    from .mcp_tools import mcp

    threading.Thread(target=run_web_server, args=(settings,), daemon=True, name="web").start()
    log.info("Local web app on http://%s:%s", settings.web_host, settings.web_port)

    if settings.transport == "stdio":
        log.info("MCP server: stdio")
        mcp.run(transport="stdio")
        return

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token=settings.mcp_bearer_token)
    log.info("MCP server: streamable-http on http://%s:%s/mcp (bearer auth required)",
             settings.mcp_host, settings.mcp_port)
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
