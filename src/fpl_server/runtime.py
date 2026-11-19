"""Process-wide singletons shared by the MCP tools (one event loop) and the local web app
(another event loop / thread). Everything here is thread-safe."""

from __future__ import annotations

import asyncio
import threading
import weakref

from .approvals import AuditLog, ProposalStore
from .config import get_settings

_lock = threading.Lock()
_proposals: ProposalStore | None = None


def get_proposals() -> ProposalStore:
    global _proposals
    with _lock:
        if _proposals is None:
            _proposals = ProposalStore(AuditLog(get_settings().audit_dir))
        return _proposals


def set_proposals(store: ProposalStore | None) -> None:
    """Test hook."""
    global _proposals
    with _lock:
        _proposals = store


_exec_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def execution_lock() -> asyncio.Lock:
    """ONE lock for everything that writes to FPL (approved proposals AND the emergency tool).

    Held from the fresh squad read through the POST, so two writers can never act on the same
    snapshot. Keyed by event loop (asyncio locks bind to the loop that first contends them).
    """
    loop = asyncio.get_running_loop()
    with _lock:
        lock = _exec_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _exec_locks[loop] = lock
        return lock
