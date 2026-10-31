import asyncio
import base64
import threading

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.testclient import TestClient

from wiki_hub.mcp import create_mcp
from wiki_hub.server import create_app


class FixtureVerifier(TokenVerifier):
    def __init__(self):
        super().__init__(required_scopes=["wiki:access"])

    async def verify_token(self, token):
        if token in ("owner-token", "stranger-token"):
            subject = token.removesuffix("-token")
            return AccessToken(token=token, client_id="test-cli", subject=subject,
                               scopes=["wiki:access"], claims={"sub": subject})
        return None


async def test_cancelled_original_download_holds_its_wiki_lock(settings, service, monkeypatch, tmp_path):
    await service.create("owner", "starter", "Starter")
    entered, release = threading.Event(), threading.Event()

    def download(*args):
        entered.set()
        assert release.wait(10), "test did not release download"
        return tmp_path / "unused.txt"

    monkeypatch.setattr(service.git, "original", download)
    app = create_app(settings, service, FixtureVerifier())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_url) as client:
        request = asyncio.create_task(client.get("/api/wikis/starter/document?path=raw/original.txt",
                                                headers={"Authorization": "Bearer owner-token"}))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            request.cancel()
            await asyncio.sleep(0)
            assert service.lock("starter").locked()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request


@pytest.mark.parametrize("waiting", ["lock", "download"])
async def test_original_download_rechecks_access_after_waiting(settings, service, monkeypatch, tmp_path, waiting):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "stranger", "read")
    original = tmp_path / "original.txt"
    original.write_text("Private synthetic original")
    initial_check = asyncio.Event()
    entered, release = threading.Event(), threading.Event()
    allowed = service.allowed

    def observed(subject, *args, **kwargs):
        result = allowed(subject, *args, **kwargs)
        if subject == "stranger":
            initial_check.set()
        return result

    def download(*args):
        entered.set()
        if waiting == "download":
            assert release.wait(10), "test did not release download"
        return original

    monkeypatch.setattr(service, "allowed", observed)
    monkeypatch.setattr(service.git, "original", download)
    if waiting == "lock":
        await service.lock("starter").acquire()
    app = create_app(settings, service, FixtureVerifier())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://wiki.example.test") as client:
        request = asyncio.create_task(client.get("/api/wikis/starter/document?path=raw/original.txt",
                                                headers={"Authorization": "Bearer stranger-token"}))
        try:
            await asyncio.wait_for(initial_check.wait(), 5)
            if waiting == "download":
                assert await asyncio.to_thread(entered.wait, 5)
            await service.share("owner", "starter", "stranger", "none")
        finally:
            if waiting == "lock":
                service.lock("starter").release()
            release.set()
        response = await asyncio.wait_for(request, 10)
    assert response.status_code == 403
    assert "Private synthetic original" not in response.text
    if waiting == "lock":
        assert not entered.is_set()


def test_http_auth_discovery_and_denied_paths(settings, service):
    with TestClient(create_app(settings, service, FixtureVerifier())) as client:
        assert client.get("/health").status_code == 200
        config = client.get("/api/config").json()
        assert config["resource"] == settings.oidc_audience
        anonymous = client.get("/api/wikis")
        assert anonymous.status_code == 401
        assert "resource_metadata=" in anonymous.headers["www-authenticate"]
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == settings.oidc_audience
        assert metadata.json()["authorization_servers"] == [settings.oidc_issuer]
        owner = {"Authorization": "Bearer owner-token"}
        assert client.post("/api/wikis", headers=owner, json={"slug": "starter", "title": "Starter"}).status_code == 201
        copy_access = client.get("/api/wikis/starter/copy-access", headers=owner)
        assert copy_access.json() == {"wiki": "starter", "role": "admin", "local_copies": True}
        assert copy_access.headers["Cache-Control"] == "no-store"
        stranger = {"Authorization": "Bearer stranger-token"}
        assert client.get("/api/wikis", headers=stranger).json() == []
        for path in ("pages", "page?path=wiki/index.md", "search?q=source", "history", "copy-access",
                     "document?path=raw/x"):
            assert client.get("/api/wikis/starter/" + path, headers=stranger).status_code == 403
        assert client.post("/api/wikis/starter/clone", headers=stranger, json={}).status_code == 403
        assert client.get("/api/wikis/starter/page?path=../secret", headers=owner).status_code == 400


def test_copy_settings_require_boolean_arguments_and_preserve_api_reads(settings, service):
    with TestClient(create_app(settings, service, FixtureVerifier())) as client:
        owner = {"Authorization": "Bearer owner-token"}
        client.post("/api/wikis", headers=owner, json={"slug": "starter", "title": "Starter"})
        path = "/api/wikis/starter/settings"
        for invalid in (None, "false", 0, []):
            assert client.post(path, headers=owner, json={"local_copies": invalid}).status_code == 400
        assert client.post(path, headers=owner, json={"local_copies": False}).json()["local_copies"] is False
        assert client.post("/api/wikis/starter/clone", headers=owner, json={}).status_code == 403
        assert client.get("/api/wikis/starter/pages", headers=owner).status_code == 200
        share = client.post("/api/wikis/starter/share", headers=owner,
                            json={"subject": "stranger", "role": "admin"})
        assert share.status_code == 200
        delegated = {"Authorization": "Bearer stranger-token"}
        assert client.get(path, headers=delegated).status_code == 200
        assert client.post("/api/users/settings", headers=delegated,
                           json={"subject": "owner", "local_copies": True}).status_code == 403
        assert client.post("/api/users/settings", headers=owner,
                           json={"subject": "stranger", "local_copies": False}).status_code == 200


