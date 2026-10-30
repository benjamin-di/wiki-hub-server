"""Real OIDC signatures, fake endpoints and disposable repositories; no live accounts."""

import asyncio
import hashlib
import shutil
import time
from dataclasses import replace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from joserfc import jwk, jwt
from starlette.requests import Request
from starlette.testclient import TestClient

from wiki_hub.auth import create_verifier
from wiki_hub.forgejo import ForgejoError
from wiki_hub.server import create_app
from wiki_hub.state import AccessConflict, State
from wiki_hub.web_state import BrowserState


class FakeIdentity:
    def __init__(self, settings):
        self.settings = settings
        self.key = jwk.RSAKey.generate_key(2048, parameters={"kid": "browser-test"})
        self.nonce = ""
        self.subject = "owner"
        self.id_changes = {}
        self.access_changes = {}
        self.token_calls = []
        self.directory_calls = []
        self.directory_down = False
        self.groups = None
        self.users = {
            "owner": {"id": "owner", "displayName": "Alex Morgan", "username": "alex", "disabled": False,
                      "email": "alex@example.com", "userGroups": []},
            "reader": {"id": "reader", "displayName": "Sam Rivera", "username": "sam", "disabled": False,
                       "email": "sam@example.com", "userGroups": [{"id": "wiki-users"}]},
            "disabled": {"id": "disabled", "displayName": "Disabled person", "username": "disabled",
                         "disabled": True, "email": "disabled@example.com"},
        }

    def sign(self, claims, typ="JWT"):
        return jwt.encode({"alg": "RS256", "typ": typ, "kid": "browser-test"}, claims, self.key)

    def tokens(self):
        now = int(time.time())
        access = {"iss": self.settings.oidc_issuer, "aud": self.settings.oidc_audience, "sub": self.subject,
                  "client_id": self.settings.web_client_id, "scope": "openid wiki:access", "iat": now, "exp": now + 900}
        access.update(self.access_changes)
        identity = {"iss": self.settings.oidc_issuer, "aud": self.settings.web_client_id, "sub": self.subject,
                    "nonce": self.nonce, "iat": now, "exp": now + 900, "name": "Alex Morgan",
                    "email": "alex@example.com"}
        identity.update(self.id_changes)
        return {"access_token": self.sign(access, "at+jwt"), "id_token": self.sign(identity),
                "token_type": "Bearer", "expires_in": 900, "refresh_token": "synthetic-refresh-token"}

    def handle(self, request):
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.settings.oidc_issuer,
                "authorization_endpoint": self.settings.oidc_issuer + "/authorize",
                "token_endpoint": self.settings.oidc_issuer + "/token",
                "jwks_uri": self.settings.oidc_issuer + "/jwks",
            })
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if path == "/token":
            data = parse_qs(request.content.decode())
            self.token_calls.append(data)
            assert data["resource"] == [self.settings.oidc_audience]
            assert request.headers["authorization"].startswith("Basic ")
            if data["grant_type"] == ["authorization_code"]:
                assert len(data["code_verifier"][0]) >= 43
                assert data["redirect_uri"] == [self.settings.public_url + "/auth/callback"]
            return httpx.Response(200, json=self.tokens())
        if path.startswith("/api/"):
            assert request.headers["x-api-key"] == "synthetic-directory-key"
            self.directory_calls.append(path)
            if self.directory_down:
                return httpx.Response(503, text="synthetic-private-diagnostic")
            if path == "/api/oidc/clients/wiki-web":
                return httpx.Response(200, json={"isGroupRestricted": self.groups is not None,
                                               "allowedUserGroups": [{"id": group} for group in self.groups or []]})
            if path == "/api/users":
                return httpx.Response(200, json={"data": list(self.users.values())})
            identifier = path.removeprefix("/api/users/")
            if identifier in self.users:
                return httpx.Response(200, json=self.users[identifier])
            return httpx.Response(404, json={})
        raise AssertionError("Unexpected fake identity endpoint: " + path)


@pytest.fixture
def browser_app(settings, service):
    settings = replace(settings, web_client_id="wiki-web", web_client_secret="synthetic-web-client-secret",
                       web_session_key="synthetic-session-key-32-characters-minimum",
                       directory_url="https://id.example.test", directory_api_key="synthetic-directory-key")
    identity = FakeIdentity(settings)
    verifier = create_verifier(settings)
    verifier._fetch_jwks = AsyncMock(return_value={"keys": [identity.key.as_dict(private=False)]})
    app = create_app(settings, service, verifier, web_transport=httpx.MockTransport(identity.handle))
    return app, identity


