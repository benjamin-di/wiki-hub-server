"""Application suspension is independent of a still-valid identity login."""

import asyncio
import base64
import json
import sqlite3
import threading
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client
from joserfc import jwk, jwt
from starlette.testclient import TestClient

from wiki_hub import cli
from wiki_hub.auth import create_verifier
from wiki_hub.forgejo import ForgejoError
from wiki_hub.gitstore import ContentError
from wiki_hub.mcp import create_mcp
from wiki_hub.server import create_app
from wiki_hub.service import Denied
from wiki_hub.state import State


async def test_suspension_covers_owned_and_shared_wikis_without_losing_grants(service, monkeypatch):
    await service.create("owner", "owned", "Owned")
    await service.create("owner", "shared", "Shared")
    with service.state.connect() as db:
        db.execute("UPDATE wikis SET owner='reader' WHERE slug='owned'")
    await service.share("owner", "shared", "reader", "write")
    await service.clone("reader", "owned")
    await service.clone("reader", "shared")
    revoked, members = [], []
    monkeypatch.setattr(service.forgejo, "revoke_copies", lambda *a: revoked.append(a))
    monkeypatch.setattr(service.forgejo, "set_collaborator", lambda *a: members.append(a))
    assert await service.user_status("owner", "reader", True) == {
        "subject": "reader", "suspended": True, "pending": False}
    assert {(a[1], a[2]) for a in revoked} == {("owned", "reader"), ("shared", "reader")}
    assert members == [("owner", "shared", "reader", "none")]
    assert service.state.role("reader", "shared") == "write"
    for wiki in ("owned", "shared"):
        with pytest.raises(Denied):
            await service.pages("reader", wiki)
        with pytest.raises(Denied):
            await service.clone("reader", wiki)
    # Neither a share refresh nor copy settings implicitly resume the account.
    await service.share("owner", "shared", "reader", "admin")
    await service.user_settings("owner", "reader", True)
    with pytest.raises(Denied):
        await service.list_wikis("reader")
    await service.wiki_settings("owner", "shared", False)
    await service.user_status("owner", "reader", False)
    assert await service.pages("reader", "owned")
    assert await service.pages("reader", "shared")
    assert members[-1][-1] == "none"  # Resume respects the wiki's copy policy.
    with pytest.raises(Denied):
        await service.clone("reader", "shared")
    assert await service.clone("reader", "owned")


async def test_failed_suspend_and_resume_stay_denied_until_explicit_retry(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "read")

    def unavailable(*_args):
        raise ForgejoError("Synthetic unavailable backend")

    monkeypatch.setattr(service.forgejo, "revoke_copies", unavailable)
    with pytest.raises(ForgejoError):
        await service.user_status("owner", "reader", True)
    assert State(service.settings.state_dir).user_status("reader") == {"suspended": True, "pending": True}
    monkeypatch.setattr(service.forgejo, "revoke_copies", lambda *_a: None)
    await service.user_status("owner", "reader", True)
    monkeypatch.setattr(service.forgejo, "set_collaborator", unavailable)
    with pytest.raises(ForgejoError):
        await service.user_status("owner", "reader", False)
    assert State(service.settings.state_dir).user_status("reader") == {"suspended": False, "pending": True}
    with pytest.raises(Denied):
        await service.read_page("reader", "starter", "wiki/index.md")
    assert not service.state.copies_allowed("reader", "starter")
    monkeypatch.setattr(service.forgejo, "set_collaborator", lambda *_a: None)
    assert not (await service.user_status("owner", "reader", False))["pending"]
    assert await service.pages("reader", "starter")


async def test_suspension_waits_for_clone_and_revokes_the_issued_credential(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "write")
    entered, release = threading.Event(), threading.Event()
    events = []
    issue = service.forgejo.clone_credential

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("Test did not release clone")
        events.append("issued")
        return issue(*args, **kwargs)

    monkeypatch.setattr(service.forgejo, "clone_credential", blocked)
    monkeypatch.setattr(service.forgejo, "revoke_copies", lambda *_a: events.append("revoked"))
    clone = asyncio.create_task(service.clone("reader", "starter"))
    suspend = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        suspend = asyncio.create_task(service.user_status("owner", "reader", True))
        await asyncio.sleep(0)
        assert not suspend.done()
    finally:
        release.set()
        await clone
        if suspend:
            await suspend
    assert events == ["issued", "revoked"]
    with pytest.raises(Denied):
        await service.clone("reader", "starter")