async def test_real_mcp_client_can_use_first_wiki(service):
    await service.create("owner", "starter", "Starter")
    job = await service.ingest("owner", "starter", "note.txt",
                               base64.b64encode(b"The workshop opens at 09:30.").decode())
    await asyncio.gather(*service.tasks)
    assert (await service.job("owner", job["id"]))["status"] == "completed"
    async with Client(create_mcp(service, subject_provider=lambda: "owner")) as client:
        tools = await client.list_tools()
        assert "wiki_search" in [tool.name for tool in tools]
        result = await client.call_tool("wiki_search", {"wiki": "starter", "query": "09:30"})
        assert "09:30" in str(result)
        page = await client.call_tool("wiki_read", {"wiki": "starter", "path": "wiki/workshop.md"})
        assert "raw/" in str(page)
    async with Client(create_mcp(service, subject_provider=lambda: "stranger")) as client:
        result = await client.call_tool("wiki_list")
        assert result.data == []
        result = await client.call_tool("wiki_search", {"wiki": "starter", "query": "09:30"}, raise_on_error=False)
        assert result.is_error


def test_http_shares_and_original_download_are_revoked(settings, service):
    with TestClient(create_app(settings, service, FixtureVerifier())) as client:
        owner = {"Authorization": "Bearer owner-token"}
        reader = {"Authorization": "Bearer stranger-token"}
        client.post("/api/wikis", headers=owner, json={"slug": "starter", "title": "Starter"}).raise_for_status()
        assert client.get("/api/me", headers=reader).json()["subject"] == "stranger"
        assert client.post("/api/wikis/starter/share", headers=reader,
                           json={"subject": "owner", "role": "read"}).status_code == 403
        grant = client.post("/api/wikis/starter/share", headers=owner,
                            json={"subject": "stranger", "role": "read"})
        assert grant.status_code == 200
        assert grant.json()["grants"][0]["role"] == "read"
        assert len(client.get("/api/wikis", headers=reader).json()) == 1
        assert client.get("/api/wikis/starter/page?path=wiki/index.md", headers=reader).status_code == 200
        assert client.post("/api/wikis/starter/clone", headers=reader, json={"write": False}).status_code == 200
        assert client.post("/api/wikis/starter/clone", headers=reader, json={"write": True}).status_code == 403
        assert client.post("/api/wikis/starter/unshare", headers=owner,
                           json={"subject": "stranger"}).status_code == 200
        assert client.get("/api/wikis", headers=reader).json() == []
        for path in ("pages", "page?path=wiki/index.md", "search?q=starter", "history", "document?path=raw/x"):
            assert client.get("/api/wikis/starter/" + path, headers=reader).status_code == 403


async def test_mcp_grant_changes_apply_to_existing_client(service):
    await service.create("owner", "starter", "Starter")
    async with Client(create_mcp(service, subject_provider=lambda: "reader")) as client:
        assert (await client.call_tool("wiki_list")).data == []
        await service.share("owner", "starter", "reader", "read")
        assert len((await client.call_tool("wiki_list")).data) == 1
        assert not (await client.call_tool("wiki_read", {"wiki": "starter", "path": "wiki/index.md"})).is_error
        await service.share("owner", "starter", "reader", "none")
        assert (await client.call_tool("wiki_list")).data == []
        assert (await client.call_tool("wiki_read", {"wiki": "starter", "path": "wiki/index.md"},
                                       raise_on_error=False)).is_error


