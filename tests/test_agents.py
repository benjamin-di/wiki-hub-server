import asyncio
import base64
import hashlib
import json
import threading

import httpx
import pytest
from fastmcp import Client
from fastmcp.server.auth import AccessToken
from starlette.testclient import TestClient

from wiki_hub import cli
from wiki_hub.auth import PrincipalVerifier
from wiki_hub.gitstore import ContentError
from wiki_hub.mcp import create_mcp
from wiki_hub.server import create_app
from wiki_hub.service import Denied


class Humans:
    async def verify_token(self, token):
        if token == "owner-token":
            return AccessToken(token=token, client_id="test", scopes=["wiki:access"], claims={"sub": "owner"})
        return None


async def test_agents_need_explicit_grants_and_never_get_git_or_admin(service):
    await service.create("owner", "starter", "Starter")
    with pytest.raises(Denied):
        await service.add_agent("stranger", "Uninvited")
    agent = await service.add_agent("owner", "Workshop reader")
    identifier, token = agent["id"], agent["credential"]
    verifier = PrincipalVerifier(Humans(), service.state)
    assert (await verifier.verify_token(token)).claims["sub"] == identifier
    with service.state.connect() as db:
        assert db.execute("SELECT token_hash FROM agents").fetchone()[0] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in json.dumps(await service.agents("owner"))
    assert await service.list_wikis(identifier) == []
    await service.share("owner", "starter", identifier, "read")
    assert await service.read_page(identifier, "starter", "wiki/index.md")
    for operation in (
        service.create(identifier, "other", "Other"), service.add_agent(identifier, "Child"),
        service.share(identifier, "starter", "stranger", "read"),
        service.clone(identifier, "starter", write=False), service.rollback(identifier, "starter", "a" * 40),
        service.ingest(identifier, "starter", "note.txt", base64.b64encode(b"Source").decode()),
        service.revoke_agent("stranger", identifier),
    ):
        with pytest.raises(Denied):
            await operation
    await service.create("owner", "private", "Private")
    assert [w["slug"] for w in await service.list_wikis(identifier)] == ["starter"]
    await service.revoke_agent("owner", identifier)
    assert await verifier.verify_token(token) is None
    assert await service.list_wikis(identifier) == []
    with pytest.raises(Denied):
        await service.pages(identifier, "starter")


async def test_agent_is_capped_by_owners_current_grant(service):
    await service.create("owner", "starter", "Starter")
    agent = await service.add_agent("owner", "Imported delegate")
    identifier = agent["id"]
    # Rehearse a delegated human owner for the next account-management slice.
    with service.state.connect() as db:
        db.execute("UPDATE agents SET owner='reader' WHERE id=?", (identifier,))
    await service.share("owner", "starter", "reader", "contribute")
    await service.share("owner", "starter", identifier, "contribute")
    job = await service.ingest(identifier, "starter", "note.txt", base64.b64encode(b"Source").decode())
    await asyncio.gather(*service.tasks)
    assert (await service.job(identifier, job["id"]))["status"] == "completed"
    await service.share("owner", "starter", "reader", "read")
    assert await service.pages(identifier, "starter")
    with pytest.raises(Denied):
        await service.ingest(identifier, "starter", "note.txt", base64.b64encode(b"Source").decode())
    await service.share("owner", "starter", "reader", "none")
    assert await service.list_wikis(identifier) == []
    with pytest.raises(Denied):
        await service.job(identifier, job["id"])


async def test_agent_revocation_while_model_runs_prevents_publication(service):
    await service.create("owner", "starter", "Starter")
    before = await service.history("owner", "starter")
    agent = await service.add_agent("owner", "Source filer")
    await service.share("owner", "starter", agent["id"], "contribute")
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        entered.set()
        await release.wait()

    service.runner = blocked
    job = await service.ingest(agent["id"], "starter", "note.txt", base64.b64encode(b"Source").decode())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await service.revoke_agent("owner", agent["id"])
    finally:
        release.set()
        await asyncio.gather(*service.tasks)
    assert (await service.job("owner", job["id"]))["status"] == "failed"
    assert await service.history("owner", "starter") == before


