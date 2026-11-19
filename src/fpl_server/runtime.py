"""Process-wide singletons shared by the MCP tools (one event loop) and the local web app
(another event loop / thread). Everything here is thread-safe."""

from __future__ import annotations

import threading

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
