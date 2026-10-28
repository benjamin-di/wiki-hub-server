"""Real signed workload tokens, local agent ceilings and both HTTP interfaces."""

import asyncio
import base64
import time
from unittest.mock import AsyncMock

import pytest
from joserfc import jwk, jwt
from starlette.testclient import TestClient

from wiki_hub.auth import PrincipalVerifier, create_verifier, principal_subject
from wiki_hub.gitstore import ContentError
from wiki_hub.server import create_app
from wiki_hub.service import Denied
from wiki_hub.state import State


@pytest.fixture
def identity(settings):
    key = jwk.RSAKey.generate_key(2048, parameters={"kid": "synthetic-key"})
    verifier = create_verifier(settings)
    verifier._fetch_jwks = AsyncMock(return_value={"keys": [key.as_dict(private=False)]})

    def issue(subject="owner", client="wiki-cli", changes=None):
        now = int(time.time())
        claims = {"iss": settings.oidc_issuer, "aud": settings.oidc_audience, "sub": subject,
                  "client_id": client, "scope": "wiki:access", "iat": now, "exp": now + 900}
        claims.update(changes or {})
        return jwt.encode({"alg": "RS256", "typ": "at+jwt", "kid": "synthetic-key"}, claims, key)

    return verifier, issue


def test_real_workload_rest_and_mcp_have_only_explicit_grants(settings, service, identity):
    verifier, issue = identity
    owner = {"Authorization": "Bearer " + issue()}
    machine_token = issue("client-source-filer", "source-filer")
    machine = {"Authorization": "Bearer " + machine_token}
    with TestClient(create_app(settings, service, verifier)) as client:
        # An unbound machine is never enrolled as a human, even with a valid signature.
        assert client.get("/api/me", headers=machine).status_code == 401
        response = client.post("/api/agents/workloads", headers=owner,
                               json={"name": "Source filer", "client_id": "source-filer"})
        assert response.status_code == 201
        agent = response.json()
        assert "credential" not in agent and "token" not in agent
        assert client.get("/api/wikis", headers=machine).json() == []
        for slug in ("handbook", "private"):
            client.post("/api/wikis", headers=owner, json={"slug": slug, "title": slug}).raise_for_status()
        client.post("/api/wikis/handbook/share", headers=owner,
                    json={"subject": agent["id"], "role": "read"}).raise_for_status()
        access = client.get("/api/me/access", headers=machine).json()
        assert access["subject"] == agent["id"] and access["credential_type"] == "workload"
        assert [row["slug"] for row in access["wikis"]] == ["handbook"]
        assert access["identity"] == {"issuer": settings.oidc_issuer, "client_id": "source-filer",
                                      "subject": "client-source-filer"}
        assert client.get("/api/wikis/private/pages", headers=machine).status_code == 403
        second = client.post("/api/agents/workloads", headers=owner,
                             json={"name": "Private reader", "client_id": "private-reader"}).json()
        client.post("/api/wikis/private/share", headers=owner,
                    json={"subject": second["id"], "role": "read"}).raise_for_status()
        other = {"Authorization": "Bearer " + issue("client-private-reader", "private-reader")}
        assert [row["slug"] for row in client.get("/api/wikis", headers=other).json()] == ["private"]
        assert client.get("/api/wikis/handbook/pages", headers=other).status_code == 403
        assert client.post("/api/wikis/handbook/clone", headers=machine, json={}).status_code == 403
        assert client.post("/api/agents/workloads", headers=machine,
                           json={"name": "Child", "client_id": "child"}).status_code == 403
        mcp_headers = {**machine, "Accept": "application/json, text/event-stream"}

        def mcp(method, params, headers=mcp_headers):
            return client.post("/mcp", headers=headers,
                               json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

        mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "synthetic-client", "version": "1"}}).raise_for_status()
        tools = mcp("tools/call", {"name": "wiki_list", "arguments": {}})
        tools.raise_for_status()
        result = tools.json()["result"]
        assert result["isError"] is False
        assert [row["slug"] for row in result["structuredContent"]["result"]] == ["handbook"]
        denied = mcp("tools/call", {"name": "wiki_read", "arguments": {"wiki": "private", "path": "wiki/index.md"}})
        assert denied.json()["result"]["isError"] is True
        other_headers = {**other, "Accept": "application/json, text/event-stream"}
        second_list = mcp("tools/call", {"name": "wiki_list", "arguments": {}}, other_headers)
        assert [row["slug"] for row in second_list.json()["result"]["structuredContent"]["result"]] == ["private"]
        client.post(f"/api/agents/{agent['id']}/revoke", headers=owner).raise_for_status()
        assert client.get("/api/wikis", headers=machine).status_code == 401
        assert mcp("tools/call", {"name": "wiki_list", "arguments": {}}).status_code == 401
        assert client.get("/api/wikis/private/pages", headers=other).status_code == 200


