import json

import httpx
import pytest

from wiki_hub.forgejo import Forgejo, ForgejoError


def test_clone_token_uses_ordinary_owner_and_only_one_repository(settings):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/tokens"):
            return httpx.Response(201, json={"sha1": "synthetic-repository-token"})
        raise AssertionError(request.url)

    client = httpx.Client(base_url="https://git.example.test/api/v1", transport=httpx.MockTransport(handle))
    forgejo = Forgejo(settings, client)
    result = forgejo.clone_credential("wiki-owner", "starter")
    assert result == {"username": "wiki-owner", "token": "synthetic-repository-token",
                      "url": "https://git.example.test/wiki-owner/starter.git"}
    assert requests[0].url.path == "/api/v1/admin/users/wiki-owner/tokens"
    payload = json.loads(requests[0].content)
    assert payload["repositories"] == [{"owner": "wiki-owner", "name": "starter"}]
    assert payload["scopes"] == ["write:repository"]
    assert settings.forgejo_username not in result.values()
    assert settings.forgejo_password not in result.values()


def test_existing_external_repository_is_never_adopted(settings):
    client = httpx.Client(base_url="https://git.example.test/api/v1", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": 1, "private": False, "description": "Other repository"}),
    ))
    with pytest.raises(ForgejoError, match="outside wiki-hub"):
        Forgejo(settings, client).create_repository("owner", "starter")


def test_server_diagnostics_cannot_disclose_backend_response(settings):
    client = httpx.Client(base_url="https://git.example.test/api/v1", transport=httpx.MockTransport(
        lambda request: httpx.Response(500, text="diagnostic with synthetic-backend-secret"),
    ))
    with pytest.raises(ForgejoError) as error:
        Forgejo(settings, client).request("GET", "/user")
    assert "synthetic-backend-secret" not in str(error.value)


def test_shared_copy_token_belongs_to_recipient_and_is_repository_bound(settings):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.startswith("/api/v1/users/"):
            return httpx.Response(200, json={"id": 2})
        if request.url.path.endswith("/tokens"):
            return httpx.Response(201, json={"sha1": "synthetic-reader-token"})
        return httpx.Response(204)

    forgejo = Forgejo(settings, httpx.Client(
        base_url="https://git.example.test/api/v1", transport=httpx.MockTransport(handle),
    ))
    forgejo.set_collaborator("wiki-owner", "starter", "reader", "read")
    copy = forgejo.clone_credential("wiki-owner", "starter", write=False, subject="reader")
    assert copy["username"] != "wiki-owner"
    assert requests[1].method == "PUT"
    assert json.loads(requests[1].content) == {"permission": "read"}
    token = requests[-1]
    assert token.url.path == f"/api/v1/admin/users/{copy['username']}/tokens"
    assert json.loads(token.content)["repositories"] == [{"owner": "wiki-owner", "name": "starter"}]
    assert json.loads(token.content)["scopes"] == ["read:repository"]
    forgejo.set_collaborator("wiki-owner", "starter", "reader", "none")
    assert requests[-1].method == "DELETE"
    assert requests[-1].url.path.endswith("/collaborators/" + copy["username"])


def test_copy_revocation_covers_legacy_tokens_and_paginates_before_deleting(settings):
    calls = []
    pages = {
        1: [{"id": 1, "name": "wiki-copy-first", "repositories": [{"owner": "owner", "name": "starter"}]}],
        2: [{"id": 2, "name": "wiki-copy-other", "repositories": [{"owner": "owner", "name": "private"}]}],
        3: [{"id": 3, "name": "unmanaged-token", "repositories": None}],
        4: [{"id": 4, "name": "wiki-copy-legacy", "repositories": None}],
        5: [],
    }

    def handle(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=pages[int(request.url.params["page"])])
        assert len([r for r in calls if r.method == "GET"]) == 5
        return httpx.Response(204)

    forgejo = Forgejo(settings, httpx.Client(base_url="https://git.example.test/api/v1",
                                            transport=httpx.MockTransport(handle)))
    forgejo.revoke_copies("owner", "starter", "owner-subject")
    assert [r.url.path.rsplit("/", 1)[1] for r in calls if r.method == "DELETE"] == ["1", "4"]


def test_revoking_copies_of_a_nonexistent_backend_identity_does_not_create_an_account(settings):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(404)

    Forgejo(settings, httpx.Client(base_url="https://git.example.test/api/v1",
                                  transport=httpx.MockTransport(handle))).revoke_copies("owner", "starter", "absent")
    assert len(calls) == 1 and calls[0].method == "GET"
