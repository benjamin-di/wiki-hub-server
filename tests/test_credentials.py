"""Exercise WinVault's byte limit and interrupted credential updates."""

import json

import pytest

from wiki_hub import cli


class LimitedKeyring:
    def __init__(self):
        self.records = {}
        self.fail_after = None

    def get_password(self, service, username):
        return self.records.get((service, username))

    def set_password(self, service, username, password):
        if len(password.encode("utf-16-le")) > 2560:
            raise ValueError("Credential blob is too large")
        if self.fail_after is not None:
            if self.fail_after == 0:
                error = OSError("A backend message containing secret-token")
                error.winerror = 1312
                raise error
            self.fail_after -= 1
        self.records[service, username] = password

    def delete_password(self, service, username):
        self.records.pop((service, username), None)


@pytest.fixture
def store(tmp_path, monkeypatch):
    instance = cli.CredentialStore(tmp_path)
    instance.keyring = LimitedKeyring()
    monkeypatch.setattr(cli, "WINDOWS", True)
    return instance


def test_large_oauth_survives_reopen_refresh_and_other_credentials(store):
    key = cli.oauth_key("https://wiki.example")
    token = {"access_token": "a" * 1500, "id_token": "b" * 900, "refresh_token": "c" * 100, "label": "雪🌍"}
    with pytest.raises(ValueError, match="too large"):
        store.keyring.set_password("wiki-hub", key, json.dumps(token))
    store.put(key, token)
    reopened = cli.CredentialStore(store.directory)
    reopened.keyring = store.keyring
    assert reopened.get(key) == token
    old_services = {s for s, _ in store.keyring.records if s != "wiki-hub"}
    token.update(access_token="d" * 1900, refresh_token="e" * 100)
    reopened.put(key, token)
    reopened.put("git:synthetic", {"username": "reader", "token": "git-secret"})
    reopened.put("agent:synthetic", {"access_token": "agent-secret"})
    assert reopened.get(key) == token
    assert reopened.get("git:synthetic")["token"] == "git-secret"
    assert reopened.get("agent:synthetic")["access_token"] == "agent-secret"
    assert not old_services.intersection(s for s, _ in store.keyring.records)
    assert not json.loads(store.path.read_text()).get("credentials")


@pytest.mark.parametrize("successful_writes", [0, 1, 3])
def test_interrupted_replacement_preserves_previous_credential(store, successful_writes):
    key = "oauth:synthetic"
    original = {"access_token": "original" * 400}
    store.put(key, original)
    before = dict(store.keyring.records)
    store.keyring.fail_after = successful_writes
    # Three chunks followed by the index: fail either a chunk or the index.
    with pytest.raises(cli.CLIError, match="Windows error 1312") as caught:
        store.put(key, {"access_token": "replacement" * 220})
    assert "secret-token" not in str(caught.value)
    assert store.keyring.records == before
    assert store.get(key) == original
    assert "replacement" not in store.path.read_text()


def test_old_record_upgrades_and_small_replacement_cleans_chunks(store):
    key = "oauth:synthetic"
    store.keyring.set_password("wiki-hub", key, json.dumps({"access_token": "legacy"}))
    assert store.get(key) == {"access_token": "legacy"}
    store.put(key, {"access_token": "large" * 500})
    assert len(store.keyring.records) > 1
    store.put(key, {"access_token": "small"})
    assert store.get(key) == {"access_token": "small"}
    assert len(store.keyring.records) == 1


def test_missing_chunk_never_returns_partial_token(store):
    key = "oauth:synthetic"
    store.put(key, {"access_token": "large" * 500})
    service = next(s for s, _ in store.keyring.records if s != "wiki-hub")
    store.keyring.delete_password(service, "credential")
    assert store.get(key) is None


def test_windows_without_backend_never_writes_plaintext(store):
    store.keyring = None
    with pytest.raises(cli.CLIError, match="No credential-manager backend"):
        store.put("oauth:synthetic", {"access_token": "secret"})
    assert not store.path.exists()
