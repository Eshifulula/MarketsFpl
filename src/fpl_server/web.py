"""Local web app: FPL login page and the human approval page.

Bound to loopback by default. Credentials and the approval PIN are only ever typed into the
forms served here - they never pass through the LLM or an MCP tool.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from html import escape

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, PlainTextResponse

from .approvals import PENDING, ApprovalError, ProposalStore
from .auth import FPLAutomation
from .client import FPLClient
from .config import Settings, get_settings
from .runtime import get_proposals
from .security import WebHardeningMiddleware
from .state import store

logger = logging.getLogger("fpl_web")

_PROPOSAL_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class LoginThrottle:
    """Global cap on login attempts - protects the FPL account from lockout / abuse."""

    def __init__(self, max_attempts: int = 5, window: int = 900):
        self._max = max_attempts
        self._window = window
        self._stamps: list[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.time()
        with self._lock:
            self._stamps = [t for t in self._stamps if now - t < self._window]
            if len(self._stamps) >= self._max:
                return False
            self._stamps.append(now)
            return True


_STYLE = """
:root{color-scheme:light dark;--bg:#f4f5f2;--card:#fff;--fg:#17211b;--mute:#5d675f;--line:#d9ddd5;--ok:#1f6b3a;--bad:#9b2f26;--warn:#8a5a00}
@media(prefers-color-scheme:dark){:root{--bg:#101512;--card:#18201b;--fg:#eef2ec;--mute:#9aa59c;--line:#2b362e;--ok:#5fc07f;--bad:#ff8a80;--warn:#f0b955}}
*{box-sizing:border-box}body{margin:0;min-height:100dvh;display:grid;place-items:center;padding:20px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{width:min(100%,520px);background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px}
h1{margin:0 0 6px;font-size:22px}p{color:var(--mute);margin:6px 0 14px}
label{display:block;margin-top:12px;font-size:13px;color:var(--mute)}
input{width:100%;padding:11px 12px;margin-top:4px;border:1px solid var(--line);border-radius:10px;background:transparent;color:var(--fg);font:inherit}
button{margin-top:16px;padding:11px 18px;border:0;border-radius:999px;font:inherit;font-weight:600;cursor:pointer}
.go{background:var(--fg);color:var(--bg)}.no{background:transparent;color:var(--bad);border:1px solid var(--bad);margin-left:8px}
ul{padding-left:20px;margin:10px 0}li{margin:4px 0}.warn{color:var(--warn)}.ok{color:var(--ok)}.bad{color:var(--bad)}
code{font-family:ui-monospace,monospace;font-size:13px}.tag{font:600 11px ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--mute)}
"""


def _page(title: str, body: str) -> HTMLResponse:
    html = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>{escape(title)}</title>'
        f"<style>{_STYLE}</style></head><body><main>{body}</main></body></html>"
    )
    return HTMLResponse(html)


def _message(title: str, message: str, *, ok: bool, status: int = 200) -> HTMLResponse:
    cls = "ok" if ok else "bad"
    response = _page(
        title,
        f'<span class="tag">Markets FPL</span><h1 class="{cls}">{escape(title)}</h1>'
        f"<p>{escape(message)}</p>",
    )
    response.status_code = status
    return response


def create_app(
    settings: Settings | None = None,
    proposals: ProposalStore | None = None,
    throttle: LoginThrottle | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    throttle = throttle or LoginThrottle()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(WebHardeningMiddleware, allowed_hosts=settings.web_allowed_hosts)

    def _proposals() -> ProposalStore:
        return proposals or get_proposals()

    @app.get("/", response_class=HTMLResponse)
    async def home():
        pending = [p for p in _proposals().list() if p.status == PENDING]
        links = "".join(
            f'<li><a href="/approve/{escape(p.id)}">{escape(p.kind)} - GW{p.event}</a></li>'
            for p in pending
        )
        body = (
            '<span class="tag">Markets FPL</span><h1>Local control panel</h1>'
            "<p>Login and approval pages are served here. Nothing is shown unless a request "
            "was started by your assistant.</p>"
            + (f"<h3>Awaiting your approval</h3><ul>{links}</ul>" if links else "")
        )
        return _page("Markets FPL", body)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz():
        return "ok"

    # ---------------------------------------------------------------- login ------------
    @app.get("/login/{request_id}", response_class=HTMLResponse)
    async def login_page(request_id: str):
        if not _valid_uuid(request_id) or store.get_pending_login(request_id) is None:
            return _message("Link expired", "Ask your assistant for a new login link.", ok=False, status=404)
        body = (
            '<span class="tag">Markets FPL - local only</span><h1>Connect your FPL account</h1>'
            "<p>Your email and password go to this local process only. They are used once to "
            "obtain a session token and are never stored or shown to the assistant.</p>"
            f'<form action="/auth/submit/{escape(request_id)}" method="post" '
            "onsubmit=\"this.querySelector('button').disabled=true;this.querySelector('button').innerText='Signing in...'\">"
            '<label>Email<input type="email" name="email" autocomplete="username" required></label>'
            '<label>Password<input type="password" name="password" autocomplete="current-password" required></label>'
            '<button class="go" type="submit">Connect</button></form>'
        )
        return _page("Connect FPL", body)

    @app.post("/auth/submit/{request_id}")
    async def submit_login(request_id: str, email: str = Form(...), password: str = Form(...)):
        if not _valid_uuid(request_id):
            return _message("Link expired", "Ask your assistant for a new login link.", ok=False, status=404)
        pending = store.get_pending_login(request_id)
        if pending is None or pending.status != "pending":
            return _message("Link expired", "Ask your assistant for a new login link.", ok=False, status=404)
        if not throttle.allow():
            return _message(
                "Slow down",
                "Too many login attempts. Wait 15 minutes to protect your FPL account.",
                ok=False,
                status=429,
            )
        try:
            auth = FPLAutomation(email, password)
            token = await auth.login_and_get_token()
            reason = auth.failure_reason
            del auth, email, password
            if token:
                client = FPLClient(store=store)
                client.set_api_token(token)
                await store.set_login_success(request_id, str(uuid.uuid4()), client)
                return _message(
                    "Connected", "You can close this tab and tell your assistant you are done.", ok=True
                )
            failure = reason or "Could not obtain an authenticated FPL session."
            store.set_login_failure(request_id, failure)
            return _message("Connection failed", failure, ok=False, status=401)
        except Exception:  # noqa: BLE001 - never echo raw exception text (could include secrets)
            logger.exception("login failed unexpectedly")
            store.set_login_failure(request_id, "unexpected_error")
            return _message("Connection error", "Unexpected error. Check the server log.", ok=False, status=500)

    # ------------------------------------------------------------- approvals -----------
    @app.get("/approve/{proposal_id}", response_class=HTMLResponse)
    async def approve_page(proposal_id: str):
        proposal = _proposals().get(proposal_id) if _PROPOSAL_ID.match(proposal_id) else None
        if proposal is None:
            return _message("Not found", "Unknown or expired proposal.", ok=False, status=404)
        summary = "".join(f"<li>{escape(line)}</li>" for line in proposal.summary)
        warns = "".join(f'<li class="warn">{escape(w)}</li>' for w in proposal.warnings)
        mode = (
            '<p class="ok">Live writes are ENABLED.</p>'
            if settings.allow_writes
            else '<p class="warn">Dry-run mode: approving will NOT change your team.</p>'
        )
        form = ""
        if proposal.status == PENDING:
            form = (
                f'<form method="post" action="/approve/{escape(proposal.id)}">'
                f'<input type="hidden" name="nonce" value="{escape(proposal.nonce)}">'
                '<label>Approval PIN<input type="password" name="pin" autocomplete="off"></label>'
                '<button class="go" name="action" value="approve" type="submit">Approve</button>'
                '<button class="no" name="action" value="reject" type="submit">Reject</button></form>'
            )
        body = (
            f'<span class="tag">Approval - {escape(proposal.kind)} - GW{proposal.event}</span>'
            f"<h1>Review this change</h1>{mode}<ul>{summary}</ul>"
            + (f"<h3>Warnings</h3><ul>{warns}</ul>" if warns else "")
            + f'<p>Status: <code>{escape(proposal.status)}</code></p>{form}'
        )
        return _page("Approve change", body)

    @app.post("/approve/{proposal_id}")
    async def approve_submit(
        proposal_id: str,
        action: str = Form(...),
        nonce: str = Form(...),
        pin: str = Form(""),
    ):
        if not _PROPOSAL_ID.match(proposal_id):
            return _message("Not found", "Unknown or expired proposal.", ok=False, status=404)
        try:
            if action == "reject":
                _proposals().reject(proposal_id, nonce)
                return _message("Rejected", "Nothing was changed.", ok=True)
            if action != "approve":
                return _message("Bad request", "Unknown action.", ok=False, status=400)
            _proposals().approve(proposal_id, pin, nonce, settings)
            note = (
                "Tell your assistant to execute it."
                if settings.allow_writes
                else "Dry-run mode: executing will only record what would have been sent."
            )
            return _message("Approved", note, ok=True)
        except ApprovalError as exc:
            return _message("Not approved", str(exc), ok=False, status=403)

    return app


def _valid_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


app = create_app()