async def test_http_submission_jobs_and_pinned_evidence_with_current_grants(settings, service):
    app = create_app(settings, service, FixtureVerifier())
    owner = {"Authorization": "Bearer owner-token"}
    reader = {"Authorization": "Bearer stranger-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://wiki.example.test",
    ) as client:
        await client.post("/api/wikis", headers=owner, json={"slug": "starter", "title": "Starter"})
        submitted = {"filename": "source.txt", "content_base64": base64.b64encode(b"Opens 09:30").decode(),
                     "submission_id": "http-request"}
        missing = await client.post("/api/wikis/starter/submissions", headers=owner,
                                    json={k: v for k, v in submitted.items() if k != "submission_id"})
        assert missing.status_code == 400 and not service.state.jobs("starter")
        first = (await client.post("/api/wikis/starter/submissions", headers=owner, json=submitted)).json()
        replay = (await client.post("/api/wikis/starter/ingest", headers=owner, json=submitted)).json()
        assert replay["id"] == first["id"]
        await asyncio.gather(*service.tasks)
        listing = await client.get("/api/wikis/starter/jobs", headers=owner)
        assert listing.headers["cache-control"] == "no-store"
        assert listing.json()["jobs"][0]["id"] == first["id"]
        result = (await client.get("/api/wikis/starter/search?q=09:30", headers=owner)).json()
        revision = result["revision"]
        await client.post("/api/wikis/starter/rollback", headers=owner,
                          json={"revision": (await service.history("owner", "starter"))[-1]["revision"]})
        page = await client.get("/api/wikis/starter/page", headers=owner,
                                params={"path": "wiki/workshop.md", "revision": revision})
        assert page.status_code == 200 and page.json()["revision"] == revision
        params = {"path": f"raw/{first['id']}-source.txt", "revision": revision}
        download = await client.get("/api/wikis/starter/document", headers=owner, params=params)
        assert download.content == b"Opens 09:30"
        assert download.headers["X-Wiki-Revision"] == revision
        await service.share("owner", "starter", "stranger", "read")
        assert (await client.get("/api/wikis/starter/document", headers=reader, params=params)).status_code == 200
        await service.share("owner", "starter", "stranger", "none")
        assert (await client.get("/api/wikis/starter/document", headers=reader, params=params)).status_code == 403
        assert (await client.get("/api/wikis/starter/jobs", headers=reader)).status_code == 403


async def test_connected_mcp_supports_submission_replay_listing_and_historical_reads(service):
    await service.create("owner", "starter", "Starter")
    async with Client(create_mcp(service, subject_provider=lambda: "owner")) as client:
        params = {"wiki": "starter", "filename": "source.txt", "content_base64": base64.b64encode(b"09:30").decode(),
                  "submission_id": "mcp-request"}
        first = (await client.call_tool("wiki_ingest", params)).data
        replay = (await client.call_tool("wiki_ingest", params)).data
        assert first["id"] == replay["id"]
        await asyncio.gather(*service.tasks)
        listing = (await client.call_tool("wiki_jobs", {"wiki": "starter"})).data
        revision = listing["jobs"][0]["revision"]
        result = await client.call_tool("wiki_read", {"wiki": "starter", "path": "wiki/workshop.md",
                                                     "revision": revision})
        assert result.data["revision"] == revision and "09:30" in result.data["content"]


async def test_pinned_server_sdk_rejects_a_new_submission_argument_on_a_legacy_tool():
    # Exercise the server's pinned SDK, independently of newer client SDK tests.
    older = FastMCP("Synthetic older tool signature")
    invoked = []

    @older.tool
    async def wiki_ingest(wiki: str, filename: str, content_base64: str, instructions: str = "") -> dict:
        invoked.append(True)
        return {"id": "unkeyed-job"}

    async with Client(older) as client:
        result = await client.call_tool("wiki_ingest", {"wiki": "pilot", "filename": "note.txt",
            "content_base64": "eA==", "submission_id": "retained-id"}, raise_on_error=False)
    assert result.is_error and invoked == []


async def test_rest_and_remote_mcp_record_verified_clients_and_ignore_public_headers(settings, service):
    app = create_app(settings, service, FixtureVerifier())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.public_url) as client:
        headers = {'Authorization': 'Bearer owner-token', 'X-Wiki-Client': 'forged-client'}
        response = await client.get('/api/me/apps', headers=headers)
        assert response.status_code == 200
        assert response.json()[0]['client_id'] == 'test-cli'
        assert await service.apps('stranger') == []
        await service.create('owner', 'starter', 'Starter')
        job = await client.post('/api/wikis/starter/ingest', headers=headers, json={
            'filename': 'source.txt', 'content_base64': base64.b64encode(b'Synthetic source').decode(),
            'client_id': 'forged-client'})
        assert job.status_code == 202 and job.json()['client_id'] == 'test-cli'
        await asyncio.gather(*service.tasks)
        mcp_headers = {**headers, 'Accept': 'application/json, text/event-stream'}
        response = await client.post('/mcp', headers=mcp_headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'wiki_ingest', 'arguments': {
                'wiki': 'starter', 'filename': 'mcp-source.txt',
                'content_base64': base64.b64encode(b'Synthetic MCP source').decode()}}})
        assert response.status_code == 200, response.text
        result = response.json()['result']
        assert not result.get('isError'), result
        assert result['structuredContent']['client_id'] == 'test-cli'
        await asyncio.gather(*service.tasks)
        service.state.set_user_status('owner', True)
        assert (await client.get('/api/me/apps', headers=headers)).status_code == 403


def test_wiki_creation_purpose_and_unsupported_head_do_not_cross_methods(settings, service):
    with TestClient(create_app(settings, service, FixtureVerifier())) as client:
        headers = {"Authorization": "Bearer owner-token"}
        assert client.head("/api/wikis", headers=headers).status_code == 404
        assert client.get("/api/wikis", headers=headers).json() == []
        created = client.post("/api/wikis", headers=headers,
                              json={"slug": "plans", "title": "Plans", "purpose": "Keep meeting notes"})
        assert created.status_code == 201
        page = client.get("/api/wikis/plans/page?path=wiki/index.md", headers=headers)
        assert "Keep meeting notes" in page.json()["content"]