def sign_in(client, app, identity, subject="owner", return_to="/"):
    identity.subject = subject
    login = client.get("/auth/login", params={"return_to": return_to}, follow_redirects=False)
    assert login.status_code == 303
    params = parse_qs(urlsplit(login.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["resource"] == [app.state.web.settings.oidc_audience]
    identity.nonce = params["nonce"][0]
    return client.get("/auth/callback", params={"code": "synthetic-code", "state": params["state"][0]},
                      follow_redirects=False)


def csrf_headers(client, app):
    web = app.state.web
    session = web.store.session(client.cookies[web.cookie], web.settings.oidc_issuer)
    return {"Origin": web.settings.public_url, "X-CSRF-Token": session["csrf"]}


def change(client, app, subject, role, **extra):
    snapshot = client.get("/ui/api/wikis/starter/access").json()
    return client.post("/ui/api/wikis/starter/access", headers=csrf_headers(client, app), json={
        "subject": subject, "role": role, "version": snapshot["version"],
        "operation_id": "browser-operation-" + str(time.time_ns()), **extra,
    })


def test_login_real_signatures_server_side_encryption_and_bearer_separation(browser_app):
    app, identity = browser_app
    with TestClient(app, base_url=identity.settings.public_url) as client:
        assert "Sign in with" in client.get("/").text
        response = sign_in(client, app, identity, return_to="/wikis/starter/access")
        assert response.status_code == 303 and response.headers["location"] == "/wikis/starter/access"
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Domain=" not in cookie
        home = client.get("/")
        assert home.status_code == 200 and "Alex Morgan" in home.text and "Create wiki" in home.text
        assert "synthetic-refresh-token" not in home.text
        assert client.get("/api/wikis").status_code == 401  # browser cookie never authorizes bearer routes
        with app.state.service.state.connect() as db:
            row = db.execute("SELECT * FROM browser_sessions").fetchone()
            assert "synthetic-refresh-token" not in row["payload"] and "access_token" not in row["payload"]
            assert client.cookies[app.state.web.cookie] not in row["id"]
        assert "frame-ancestors 'none'" in home.headers["content-security-policy"]
        assert home.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("changes", [
    {"nonce": "wrong-nonce"}, {"iss": "https://other.example.test"}, {"aud": "another-app"},
    {"sub": "another-person"}, {"exp": 1}, {"iat": True},
])
def test_login_rejects_wrong_identity_claims(browser_app, changes):
    app, identity = browser_app
    identity.id_changes = changes
    with TestClient(app, base_url=identity.settings.public_url) as client:
        assert sign_in(client, app, identity).status_code == 400
        assert app.state.web.cookie not in client.cookies


@pytest.mark.parametrize("changes", [{"client_id": "wiki-cli"}, {"scope": "openid"}, {"sub": "client-workload"}])
def test_login_rejects_wrong_access_contract(browser_app, changes):
    app, identity = browser_app
    identity.access_changes = changes
    with TestClient(app, base_url=identity.settings.public_url) as client:
        assert sign_in(client, app, identity).status_code == 400


def test_login_binding_replay_expiry_and_local_redirects(browser_app):
    app, identity = browser_app
    web = app.state.web
    with TestClient(app, base_url=identity.settings.public_url) as client:
        for return_to in ("https://other.example.test", "//other.example.test", "/wikis/%2f%2fother", "/wikis/a?x=1"):
            assert sign_in(client, app, identity, return_to=return_to).headers["location"] == "/"
        login = client.get("/auth/login", follow_redirects=False)
        params = parse_qs(urlsplit(login.headers["location"]).query)
        identity.nonce = params["nonce"][0]
        callback = "/auth/callback?code=synthetic-code&state=" + params["state"][0]
        binding = client.cookies[web.login_cookie]
        client.cookies.delete(web.login_cookie)
        assert client.get(callback).status_code == 400
        client.cookies.set(web.login_cookie, binding)
        assert client.get(callback, follow_redirects=False).status_code == 303
        assert client.get(callback).status_code == 400
        login = client.get("/auth/login", follow_redirects=False)
        params = parse_qs(urlsplit(login.headers["location"]).query)
        with web.store.state.connect() as db:
            db.execute("UPDATE browser_logins SET expires=0")
        expired = client.get("/auth/callback", params={"code": "synthetic-code", "state": params["state"][0]})
        assert expired.status_code == 400


def test_session_refresh_rotation_logout_and_csrf(browser_app):
    app, identity = browser_app
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        web = app.state.web
        old = client.cookies[web.cookie]
        session = web.store.session(old, identity.settings.oidc_issuer)
        session["token"]["expires_at"] = time.time() - 1
        web.store.update_session(old, session["token"], session["csrf"])
        assert client.get("/").status_code == 200
        assert identity.token_calls[-1]["grant_type"] == ["refresh_token"]
        sign_in(client, app, identity)
        assert client.cookies[web.cookie] != old and web.store.session(old, identity.settings.oidc_issuer) is None
        assert client.post("/auth/logout", json={}).status_code == 403
        assert client.post("/auth/logout", headers=csrf_headers(client, app), json={}).status_code == 200
        assert web.cookie not in client.cookies and "Sign in with" in client.get("/").text


def test_web_create_share_change_remove_and_activity(browser_app):
    app, identity = browser_app
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        created = client.post("/ui/api/wikis", headers=csrf_headers(client, app),
                              json={"slug": "starter", "title": "Team handbook"})
        assert created.status_code == 201
        assert "Team handbook" in client.get("/").text
        assert "Manage access" in client.get("/wikis/starter/access").text
        people = client.get("/ui/api/wikis/starter/people?q=sa").json()["people"]
        assert {person["subject"] for person in people} == {"owner", "reader"}
        assert all(set(person) == {"subject", "name", "email", "username"} for person in people)
        for role in ("read", "contribute", "write", "admin"):
            response = change(client, app, "reader", role)
            assert response.status_code == 200 and response.json()["operation"]["status"] == "completed"
            assert app.state.service.state.role("reader", "starter") == role
        response = change(client, app, "reader", "none")
        assert response.status_code == 200
        current = client.get("/ui/api/wikis/starter/access").json()
        assert current["grants"] == [] and len(current["activity"]) == 5
        assert all(event["actor"] == "owner" for event in current["activity"])


def test_backend_pending_receipt_replay_retry_restart_and_supersession(browser_app, monkeypatch):
    app, identity = browser_app
    service = app.state.service
    asyncio.run(service.create("owner", "starter", "Team handbook"))
    original = service.forgejo.set_collaborator
    calls = []

    def unavailable(*args):
        calls.append(args)
        raise ForgejoError("synthetic-private-diagnostic")

    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        monkeypatch.setattr(service.forgejo, "set_collaborator", unavailable)
        operation = "retained-access-operation"
        result = change(client, app, "reader", "write", operation_id=operation)
        assert result.status_code == 202 and result.json()["pending"]
        assert "synthetic-private" not in result.text
        assert State(service.settings.state_dir).access_operation(operation)["status"] == "pending"
        assert service.state.role("reader", "starter") is None
        assert change(client, app, "reader", "write", operation_id=operation).status_code == 200
        assert len(calls) == 1  # status/replay does not retry a backend mutation
        assert client.get("/ui/api/wikis/starter/access").json()["pending_operations"][0]["id"] == operation
        monkeypatch.setattr(service.forgejo, "set_collaborator", original)
        assert change(client, app, "reader", "write", operation_id=operation, retry=True).status_code == 200
        assert service.state.role("reader", "starter") == "write"
        monkeypatch.setattr(service.forgejo, "set_collaborator", unavailable)
        assert change(client, app, "reader", "none", operation_id="pending-removal-operation").status_code == 202
        monkeypatch.setattr(service.forgejo, "set_collaborator", original)
        change(client, app, "reader", "read")
        result = change(client, app, "reader", "none", operation_id="pending-removal-operation", retry=True)
        assert result.status_code == 409 and service.state.role("reader", "starter") == "read"


def test_current_grants_suspension_private_pages_and_self_demotion(browser_app):
    app, identity = browser_app
    service = app.state.service
    asyncio.run(service.create("owner", "starter", "Private title"))
    asyncio.run(service.share("owner", "starter", "reader", "read"))
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity, subject="reader")
        assert client.get("/wikis/starter").status_code == 200
        for path in ("/wikis/starter/access", "/ui/api/wikis/starter/access", "/ui/api/wikis/starter/people?q=sa"):
            response = client.get(path)
            assert response.status_code == 403 and "Private title" not in response.text
        assert client.post("/ui/api/wikis", headers=csrf_headers(client, app),
                           json={"slug": "new", "title": "New"}).status_code == 403
        asyncio.run(service.share("owner", "starter", "reader", "admin"))
        response = change(client, app, "reader", "read")
        assert response.status_code == 200 and set(response.json()) == {"operation"}
        assert client.get("/ui/api/wikis/starter/access").status_code == 403
        asyncio.run(service.share("owner", "starter", "reader", "none"))
        assert "Private title" not in client.get("/").text
        assert client.get("/wikis/starter").status_code == 403
        asyncio.run(service.user_status("owner", "reader", True))
        assert client.get("/").status_code == 403
        assert client.post("/auth/logout", headers=csrf_headers(client, app), json={}).status_code == 200


def test_directory_filters_admission_and_revalidates_selected_identity(browser_app):
    app, identity = browser_app
    asyncio.run(app.state.service.create("owner", "starter", "Starter"))
    identity.groups = {"wiki-users"}
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        rows = client.get("/ui/api/wikis/starter/people?q=sa").json()["people"]
        assert [person["subject"] for person in rows] == ["reader"]
        identity.users["reader"]["disabled"] = True
        assert change(client, app, "reader", "read").status_code == 400
        identity.users["reader"]["disabled"] = False
        assert change(client, app, "reader", "read").status_code == 200
        identity.directory_down = True
        result = client.get("/ui/api/wikis/starter/people?q=sa")
        assert result.status_code == 503 and "synthetic-private" not in result.text
        assert change(client, app, "reader", "none").status_code == 200
        assert client.get("/ui/api/wikis/starter/access").status_code == 200


def test_csrf_wrong_origin_stale_tabs_and_xss_escaping(browser_app):
    app, identity = browser_app
    service = app.state.service
    asyncio.run(service.create("owner", "starter", '<script>alert("title")</script>'))
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        assert "<script>alert" not in client.get("/").text
        assert "&lt;script&gt;" in client.get("/").text
        headers = csrf_headers(client, app)
        payload = {"subject": "reader", "role": "read", "version": 0, "operation_id": "stale-tab-operation"}
        for invalid in ({}, {**headers, "Origin": "https://other.example.test"},
                        {**headers, "X-CSRF-Token": "wrong"}):
            assert client.post("/ui/api/wikis/starter/access", headers=invalid, json=payload).status_code == 403
        asyncio.run(service.share("owner", "starter", "other", "read"))
        assert client.post("/ui/api/wikis/starter/access", headers=headers,
                           json=payload).status_code == 409
        assert service.state.role("reader", "starter") is None
        assert client.get("/ui/assets/unknown.js").status_code == 404
        for name in ("app.css", "app.js", "logo.svg"):
            assert client.get("/ui/assets/" + name).status_code == 200


def test_schema_rehearsal_on_copied_state_preserves_original(settings, tmp_path):
    state = State(settings.state_dir)
    state.add_wiki("starter", "Starter", "owner", "owner", 1)
    state.set_grant("starter", "reader", "write")
    state.projected("starter", "reader")
    with state.connect() as db:
        db.execute("DROP TABLE access_operations")
        db.execute("DROP TABLE access_versions")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(state.path.read_bytes()).hexdigest()
    copied = tmp_path / "rehearsal"
    copied.mkdir()
    shutil.copy2(state.path, copied / state.path.name)
    upgraded = State(copied)
    store = BrowserState(upgraded, "synthetic-copy-rehearsal-session-key")
    assert upgraded.role("reader", "starter") == "write"
    assert upgraded.wiki("starter")["owner"] == "owner"
    assert store.profile(settings.oidc_issuer, "reader")["name"] == "Unknown person"
    assert hashlib.sha256(state.path.read_bytes()).hexdigest() == before


async def test_grant_journal_lost_reply_current_access_and_reused_identifier(service):
    await service.create("owner", "starter", "Starter")
    version = service.state.access_version("starter")
    result = await service.share("owner", "starter", "reader", "write",
                                 operation_id="lost-reply-operation", expected_version=version)
    assert result["operation"]["status"] == "completed"
    await service.share("owner", "starter", "reader", "read")
    replay = await service.share("owner", "starter", "reader", "write", operation_id="lost-reply-operation")
    assert replay["operation"]["status"] == "completed" and service.state.role("reader", "starter") == "read"
    with pytest.raises(AccessConflict):
        await service.share("owner", "starter", "other", "read", operation_id="lost-reply-operation")


def test_signin_capacity_is_a_safe_retryable_error(browser_app):
    app, identity = browser_app
    with app.state.service.state.connect() as db:
        db.executemany("INSERT INTO browser_logins VALUES(?,?,?,?)",
                       [(str(i), "binding", "encrypted", time.time() + 600) for i in range(2000)])
    with TestClient(app, base_url=identity.settings.public_url) as client:
        response = client.get("/auth/login", follow_redirects=False)
        assert response.status_code == 503
        assert "temporarily unavailable" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert app.state.web.login_cookie not in client.cookies


def test_deleted_directory_recipient_is_rejected_without_saving_intent(browser_app):
    app, identity = browser_app
    service = app.state.service
    asyncio.run(service.create("owner", "starter", "Starter"))
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        assert client.get("/ui/api/wikis/starter/people?q=sa").status_code == 200
        del identity.users["reader"]
        response = change(client, app, "reader", "read")
        assert response.status_code == 400
        assert "active person" in response.json()["error"]
        assert service.state.grants("starter") == []
        assert service.state.access_activity("starter") == []


async def test_slow_session_refresh_does_not_block_another_browser(browser_app, monkeypatch):
    app, identity = browser_app
    web = app.state.web
    token = identity.tokens()
    token["expires_at"] = time.time() - 1
    slow_id, _ = web.store.create_session(identity.settings.oidc_issuer, "owner", token)
    fresh_token = identity.tokens()
    fresh_token["expires_at"] = time.time() + 900
    fast_id, _ = web.store.create_session(identity.settings.oidc_issuer, "owner", fresh_token)
    started, finish = asyncio.Event(), asyncio.Event()

    async def refresh(*_):
        started.set()
        await finish.wait()
        return fresh_token

    monkeypatch.setattr(web.identity, "refresh", refresh)
    def request(identifier):
        return Request({"type": "http", "headers": [(b"cookie", f"{web.cookie}={identifier}".encode())]})

    slow = asyncio.create_task(web.principal(request(slow_id)))
    try:
        await asyncio.wait_for(started.wait(), 2)
        fast = await asyncio.wait_for(web.principal(request(fast_id)), 2)
        assert fast["subject"] == "owner"
    finally:
        finish.set()
        await slow
    assert not web.refresh_locks  # Finished sessions do not accumulate lock entries.


def logout_token(identity, **changes):
    issued = int(time.time())
    claims = {"iss": identity.settings.oidc_issuer, "aud": identity.settings.web_client_id,
              "iat": issued, "exp": issued + 300, "jti": "synthetic-logout-event", "sub": "owner",
              "events": {"http://schemas.openid.net/event/backchannel-logout": {}}}
    claims.update(changes)
    return identity.sign({k: v for k, v in claims.items() if v is not None}, "logout+jwt")


def test_provider_logout_replay_new_login_and_inflight_login_fence(browser_app):
    app, identity = browser_app
    web = app.state.web
    with TestClient(app, base_url=identity.settings.public_url) as client:
        assert sign_in(client, app, identity).status_code == 303
        original = web.store.session(client.cookies[web.cookie], identity.settings.oidc_issuer)
        issued = original["token"]["_issued_at"]
        token = logout_token(identity, iat=issued)
        response = client.post("/auth/backchannel-logout", data={"logout_token": token})
        assert response.status_code == 200
        assert client.get("/ui/api/agents").status_code == 401
        with pytest.raises(ValueError, match="revoked"):
            web.store.create_session(identity.settings.oidc_issuer, "owner", original["token"])
        identity.id_changes["iat"] = issued + 1
        assert sign_in(client, app, identity).status_code == 303
        # Retries of one notification cannot sign out a newer browser login.
        assert client.post("/auth/backchannel-logout", data={"logout_token": token}).status_code == 200
        assert client.get("/ui/api/agents").status_code == 200


@pytest.mark.parametrize("changes", [
    {"iss": "https://other.example.test"}, {"aud": "wiki-cli"}, {"iat": True},
    {"iat": time.time() - 600}, {"nonce": "not-a-logout"}, {"events": {}},
    {"sub": "", "sid": ""}, {"exp": 1}, {"jti": []},
])
def test_provider_logout_rejects_invalid_tokens(browser_app, changes):
    app, identity = browser_app
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        response = client.post("/auth/backchannel-logout", data={"logout_token": logout_token(identity, **changes)})
        assert response.status_code == 400
        assert client.get("/ui/api/agents").status_code == 200


def test_provider_session_logout_does_not_end_another_session(browser_app):
    app, identity = browser_app
    web = app.state.web
    token = {**identity.tokens(), "_sid": "second-provider-session", "_issued_at": int(time.time())}
    second, _ = web.store.create_session(identity.settings.oidc_issuer, "owner", token)
    identity.id_changes["sid"] = "first-provider-session"
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        response = client.post("/auth/backchannel-logout", data={
            "logout_token": logout_token(identity, sub=None, sid="first-provider-session")})
        assert response.status_code == 200
        assert client.get("/ui/api/agents").status_code == 401
        assert web.store.session(second, identity.settings.oidc_issuer)


def test_web_settings_agents_accounts_and_current_authority(browser_app, monkeypatch):
    app, identity = browser_app
    service = app.state.service
    asyncio.run(service.create("owner", "starter", "Starter"))
    asyncio.run(service.share("owner", "starter", "reader", "admin"))
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        for page in ("/wikis/starter/settings", "/connections", "/agents", "/administration"):
            assert client.get(page).status_code == 200
        headers = csrf_headers(client, app)
        registered = client.post("/ui/api/agents", headers=headers, json={"name": "Synthetic agent"})
        assert registered.status_code == 200
        agent = registered.json()
        assert agent["credential"].startswith("whagent_")
        assert agent["credential"] not in client.get("/agents").text
        assert "Synthetic agent" in client.get("/wikis/starter/access").text
        assert change(client, app, agent["id"], "contribute").status_code == 200
        assert client.post(f"/ui/api/agents/{agent['id']}/revoke", headers=headers, json={}).status_code == 200
        assert service.state.token_agent(agent["credential"]) is None
        assert client.post("/ui/api/wikis/starter/archive", headers=headers,
                           json={"archived": True}).status_code == 200
        assert "archived" in client.get("/wikis/starter/settings").text
        assert client.post("/ui/api/wikis/starter/archive", headers=headers,
                           json={"archived": False}).status_code == 200
        original = service.forgejo.revoke_copies
        monkeypatch.setattr(service.forgejo, "revoke_copies", lambda *_: (_ for _ in ()).throw(ForgejoError("private")))
        response = client.post("/ui/api/wikis/starter/settings", headers=headers, json={"local_copies": False})
        assert response.status_code == 202 and response.json()["pending"]
        assert "Copy settings are pending" in client.get("/wikis/starter/settings").text
        monkeypatch.setattr(service.forgejo, "revoke_copies", original)
        assert client.post("/ui/api/wikis/starter/settings", headers=headers,
                           json={"local_copies": False}).status_code == 200
        sign_in(client, app, identity, subject="reader")
        headers = csrf_headers(client, app)
        assert client.get("/administration").status_code == 403
        assert client.post("/ui/api/accounts/status", headers=headers,
                           json={"subject": "owner", "suspended": True}).status_code == 403
        assert client.post("/ui/api/agents", headers=headers, json={"name": "Denied"}).status_code == 403
        assert client.get("/connections?subject=owner").status_code == 403
        assert client.post("/ui/api/wikis/starter/settings", headers=headers,
                           json={"local_copies": True}).status_code == 200
        asyncio.run(service.share("owner", "starter", "reader", "read"))
        assert client.post("/ui/api/wikis/starter/settings", headers=headers,
                           json={"local_copies": False}).status_code == 403
        assert client.get("/wikis/starter/settings").status_code == 403
        response = client.get("/ui/api/session?page=/wikis/starter/settings")
        assert response.status_code == 403 and response.json()["access_lost"]


def test_explicit_profile_refresh_preserves_grants_and_directory_failure(browser_app):
    app, identity = browser_app
    service = app.state.service
    asyncio.run(service.create("owner", "starter", "Starter"))
    asyncio.run(service.share("owner", "starter", "reader", "read"))
    with TestClient(app, base_url=identity.settings.public_url) as client:
        sign_in(client, app, identity)
        identity.users["reader"]["displayName"] = "Sam Updated"
        identity.users["reader"]["disabled"] = True
        response = client.post("/ui/api/wikis/starter/profiles", headers=csrf_headers(client, app), json={})
        assert response.status_code == 200
        current = client.get("/ui/api/wikis/starter/access").json()
        assert current["grants"][0]["profile"]["name"] == "Sam Updated"
        assert service.state.role("reader", "starter") == "read"  # Display refresh never changes grants.
        identity.directory_down = True
        response = client.post("/ui/api/wikis/starter/profiles", headers=csrf_headers(client, app), json={})
        assert response.status_code == 202 and response.json()["unavailable"] == 2
        assert "Sam Updated" in client.get("/wikis/starter/access").text
        assert change(client, app, "reader", "none").status_code == 200