async def test_existing_mcp_sessions_and_owner_capped_agents_lose_access(service):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "contribute")
    agent = await service.add_agent("owner", "Delegated reader")
    with service.state.connect() as db:
        db.execute("UPDATE agents SET owner='reader' WHERE id=?", (agent["id"],))
    await service.share("owner", "starter", agent["id"], "read")
    async with Client(create_mcp(service, subject_provider=lambda: "reader")) as human:
        async with Client(create_mcp(service, subject_provider=lambda: agent["id"])) as machine:
            for client in (human, machine):
                assert len((await client.call_tool("wiki_list")).data) == 1
            await service.user_status("owner", "reader", True)
            for client in (human, machine):
                for tool, args in (("wiki_list", {}), ("wiki_read", {
                    "wiki": "starter", "path": "wiki/index.md"})):
                    assert (await client.call_tool(tool, args, raise_on_error=False)).is_error
            await service.user_status("owner", "reader", False)
            assert len((await machine.call_tool("wiki_list")).data) == 1
            await service.revoke_agent("reader", agent["id"])
            await service.user_status("owner", "reader", True)
            await service.user_status("owner", "reader", False)
            assert (await machine.call_tool("wiki_list")).data == []
            assert service.state.token_agent(agent["credential"]) is None


@pytest.mark.parametrize("as_agent", [False, True])
async def test_suspension_during_model_run_prevents_publication(service, as_agent):
    await service.create("owner", "starter", "Starter")
    before = await service.history("owner", "starter")
    await service.share("owner", "starter", "reader", "contribute")
    subject = "reader"
    if as_agent:
        agent = await service.add_agent("owner", "Delegated filer")
        with service.state.connect() as db:
            db.execute("UPDATE agents SET owner='reader' WHERE id=?", (agent["id"],))
        await service.share("owner", "starter", agent["id"], "contribute")
        subject = agent["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    runner = service.runner

    async def blocked(*args):
        entered.set()
        await release.wait()
        await runner(*args)

    service.runner = blocked
    job = await service.ingest(subject, "starter", "note.txt", base64.b64encode(b"Source").decode())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await service.user_status("owner", "reader", True)
    finally:
        release.set()
        await asyncio.gather(*service.tasks)
    assert (await service.job("owner", job["id"]))["status"] == "failed"
    assert await service.history("owner", "starter") == before


async def test_suspension_waits_for_already_authorized_publication(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "contribute")
    entered, release = threading.Event(), threading.Event()
    publish = service.git.publish

    def blocked(*args):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("Test did not release publication")
        return publish(*args)

    monkeypatch.setattr(service.git, "publish", blocked)
    job = await service.ingest("reader", "starter", "note.txt", base64.b64encode(b"Source").decode())
    change = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        change = asyncio.create_task(service.user_status("owner", "reader", True))
        await asyncio.sleep(0)
        assert service.state.user_status("reader")["suspended"]
        assert not change.done()
    finally:
        release.set()
        if change:
            await change
        await asyncio.gather(*service.tasks)
    assert (await service.job("owner", job["id"]))["status"] == "completed"


def test_existing_signed_token_loses_all_api_access_without_identity_relogin(settings, service):
    signing_key = jwk.RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    verifier = create_verifier(settings)
    verifier._fetch_jwks = AsyncMock(return_value={"keys": [signing_key.as_dict(private=False)]})

    def bearer(subject):
        stamp = int(time.time())
        token = jwt.encode({"alg": "RS256", "typ": "at+jwt", "kid": "test-key"}, {
            "iss": settings.oidc_issuer, "aud": settings.oidc_audience, "sub": subject,
            "client_id": "test-cli", "scope": "wiki:access", "iat": stamp, "exp": stamp + 900,
        }, signing_key)
        return {"Authorization": "Bearer " + token}

    owner, reader = bearer("owner"), bearer("reader")
    status = "/api/users/status"
    with TestClient(create_app(settings, service, verifier)) as client:
        client.post("/api/wikis", headers=owner, json={"slug": "starter", "title": "Starter"}).raise_for_status()
        client.post("/api/wikis/starter/share", headers=owner,
                    json={"subject": "reader", "role": "admin"}).raise_for_status()
        assert client.get("/api/me", headers=reader).status_code == 200
        assert client.get(status, headers=reader, params={"subject": "owner"}).status_code == 403
        assert client.post(status, headers=owner, json={"subject": "owner", "suspended": True}).status_code == 400
        for invalid in (None, "false", 0, []):
            response = client.post(status, headers=owner, json={"subject": "reader", "suspended": invalid})
            assert response.status_code == 400
        change = client.post(status, headers=owner, json={"subject": "reader", "suspended": True})
        assert change.status_code == 200
        assert change.headers["Cache-Control"] == "no-store"
        for path in ("me", "wikis", "agents", "wikis/starter/pages", "wikis/starter/copy-access",
                     "wikis/starter/document?path=raw/source.pdf"):
            assert client.get("/api/" + path, headers=reader).status_code == 403
        for path, body in (("wikis", {"slug": "new", "title": "New"}), ("agents", {"name": "New"}),
                           ("users/settings", {"subject": "reader", "local_copies": True}),
                           ("users/status", {"subject": "reader", "suspended": False})):
            assert client.post("/api/" + path, headers=reader, json=body).status_code == 403
        client.post(status, headers=owner, json={"subject": "reader", "suspended": False}).raise_for_status()
        assert client.get("/api/me", headers=reader).status_code == 200  # Same signed token, no new login.
        # Owner's implicit authority must not bypass the local status guard either.
        service.state.set_user_status("owner", True)
        assert client.get("/api/me", headers=owner).status_code == 403
        assert client.post("/api/wikis", headers=owner, json={"slug": "new", "title": "New"}).status_code == 403


def test_upgrade_on_copied_catalog_preserves_existing_records(service, tmp_path):
    state = service.state
    state.add_wiki("starter", "Starter", "owner", "backend", 1)
    state.ready("starter")
    state.set_grant("starter", "reader", "write")
    state.projected("starter", "reader")
    state.add_agent("agent:fixture", "reader", "Fixture", "synthetic-key")
    state.add_job("fixture-job", "starter", "reader", "note.txt", "/synthetic/source", "")
    state.set_copy_setting("wiki", "starter", False)
    with state.connect() as db:
        db.execute("DROP TABLE user_status")  # Released catalog has no suspension table.
        previous = {name: [tuple(row) for row in db.execute("SELECT * FROM " + name)]
                    for name in ("wikis", "grants", "agents", "jobs", "copy_settings")}
        copied = tmp_path / "copy"
        copied.mkdir()
        with sqlite3.connect(copied / "wiki-hub.db") as target:
            db.backup(target)
    upgraded = State(copied)
    with upgraded.connect() as db:
        for name, rows in previous.items():
            assert [tuple(row) for row in db.execute("SELECT * FROM " + name)] == rows
    assert upgraded.user_status("reader") == {"suspended": False, "pending": False}
    upgraded.set_user_status("reader", True)
    assert State(copied).user_status("reader") == {"suspended": True, "pending": True}
    with state.connect() as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='user_status'").fetchone()[0] == 0


@pytest.mark.parametrize("recipient", ["", "agent:fixture", "two words", "reader\n", None])
async def test_status_accepts_only_an_exact_human_subject(service, recipient):
    with pytest.raises(ContentError):
        await service.user_status("owner", recipient, True)


@pytest.mark.parametrize("action", ["status", "suspend", "resume"])
def test_cli_status_commands_preserve_the_subject_and_use_strict_booleans(tmp_path, monkeypatch, capsys, action):
    store = cli.CredentialStore(tmp_path / "settings")
    store.keyring = None
    store.set_server("https://wiki.example.test")
    store.put(cli.oauth_key(store.server()), {"access_token": "synthetic-secret", "expires_at": 1e20})

    def handle(request):
        assert request.url.path == "/api/users/status"
        if action == "status":
            assert request.method == "GET"
            assert request.url.params["subject"] == "reader"
        else:
            assert request.method == "POST"
            assert json.loads(request.content) == {"subject": "reader", "suspended": action == "suspend"}
        return httpx.Response(200, json={"subject": "reader", "suspended": action == "suspend", "pending": False})

    client = cli.APIClient(store.server(), store, httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(cli, "CredentialStore", lambda: store)
    monkeypatch.setattr(cli, "APIClient", lambda *_a: client)
    assert cli.main(["user", action, "reader"]) == 0
    assert "synthetic-secret" not in capsys.readouterr().out
