import asyncio
import base64

from fastmcp import Client
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
        stranger = {"Authorization": "Bearer stranger-token"}
        assert client.get("/api/wikis", headers=stranger).json() == []
        for path in ("pages", "page?path=wiki/index.md", "search?q=source", "history", "document?path=raw/x"):
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
