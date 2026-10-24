import importlib.util
import json
import stat
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def example(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "examples" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_configuration_generation_preserves_existing_secrets(tmp_path):
    setup = example("initialize")
    destination = tmp_path / ".env"
    setup.initialize(ROOT / ".env.example", destination)
    first = destination.read_text()
    fields = dict(line.split("=", 1) for line in first.splitlines() if "=" in line and not line.startswith("#"))
    assert all(len(fields[key]) >= 40 for key in setup.SECRET_FIELDS)
    setup.initialize(ROOT / ".env.example", destination)
    assert destination.read_text() == first
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_identity_setup_registers_only_scoped_public_clients_and_reuses_matching_state():
    setup = example("configure-identity")
    clients, permission, writes = {}, None, []

    def handle(request):
        nonlocal permission
        path = request.url.path
        if request.method == "GET":
            if path == "/api/users":
                return httpx.Response(200, json=[{"id": "synthetic-owner", "username": "operator", "isAdmin": True}])
            if path == "/api/signup/setup":
                return httpx.Response(404, json={"code": "setup_not_available"})
            if path == "/api/apis":
                return httpx.Response(200, json=[{"id": "api", "resource": "https://wiki.example.com/mcp"}])
            if path == "/api/apis/api":
                return httpx.Response(200, json={"permissions": [permission] if permission else []})
            if path == "/api/oidc/clients":
                return httpx.Response(200, json=list(clients.values()))
        payload = json.loads(request.content)
        writes.append((path, payload))
        if path == "/api/apis/api/permissions":
            permission = {"id": "permission", "key": "wiki:access", "name": "Use Wiki Hub"}
        elif path == "/api/oidc/clients":
            assert payload["isPublic"] is True and payload["pkceEnabled"] is True
            assert payload["skipConsent"] is False and "clientSecret" not in payload
            clients[payload["id"]] = payload
        elif "/clients/" in path:
            assert payload == {"userDelegatedAccess": True, "clientAccess": False,
                               "userDelegatedPermissionIds": ["permission"], "clientPermissionIds": []}
        else:
            pytest.fail("Unexpected identity setup operation")
        return httpx.Response(200, json={})

    client = httpx.Client(base_url="https://id.example.test", transport=httpx.MockTransport(handle))
    first = setup.configure(client, "https://wiki.example.com", "operator")
    assert first["owner_subject"] == "synthetic-owner"
    assert len(clients) == 3
    count = len([path for path, _ in writes if path == "/api/oidc/clients"])
    assert setup.configure(client, "https://wiki.example.com", "operator") == first
    assert len([path for path, _ in writes if path == "/api/oidc/clients"]) == count
    clients["wiki-chatgpt"]["callbackURLs"] = ["https://untrusted.example.test/callback"]
    with pytest.raises(setup.SetupError, match="configuration differs"):
        setup.configure(client, "https://wiki.example.com", "operator")


def test_identity_provider_error_does_not_disclose_response_body():
    setup = example("configure-identity")
    client = httpx.Client(base_url="https://id.example.test", transport=httpx.MockTransport(
        lambda _: httpx.Response(500, text="synthetic-provider-secret")))
    with pytest.raises(setup.SetupError) as error:
        setup.configure(client, "https://wiki.example.com", "operator")
    assert "synthetic-provider-secret" not in str(error.value)


def test_identity_listing_stops_at_the_last_populated_page():
    setup = example("configure-identity")
    seen = []

    def handle(request):
        page = int(request.url.params["pagination[page]"])
        seen.append(page)
        if page > 2:
            return httpx.Response(429)
        return httpx.Response(200, json={
            "data": [{"id": str(page)}],
            "pagination": {"currentPage": page, "totalPages": 2},
        })

    client = httpx.Client(base_url="https://id.example.test", transport=httpx.MockTransport(handle))
    assert setup.items(client, "/api/users") == [{"id": "1"}, {"id": "2"}]
    assert seen == [1, 2]


def test_first_owner_is_created_only_when_explicit_and_public_setup_is_checked():
    setup = example("configure-identity")
    users = []
    setup_checked = False

    def handle(request):
        nonlocal setup_checked
        if request.url.path == "/api/users":
            if request.method == "GET":
                return httpx.Response(200, json=users)
            payload = json.loads(request.content)
            assert str(uuid.UUID(payload["id"])) == payload["id"]
            assert payload["isAdmin"] is True and payload["disabled"] is False
            assert payload["email"] == "operator@example.invalid"
            users.append(payload)
            return httpx.Response(201, json=payload)
        if request.url.path == "/api/signup/setup":
            setup_checked = True
            return httpx.Response(200, json={})
        pytest.fail("No API or clients may be configured while public setup is open")

    client = httpx.Client(base_url="https://id.example.test", transport=httpx.MockTransport(handle))
    with pytest.raises(setup.SetupError, match="active administrator"):
        setup.configure(client, "https://wiki.example.com", "operator")
    assert users == []
    with pytest.raises(setup.SetupError, match="setup has not closed"):
        setup.configure(client, "https://wiki.example.com", "operator", create_owner=True,
                        owner_email="operator@example.invalid")
    assert len(users) == 1 and setup_checked


def test_enrollment_is_one_hour_private_and_rejects_the_short_provider_code(tmp_path):
    setup = example("configure-identity")
    target = tmp_path / "enrollment.txt"

    def handle(request):
        assert json.loads(request.content)["ttl"] == 3600
        return httpx.Response(200, json={"token": "AAAAAAAAAAAA"})

    client = httpx.Client(base_url="https://id.example.test", transport=httpx.MockTransport(handle))
    setup.enroll(client, "synthetic-owner", "https://id.example.test", target)
    assert target.read_text() == "https://id.example.test/lc/AAAAAAAAAAAA\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    invalid = httpx.Client(base_url="https://id.example.test", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"token": "AAAAAA"})))
    with pytest.raises(setup.SetupError, match="twelve-character"):
        setup.enroll(invalid, "synthetic-owner", "https://id.example.test", target)
    assert target.read_text() == "https://id.example.test/lc/AAAAAAAAAAAA\n"
