"""Structural guarantees: which code may write to FPL, and which tools exist."""

import asyncio
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "fpl_server"

WRITE_CAPABLE_TOOLS = {
    "propose_transfers",
    "propose_captain",
    "propose_lineup",
    "propose_chip",
    "cancel_proposal",
    "execute_approved_proposal",  # needs a human-approved proposal
    "emergency_lineup_swap",  # the single, narrowly-scoped exception
}
HINTS = ("propose", "execute", "emergency", "approve", "make_", "apply", "submit", "activate", "captain")


def test_only_the_two_gated_modules_may_send_writes():
    pattern = re.compile(r"\.post_(transfers|my_team)\(")
    callers = {f.name for f in SRC.glob("*.py") if pattern.search(f.read_text(encoding="utf-8"))}
    assert callers == {"mcp_write_tools.py", "mcp_emergency_tools.py"}


def test_no_other_module_posts_through_the_raw_client():
    # client.py defines the transport; auth.py only *observes* the login page's own token response.
    for f in SRC.glob("*.py"):
        if f.name in {"client.py", "auth.py"}:
            continue
        text = f.read_text(encoding="utf-8")
        assert '"POST"' not in text and "'POST'" not in text, f.name


def test_write_capable_tool_set_is_pinned():
    from fpl_server import (  # noqa: F401  (importing registers the tools)
        mcp_emergency_tools,
        mcp_prompts,
        mcp_resources,
        mcp_write_tools,
    )
    from fpl_server.mcp_tools import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    suspicious = {n for n in names if any(h in n for h in HINTS)}
    unexpected = suspicious - WRITE_CAPABLE_TOOLS - {"get_proposal", "list_proposals", "get_write_policy"}
    assert not unexpected, f"new tool(s) look write-capable and are not reviewed: {unexpected}"
    assert WRITE_CAPABLE_TOOLS <= names
    assert "make_transfers" not in names
    assert not [n for n in names if n.startswith("approve")], "there must be no approve tool"
