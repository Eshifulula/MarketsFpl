"""Runtime configuration, read from environment variables.

Nothing in this module (or anywhere in the repo) holds a real secret. Secrets are only ever
supplied through the environment at runtime - see `.env.example`.

Safe-by-default: writes are OFF, approvals are impossible until a PIN is configured, and the
network listeners bind to loopback.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

MIN_PIN_LENGTH = 8
MIN_BEARER_LENGTH = 32

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(RuntimeError):
    """Raised for unsafe or malformed configuration. Never includes secret values."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false.")


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"{name} is out of range.")
    return value


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def is_loopback_host(host: str) -> bool:
    if host in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    # --- write policy -------------------------------------------------------------------
    allow_writes: bool
    """Master switch. False => `execute_approved_proposal` only records a dry run."""
    approval_pin: str | None
    """Operator PIN, entered ONLY on the local approval page. Never an MCP parameter."""
    proposal_ttl_seconds: int
    """How long a proposal may wait for human approval."""
    execute_window_seconds: int
    """How long an approved proposal may wait to be executed."""
    max_transfers: int
    """Max transfers in one proposal (not applied to wildcard / free hit)."""
    max_points_hit: int
    """Refuse proposals that would cost more than this many points."""
    pin_max_failures: int
    pin_lockout_seconds: int
    audit_dir: Path

    # --- network ------------------------------------------------------------------------
    web_host: str
    web_port: int
    mcp_host: str
    mcp_port: int
    transport: str
    mcp_bearer_token: str | None
    web_allowed_hosts: tuple[str, ...]
    mcp_allowed_hosts: tuple[str, ...]
    mcp_allowed_origins: tuple[str, ...]
    allow_nonloopback_bind: bool
    public_web_base_url: str
    """Base URL used when telling the user where the approval/login page lives."""


def get_settings() -> Settings:
    web_port = _env_int("FPL_AUTH_PORT", 8020, minimum=1, maximum=65535)
    mcp_port = _env_int("FPL_MCP_PORT", 8021, minimum=1, maximum=65535)
    pin = os.environ.get("FPL_APPROVAL_PIN") or None
    bearer = os.environ.get("FPL_MCP_BEARER_TOKEN") or None
    web_host = os.environ.get("FPL_WEB_HOST", "127.0.0.1")

    return Settings(
        allow_writes=_env_bool("FPL_ALLOW_WRITES", False),
        approval_pin=pin,
        proposal_ttl_seconds=_env_int("FPL_PROPOSAL_TTL_SECONDS", 900, minimum=30, maximum=86400),
        execute_window_seconds=_env_int(
            "FPL_EXECUTE_WINDOW_SECONDS", 600, minimum=30, maximum=3600
        ),
        max_transfers=_env_int("FPL_MAX_TRANSFERS_PER_PROPOSAL", 3, minimum=1, maximum=15),
        max_points_hit=_env_int("FPL_MAX_POINTS_HIT", 4, minimum=0, maximum=100),
        pin_max_failures=_env_int("FPL_PIN_MAX_FAILURES", 5, minimum=1, maximum=100),
        pin_lockout_seconds=_env_int("FPL_PIN_LOCKOUT_SECONDS", 900, minimum=30, maximum=86400),
        audit_dir=Path(os.environ.get("FPL_AUDIT_DIR", "audit")),
        web_host=web_host,
        web_port=web_port,
        mcp_host=os.environ.get("FPL_MCP_HOST", "127.0.0.1"),
        mcp_port=mcp_port,
        transport=os.environ.get("FPL_MCP_TRANSPORT", "stdio"),
        mcp_bearer_token=bearer,
        web_allowed_hosts=tuple(
            _env_list(
                "FPL_WEB_ALLOWED_HOSTS",
                [f"127.0.0.1:{web_port}", f"localhost:{web_port}"],
            )
        ),
        mcp_allowed_hosts=tuple(
            _env_list("FPL_MCP_ALLOWED_HOSTS", ["127.0.0.1:*", "localhost:*", "[::1]:*"])
        ),
        mcp_allowed_origins=tuple(
            _env_list("FPL_MCP_ALLOWED_ORIGINS", ["http://127.0.0.1:*", "http://localhost:*"])
        ),
        allow_nonloopback_bind=_env_bool("FPL_ALLOW_NONLOOPBACK_BIND", False),
        public_web_base_url=os.environ.get(
            "FPL_AUTH_BASE_URL", f"http://127.0.0.1:{web_port}"
        ).rstrip("/"),
    )


def validate_for_startup(settings: Settings) -> list[str]:
    """Fail closed on unsafe config. Returns human-readable warnings for acceptable-but-notable
    config. Raises ConfigError for anything unsafe."""
    warnings: list[str] = []

    if settings.transport not in {"stdio", "streamable-http"}:
        raise ConfigError("FPL_MCP_TRANSPORT must be 'stdio' or 'streamable-http'.")

    for label, host in (("FPL_WEB_HOST", settings.web_host), ("FPL_MCP_HOST", settings.mcp_host)):
        if not is_loopback_host(host) and not settings.allow_nonloopback_bind:
            raise ConfigError(
                f"{label} is not a loopback address. Refusing to bind. Set "
                "FPL_ALLOW_NONLOOPBACK_BIND=true only inside a container whose published "
                "ports are bound to 127.0.0.1 on the host."
            )

    if settings.transport == "streamable-http":
        token = settings.mcp_bearer_token
        if not token or len(token) < MIN_BEARER_LENGTH:
            raise ConfigError(
                f"FPL_MCP_BEARER_TOKEN (>= {MIN_BEARER_LENGTH} chars) is required for the "
                "HTTP transport."
            )

    if settings.approval_pin is not None and len(settings.approval_pin) < MIN_PIN_LENGTH:
        raise ConfigError(f"FPL_APPROVAL_PIN must be at least {MIN_PIN_LENGTH} characters.")

    if settings.allow_writes and settings.approval_pin is None:
        raise ConfigError("FPL_ALLOW_WRITES=true requires FPL_APPROVAL_PIN to be set.")

    if settings.approval_pin is None:
        warnings.append("No FPL_APPROVAL_PIN set: proposals can be created but never approved.")
    if not settings.allow_writes:
        warnings.append("FPL_ALLOW_WRITES is off: approved proposals are DRY RUN only.")
    return warnings