async def test_revoke_waits_for_already_authorized_publication(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    agent = await service.add_agent("owner", "Source filer")
    await service.share("owner", "starter", agent["id"], "contribute")
    entered, release = threading.Event(), threading.Event()
    publish = service.git.publish

    def delayed(*args):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("Test did not release publication")
        return publish(*args)

    monkeypatch.setattr(service.git, "publish", delayed)
    job = await service.ingest(agent["id"], "starter", "note.txt", base64.b64encode(b"Source").decode())
    revoke = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        revoke = asyncio.create_task(service.revoke_agent("owner", agent["id"]))
        await asyncio.sleep(0)
        assert service.state.token_agent(agent["credential"]) is None
        assert not revoke.done()
    finally:
        release.set()
        if revoke:
            await revoke
        await asyncio.gather(*service.tasks)
    assert (await service.job("owner", job["id"]))["status"] == "completed"


def test_live_http_agent_token_revocation_and_one_time_credential_response(settings, service):
    owner = {"Authorization": "Bearer owner-token"}
    with TestClient(create_app(settings, service, Humans())) as client:
        client.post("/api/wikis", headers=owner, json={"slug": "starter", "title": "Starter"}).raise_for_status()
        created = client.post("/api/agents", headers=owner, json={"name": "Workshop reader"})
        assert created.headers["cache-control"] == "no-store"
        agent = created.json()
        bearer = {"Authorization": "Bearer " + agent["credential"]}
        assert client.get("/api/wikis", headers=bearer).json() == []
        client.post("/api/wikis/starter/share", headers=owner,
                    json={"subject": agent["id"], "role": "read"}).raise_for_status()
        assert len(client.get("/api/wikis", headers=bearer).json()) == 1
        assert client.post("/api/wikis/starter/clone", headers=bearer, json={"write": False}).status_code == 403
        assert agent["credential"] not in client.get("/api/agents", headers=owner).text
        client.post(f"/api/agents/{agent['id']}/revoke", headers=owner).raise_for_status()
        assert client.get("/api/wikis", headers=bearer).status_code == 401


async def test_already_connected_mcp_agent_cannot_keep_revoked_access(service):
    await service.create("owner", "starter", "Starter")
    agent = await service.add_agent("owner", "Workshop reader")
    await service.share("owner", "starter", agent["id"], "read")
    async with Client(create_mcp(service, subject_provider=lambda: agent["id"])) as client:
        assert len((await client.call_tool("wiki_list")).data) == 1
        await service.revoke_agent("owner", agent["id"])
        assert (await client.call_tool("wiki_list")).data == []
        assert (await client.call_tool("wiki_read", {"wiki": "starter", "path": "wiki/index.md"},
                                       raise_on_error=False)).is_error


async def test_human_tokens_cannot_impersonate_registered_agent_namespace(service):
    class CollidingIdentity:
        async def verify_token(self, token):
            return AccessToken(token=token, client_id="test", scopes=["wiki:access"],
                               claims={"sub": "agent:fixture"})

    assert await PrincipalVerifier(CollidingIdentity(), service.state).verify_token("human-token") is None


@pytest.mark.parametrize("role", ["write", "admin"])
async def test_agent_grants_cannot_escalate_above_contribute(service, role):
    await service.create("owner", "starter", "Starter")
    agent = await service.add_agent("owner", "Reader")
    with pytest.raises(ContentError):
        await service.share("owner", "starter", agent["id"], role)
    assert await service.list_wikis(agent["id"]) == []


def test_cli_saves_agent_key_separately_and_never_prints_it(tmp_path, monkeypatch, capsys):
    original_store = cli.CredentialStore
    owner_store = original_store(tmp_path / "owner")
    owner_store.keyring = None
    owner_store.set_server("https://wiki.example.test")
    owner_store.put(cli.oauth_key(owner_store.server()), {"access_token": "human-secret", "expires_at": 1e20})
    destination = tmp_path / "agent"
    token = "whagent_synthetic-secret"

    def stores(directory=None):
        result = original_store(directory) if directory else owner_store
        result.keyring = None
        return result

    def handle(request):
        assert request.headers["Authorization"] == "Bearer human-secret"
        return httpx.Response(201, json={"id": "agent:fixture", "name": "Reader", "credential": token})

    client = cli.APIClient(owner_store.server(), owner_store, httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(cli, "CredentialStore", stores)
    monkeypatch.setattr(cli, "APIClient", lambda *args: client)
    assert cli.main(["agent", "add", "Reader", "--directory", str(destination)]) == 0
    assert token not in capsys.readouterr().out
    saved = stores(destination)
    assert saved.agent() == "agent:fixture"
    assert saved.get(cli.agent_key(owner_store.server(), saved.agent()))["access_token"] == token
    assert "human-secret" not in saved.path.read_text()


def test_agent_cli_never_refreshes_or_uses_human_oauth(tmp_path):
    store = cli.CredentialStore(tmp_path / "agent")
    store.keyring = None
    store.set_agent("agent:fixture")
    store.put(cli.agent_key("https://wiki.example.test", store.agent()), {"access_token": "whagent_synthetic"})

    def handle(request):
        assert request.headers["Authorization"] == "Bearer whagent_synthetic"
        assert request.url.path == "/api/wikis"
        return httpx.Response(200, json=[])

    client = cli.APIClient("https://wiki.example.test", store, httpx.Client(transport=httpx.MockTransport(handle)))
    assert client.request("GET", "/api/wikis") == []
