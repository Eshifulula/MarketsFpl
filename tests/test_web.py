import uuid

import pytest
from conftest import PIN, make_proposal
from starlette.testclient import TestClient

from fpl_server import web
from fpl_server.approvals import APPROVED, PENDING
from fpl_server.state import store


@pytest.fixture
def client(settings, proposals):
    app = web.create_app(settings, proposals, web.LoginThrottle(max_attempts=3))
    return TestClient(app, base_url="http://127.0.0.1:8020")


def test_wrong_host_refused_and_docs_disabled(client, settings):
    evil = TestClient(web.create_app(settings), base_url="http://evil.example")
    assert evil.get("/").status_code == 421
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/healthz").text == "ok"


def test_login_page_requires_a_real_pending_request(client):
    assert client.get("/login/not-a-uuid").status_code == 404
    rid = str(uuid.uuid4())
    assert client.get(f"/login/{rid}").status_code == 404  # visiting alone creates nothing
    store.create_login_request(rid)
    assert client.get(f"/login/{rid}").status_code == 200


class _FailingAuth:
    failure_reason = "The Premier League rejected the email or password."

    def __init__(self, email, password):
        pass

    async def login_and_get_token(self):
        return None


def test_login_failure_never_echoes_credentials_and_is_throttled(client, monkeypatch):
    monkeypatch.setattr(web, "FPLAutomation", _FailingAuth)
    rid = str(uuid.uuid4())
    store.create_login_request(rid)
    resp = client.post(f"/auth/submit/{rid}", data={"email": "a@b.co", "password": "hunter2hunter2"})
    assert resp.status_code == 401
    assert "hunter2hunter2" not in resp.text and "a@b.co" not in resp.text
    # the request is now failed => cannot be reused for further guessing
    again = client.post(f"/auth/submit/{rid}", data={"email": "a@b.co", "password": "x"})
    assert again.status_code == 404
    codes = []
    for _ in range(4):
        r = str(uuid.uuid4())
        store.create_login_request(r)
        codes.append(client.post(f"/auth/submit/{r}", data={"email": "a@b.co", "password": "x"}).status_code)
    assert 429 in codes


def test_unexpected_login_error_is_generic(client, monkeypatch):
    class Boom:
        def __init__(self, email, password):
            raise RuntimeError("secret-internal-detail password=hunter2")

    monkeypatch.setattr(web, "FPLAutomation", Boom)
    rid = str(uuid.uuid4())
    store.create_login_request(rid)
    resp = client.post(f"/auth/submit/{rid}", data={"email": "a@b.co", "password": "hunter2"})
    assert resp.status_code == 500
    assert "secret-internal-detail" not in resp.text and "hunter2" not in resp.text


def test_approval_page_escapes_html(client, proposals, settings):
    p = make_proposal(proposals, settings, summary=["<script>alert(1)</script>"], warnings=["<b>x</b>"])
    body = client.get(f"/approve/{p.id}").text
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body
    assert "&lt;b&gt;x&lt;/b&gt;" in body


def test_approve_flow_wrong_pin_then_right_pin(client, proposals, settings):
    p = make_proposal(proposals, settings)
    bad = client.post(f"/approve/{p.id}", data={"action": "approve", "nonce": p.nonce, "pin": "nope-nope-nope"})
    assert bad.status_code == 403 and proposals.get(p.id).status == PENDING
    forged = client.post(f"/approve/{p.id}", data={"action": "approve", "nonce": "x", "pin": PIN})
    assert forged.status_code == 403 and proposals.get(p.id).status == PENDING
    ok = client.post(f"/approve/{p.id}", data={"action": "approve", "nonce": p.nonce, "pin": PIN})
    assert ok.status_code == 200 and proposals.get(p.id).status == APPROVED


def test_reject_and_unknown(client, proposals, settings):
    p = make_proposal(proposals, settings)
    assert client.post(f"/approve/{p.id}", data={"action": "reject", "nonce": p.nonce}).status_code == 200
    assert proposals.get(p.id).status == "rejected"
    assert client.get("/approve/does-not-exist-123").status_code == 404
    assert client.post("/approve/bad", data={"action": "approve", "nonce": "x"}).status_code == 404


def test_pin_never_rendered_in_pages(client, proposals, settings):
    p = make_proposal(proposals, settings)
    assert PIN not in client.get(f"/approve/{p.id}").text
    assert PIN not in client.get("/").text
