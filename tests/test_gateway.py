"""The trusted gateway authenticates a caller, never substitutes their grants."""

import asyncio
import base64
from dataclasses import replace

import httpx
import pytest
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.testclient import TestClient

from wiki_hub.auth import GatewayVerifier
from wiki_hub.server import create_app

KEY = "synthetic-gateway-backend-credential-0001"
ROTATED_KEY = "synthetic-gateway-backend-credential-0002"


class HumanVerifier(TokenVerifier):
    async def verify_token(self, token):
        if token == "human-oauth":
            return AccessToken(token=token, client_id="fixture", scopes=["wiki:access"],
                               subject="owner", claims={"sub": "owner"})
        return None


def headers(subject="owner", key=KEY):
    return {"Authorization": "Bearer " + key, "X-Wiki-Subject": subject}


def call(name="wiki_list", arguments=None):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name.removeprefix("wiki_"), "arguments": arguments or {}}}


async def post(client, subject="owner", name="wiki_list", arguments=None):
    response = await client.post("/gateway/mcp", headers=headers(subject), json=call(name, arguments))
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_gateway_is_disabled_by_default_and_weak_configuration_refuses_start(settings, service):
    with TestClient(create_app(settings, service, HumanVerifier())) as client:
        assert client.post("/gateway/mcp", json=call(), headers=headers()).status_code == 404
    with pytest.raises(ValueError, match="at least 32"):
        create_app(replace(settings, gateway_bearer_tokens="weak"), service, HumanVerifier())
    for value in ("", KEY + ",", KEY + ",weak"):
        with pytest.raises(ValueError, match="at least 32"):
            GatewayVerifier(value)


def test_gateway_credentials_and_identity_are_required_and_isolated(settings, service):
    settings = replace(settings, gateway_bearer_tokens=KEY + "," + ROTATED_KEY)
    app = create_app(settings, service, HumanVerifier())
    with TestClient(app, base_url=settings.public_url,
                    headers={"Accept": "application/json, text/event-stream"}) as client:
        for supplied in ({}, {"X-Wiki-Subject": "owner"},
                         {"Authorization": "Bearer " + KEY}, headers(key="human-oauth"),
                         headers(key="wrong-key"), headers(subject="agent:owner"),
                         headers(subject="client-machine"), headers(subject=" owner "),
                         headers(subject="owner,stranger"), headers(subject="x" * 256)):
            assert client.post("/gateway/mcp", headers=supplied, json=call()).status_code == 401
        duplicate = [("Authorization", "Bearer " + KEY),
                     ("X-Wiki-Subject", "owner"), ("X-Wiki-Subject", "stranger")]
        assert client.post("/gateway/mcp", headers=duplicate, json=call()).status_code == 401
        for key in (KEY, ROTATED_KEY):
            assert client.post("/gateway/mcp", headers=headers(key=key), json=call()).status_code == 200
            assert client.post("/mcp", headers=headers(key=key), json=call()).status_code == 401
            assert client.get("/api/wikis", headers=headers(key=key)).status_code == 401
        assert client.post("/mcp", headers=headers(key="human-oauth"),
                           json={**call(), "params": {"name": "wiki_list", "arguments": {}}}).status_code == 200
        assert client.post("/gateway/mcp", headers=headers(key="human-oauth"), json=call()).status_code == 401


def test_gateway_validates_host_and_origin(settings, service):
    app = create_app(replace(settings, gateway_bearer_tokens=KEY,
                             gateway_allowed_hosts=("relay.example.test",)), service, HumanVerifier())
    with TestClient(app, base_url=settings.public_url,
                    headers={"Accept": "application/json, text/event-stream"}) as client:
        for host in ("attacker.example.test", "wiki.example.test.attacker.example.test"):
            assert client.post("/gateway/mcp", headers={**headers(), "Host": host}, json=call()).status_code == 421
        assert client.post("/gateway/mcp", headers={**headers(), "Origin": "https://attacker.example.test"},
                           json=call()).status_code == 403
        for supplied in (headers(), {**headers(), "Host": "relay.example.test:8234"},
                         {**headers(), "Origin": settings.public_url}):
            assert client.post("/gateway/mcp", headers=supplied, json=call()).status_code == 200


async def test_gateway_rechecks_grants_suspension_and_pending_changes_without_reconnecting(settings, service):
    await service.create("owner", "starter", "Starter")
    await service.create("owner", "private", "Private")
    await service.share("owner", "starter", "reader", "read")
    app = create_app(replace(settings, gateway_bearer_tokens=KEY), service, HumanVerifier())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_url,
                                     headers={"Accept": "application/json, text/event-stream"}) as client:
            # Reuse the same HTTP transport/key while interleaving distinct callers.
            for _ in range(3):
                owner, reader, stranger = await asyncio.gather(
                    post(client), post(client, "reader"), post(client, "stranger"))
                assert "private" in str(owner) and "starter" in str(owner)
                assert "starter" in str(reader) and "private" not in str(reader)
                assert "starter" not in str(stranger) and "private" not in str(stranger)
            page = {"wiki": "starter", "path": "wiki/index.md"}
            assert not (await post(client, "reader", "wiki_read", page)).get("isError")
            assert (await post(client, "reader", "wiki_read", {**page, "wiki": "private"}))["isError"]
            source = {"wiki": "starter", "filename": "note.txt",
                      "content_base64": base64.b64encode(b"Synthetic source").decode(), "submission_id": "read-denied"}
            assert (await post(client, "reader", "wiki_ingest", source))["isError"]
            assert service.state.jobs("starter") == []
            service.state.set_grant("starter", "reader", "read")  # pending backend projection
            assert (await post(client, "reader", "wiki_read", page))["isError"]
            service.state.projected("starter", "reader")
            assert not (await post(client, "reader", "wiki_read", page)).get("isError")
            await service.share("owner", "starter", "reader", "none")
            assert (await post(client, "reader", "wiki_read", page))["isError"]
            await service.share("owner", "starter", "reader", "read")
            service.state.set_user_status("reader", True)
            assert (await post(client, "reader", "wiki_read", page))["isError"]
            assert not (await post(client, "owner", "wiki_read", page)).get("isError")