async def test_workload_preserves_signed_identity_and_survives_state_reopen(service, identity, settings):
    oidc, issue = identity
    legacy = await service.add_agent("owner", "Legacy reader")
    created = await service.add_workload_agent("owner", "Source filer", "source-filer")
    raw = issue("client-source-filer", "source-filer")
    state = State(settings.state_dir)
    verifier = PrincipalVerifier(oidc, state)
    assert principal_subject(await verifier.verify_token(legacy["credential"])) == legacy["id"]
    access = await verifier.verify_token(raw)
    assert access.claims["sub"] == access.subject == "client-source-filer"
    assert access.claims["client_id"] == access.client_id == "source-filer"
    assert principal_subject(access) == created["id"]
    await service.revoke_agent("owner", created["id"])
    assert await verifier.verify_token(raw) is None


@pytest.mark.parametrize("changes", [
    {"sub": "client-other"}, {"sub": "owner"}, {"client_id": "other"}, {"exp": int(time.time()) + 3600},
    {"scope": "openid wiki:access"}, {"groups": ["administrators"]},
    {"iss": "https://other.example.test"}, {"aud": "https://other.example.test/mcp"},
])
async def test_workload_rejects_mismatches_and_human_authority(service, identity, changes):
    oidc, issue = identity
    await service.add_workload_agent("owner", "Reader", "source-filer")
    raw = issue("client-source-filer", "source-filer", changes)
    assert await PrincipalVerifier(oidc, service.state).verify_token(raw) is None


async def test_workload_owner_cap_and_revoke_prevent_queued_publication(service, identity):
    oidc, issue = identity
    await service.create("owner", "handbook", "Handbook")
    created = await service.add_workload_agent("owner", "Filer", "source-filer")
    # A delegated owner demonstrates that a machine does not inherit its registering operator.
    with service.state.connect() as db:
        db.execute("UPDATE agents SET owner='editor' WHERE id=?", (created["id"],))
    await service.share("owner", "handbook", "editor", "contribute")
    await service.share("owner", "handbook", created["id"], "contribute")
    token = await PrincipalVerifier(oidc, service.state).verify_token(issue("client-source-filer", "source-filer"))
    actor = principal_subject(token)
    before = await service.history("owner", "handbook")
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*_args):
        entered.set()
        await release.wait()

    service.runner = blocked
    job = await service.ingest(actor, "handbook", "note.txt", base64.b64encode(b"Synthetic source").decode())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await service.share("owner", "handbook", "editor", "none")
        with pytest.raises(Denied):
            await service.pages(actor, "handbook")
    finally:
        release.set()
        await asyncio.gather(*service.tasks)
    assert (await service.job("owner", job["id"]))["status"] == "failed"
    assert await service.history("owner", "handbook") == before


async def test_workload_registration_is_owner_only_and_cannot_restore_a_revoked_binding(service):
    with pytest.raises(Denied):
        await service.add_workload_agent("stranger", "Reader", "source-filer")
    first = await service.add_workload_agent("owner", "Reader", "source-filer")
    await service.revoke_agent("owner", first["id"])
    with pytest.raises(ContentError):
        await service.add_workload_agent("owner", "Replacement", "source-filer")


async def test_workloads_cannot_bypass_agent_roles_through_direct_subject_grants(service):
    await service.create("owner", "handbook", "Handbook")
    for role in ("read", "write", "admin"):
        with pytest.raises(ContentError, match="registered agent"):
            await service.share("owner", "handbook", "client-source-filer", role)


@pytest.mark.parametrize("client_id", [None, "", "wiki-cli", " padded ", "bad\nvalue", "bad/value", "x" * 161, []])
async def test_workload_registration_rejects_invalid_identifiers(service, client_id):
    with pytest.raises(ContentError):
        await service.add_workload_agent("owner", "Reader", client_id)
    assert await service.agents("owner") == []


async def test_signed_claims_cannot_select_a_different_local_actor(service, identity):
    oidc, issue = identity
    agent = await service.add_workload_agent("owner", "Reader", "source-filer")
    verifier = PrincipalVerifier(oidc, service.state)
    raw = issue("client-source-filer", "source-filer", {"wiki_subject": "owner"})
    assert principal_subject(await verifier.verify_token(raw)) == agent["id"]
    human = issue(changes={"wiki_subject": agent["id"]})
    assert principal_subject(await verifier.verify_token(human)) == "owner"
