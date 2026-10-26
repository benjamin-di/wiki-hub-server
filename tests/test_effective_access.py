import json

import pytest
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.testclient import TestClient

from wiki_hub.server import create_app
from wiki_hub.service import Denied


@pytest.mark.parametrize("role", ["none", "read", "contribute", "write", "admin"])
async def test_effective_access_reports_only_the_callers_current_permissions(service, role):
    await service.create("owner", "shared", "Shared")
    await service.create("owner", "hidden", "Hidden")
    await service.share("owner", "shared", "reader", role)
    report = await service.effective_access("reader")
    assert report["subject"] == "reader" and report["credential_type"] == "oauth"
    assert report["owner"] is False
    if role == "none":
        assert report["wikis"] == []
    else:
        permissions = ["read", "contribute", "write", "admin"]
        assert report["wikis"] == [{"slug": "shared", "title": "Shared", "status": "ready",
                                    "permissions": permissions[:permissions.index(role) + 1],
                                    "local_copies": True}]
    assert "hidden" not in json.dumps(report)


async def test_effective_agent_access_is_explicit_capped_and_never_includes_git(service):
    await service.create("owner", "shared", "Shared")
    await service.create("owner", "hidden", "Hidden")
    agent = await service.add_agent("owner", "Source filer")
    identifier = agent["id"]
    assert (await service.effective_access(identifier))["wikis"] == []
    with service.state.connect() as db:
        db.execute("UPDATE agents SET owner='reader' WHERE id=?", (identifier,))
    await service.share("owner", "shared", "reader", "contribute")
    await service.share("owner", "shared", identifier, "contribute")
    report = await service.effective_access(identifier)
    assert report["subject"] == identifier and report["credential_type"] == "agent_key"
    assert report["wikis"][0]["permissions"] == ["read", "contribute"]
    assert report["wikis"][0]["local_copies"] is False
    assert "hidden" not in json.dumps(report) and agent["credential"] not in json.dumps(report)
    await service.share("owner", "shared", "reader", "read")
    assert (await service.effective_access(identifier))["wikis"][0]["permissions"] == ["read"]
    await service.share("owner", "shared", "reader", "none")
    assert (await service.effective_access(identifier))["wikis"] == []


async def test_effective_access_distinguishes_archived_content_from_administration(service):
    await service.create("owner", "shared", "Shared")
    await service.archive("owner", "shared", True)
    report = await service.effective_access("owner")
    assert report["owner"] is True
    assert report["wikis"][0]["status"] == "archived"
    assert report["wikis"][0]["permissions"] == ["read", "admin"]
    service.state.set_copy_setting("wiki", "shared", True)  # Pending backend updates deny copies.
    assert (await service.effective_access("owner"))["wikis"][0]["local_copies"] is False


async def test_effective_access_excludes_pending_grants_and_denies_suspended_identities(service):
    await service.create("owner", "shared", "Shared")
    service.state.set_grant("shared", "reader", "write")
    assert (await service.effective_access("reader"))["wikis"] == []
    agent = await service.add_agent("owner", "Source filer")
    service.state.set_user_status("owner", True)
    for subject in ("owner", agent["id"]):
        with pytest.raises(Denied):
            await service.effective_access(subject)


class Humans(TokenVerifier):
    async def verify_token(self, token):
        if token == "owner-token":
            return AccessToken(token=token, client_id="fixture", scopes=["wiki:access"], claims={"sub": "owner"})
        return None


def test_access_diagnostic_requires_authentication_and_is_not_cacheable(settings, service):
    with TestClient(create_app(settings, service, Humans())) as client:
        assert client.get("/api/me/access").status_code == 401
        owner = {"Authorization": "Bearer owner-token"}
        response = client.get("/api/me/access", headers=owner)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {"subject": "owner", "owner": True, "credential_type": "oauth", "wikis": []}
        assert client.get("/api/me", headers=owner).json() == {"subject": "owner", "owner": True}
        client.post("/api/wikis", headers=owner, json={"slug": "shared", "title": "Shared"}).raise_for_status()
        agent = client.post("/api/agents", headers=owner, json={"name": "Reader"}).json()
        agent_headers = {"Authorization": "Bearer " + agent["credential"]}
        assert client.get("/api/me/access", headers=agent_headers).json()["wikis"] == []
        client.post("/api/wikis/shared/share", headers=owner,
                    json={"subject": agent["id"], "role": "read"}).raise_for_status()
        report = client.get("/api/me/access", headers=agent_headers).json()
        assert report["subject"] == agent["id"] and report["credential_type"] == "agent_key"
        assert report["wikis"][0]["permissions"] == ["read"]
        assert report["wikis"][0]["local_copies"] is False
        client.post(f"/api/agents/{agent['id']}/revoke", headers=owner).raise_for_status()
        assert client.get("/api/me/access", headers=agent_headers).status_code == 401
