"""Real Windows Credential Manager acceptance; synthetic credentials only."""

import json
import os
import time
import uuid

import httpx
import pytest

from wiki_hub import cli

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires native Windows Credential Manager")


def test_native_device_login_reopen_refresh_and_git_credential(tmp_path, monkeypatch):
    import keyring
    from keyring.backends.Windows import WinVaultKeyring, pywintypes

    backend = WinVaultKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    server = "https://" + uuid.uuid4().hex + ".example.test"
    key = cli.oauth_key(server)
    git_key = "git:" + uuid.uuid4().hex
    token = {"access_token": "a" * 1500, "id_token": "b" * 900, "refresh_token": "refresh-test", "expires_in": 300}
    store = cli.CredentialStore(tmp_path)

    def handle(request):
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"issuer": server, "client_id": "cli-test", "resource": server + "/mcp"})
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"device_authorization_endpoint": server + "/device",
                                            "token_endpoint": server + "/token"})
        if request.url.path == "/device":
            return httpx.Response(200, json={"device_code": "synthetic-device", "user_code": "TEST",
                                            "verification_uri": server + "/activate", "interval": 1})
        if request.url.path == "/token":
            if b"grant_type=refresh_token" in request.content:
                return httpx.Response(200, json={"access_token": "new" * 700, "refresh_token": "rotated-test",
                                                "expires_in": 300})
            return httpx.Response(200, json=token)
        assert request.headers["Authorization"] == "Bearer " + "new" * 700
        return httpx.Response(200, json={"ok": True})

    try:
        # Demonstrate the original bug on the actual Windows backend.
        with pytest.raises((ValueError, pywintypes.error)):
            backend.set_password("wiki-hub", key, json.dumps(token))
        monkeypatch.setattr(cli.time, "sleep", lambda _: None)
        transport = httpx.MockTransport(handle)
        cli.APIClient(server, store, httpx.Client(transport=transport)).login(open_browser=False)
        reopened = cli.CredentialStore(tmp_path)
        assert reopened.get(key)["access_token"] == token["access_token"]
        saved = reopened.get(key)
        saved["expires_at"] = time.time() - 1
        reopened.put(key, saved)
        client = cli.APIClient(server, reopened, httpx.Client(transport=transport))
        assert client.request("GET", "/probe") == {"ok": True}
        reopened.put(git_key, {"username": "test", "token": "synthetic-git-secret"})
        assert cli.CredentialStore(tmp_path).get(key)["refresh_token"] == "rotated-test"
        assert cli.CredentialStore(tmp_path).get(git_key)["token"] == "synthetic-git-secret"
        assert "access_token" not in reopened.path.read_text()
    finally:
        for name in (key, git_key):
            existing = backend.get_password("wiki-hub", name)
            if existing:
                store._delete_chunks(store._chunk_services(name, json.loads(existing)))
                backend.delete_password("wiki-hub", name)
