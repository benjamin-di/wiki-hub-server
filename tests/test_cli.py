import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from wiki_hub import cli


@pytest.fixture
def store(tmp_path):
    instance = cli.CredentialStore(tmp_path / "settings")
    instance.keyring = None
    return instance


def test_device_login_pending_slow_down_then_search_and_refresh(store, monkeypatch, capsys):
    polls = 0
    waits = []
    refreshes = []

    def handle(request):
        nonlocal polls
        if request.url.path == "/api/config":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://id.example",
                    "client_id": "wiki-cli",
                    "resource": "https://wiki.example/mcp",
                    "scope": "openid offline_access",
                },
            )
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "device_authorization_endpoint": "https://id.example/device",
                    "token_endpoint": "https://id.example/token",
                },
            )
        body = parse_qs(request.content.decode())
        if request.url.path == "/device":
            assert body["resource"] == ["https://wiki.example/mcp"]
            return httpx.Response(
                200,
                json={
                    "device_code": "secret-device",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://id.example/activate",
                    "interval": 1,
                },
            )
        if request.url.path == "/token":
            assert body["client_id"] == ["wiki-cli"]
            assert body["resource"] == ["https://wiki.example/mcp"]
            if body["grant_type"] == ["refresh_token"]:
                refreshes.append(body["refresh_token"])
                return httpx.Response(200, json={"access_token": "secret-new-access", "expires_in": 300})
            polls += 1
            if polls == 1:
                return httpx.Response(400, json={"error": "authorization_pending"})
            if polls == 2:
                return httpx.Response(400, json={"error": "slow_down"})
            return httpx.Response(
                200, json={"access_token": "secret-access", "refresh_token": "secret-refresh", "expires_in": 300}
            )
        assert request.url.path == "/api/wikis/pilot/search"
        assert request.headers["Authorization"] == "Bearer secret-new-access"
        assert request.url.params["q"] == "opening hours"
        return httpx.Response(200, json=[{"path": "wiki/hours.md", "snippet": "opens at nine"}])

    monkeypatch.setattr(cli.time, "sleep", waits.append)
    client = cli.APIClient("https://wiki.example", store, httpx.Client(transport=httpx.MockTransport(handle)))
    client.login(open_browser=False)
    assert waits == [1, 1, 6]
    token = store.get(cli.oauth_key(client.server))
    token["expires_at"] = time.time() - 1
    store.put(cli.oauth_key(client.server), token)
    result = client.request("GET", "/api/wikis/pilot/search", params={"q": "opening hours"})
    assert result[0]["path"] == "wiki/hours.md"
    assert refreshes == [["secret-refresh"]]
    assert store.get(cli.oauth_key(client.server))["refresh_token"] == "secret-refresh"
    output = capsys.readouterr()
    assert "ABCD-EFGH" in output.out
    assert "Signed in." in output.out
    assert "secret-" not in output.out + output.err
    if os.name != "nt":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_cli_search_uses_authenticated_api(store, monkeypatch, capsys):
    store.set_server("https://wiki.example")
    store.put(cli.oauth_key(store.server()), {"access_token": "secret-token", "expires_at": time.time() + 1000})

    def handle(request):
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.url.params["q"] == "ferry"
        return httpx.Response(200, json=[{"path": "wiki/transport.md", "snippet": "Take the ferry."}])

    client = cli.APIClient(store.server(), store, httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(cli, "CredentialStore", lambda: store)
    monkeypatch.setattr(cli, "APIClient", lambda *args: client)
    assert cli.main(["search", "pilot", "ferry"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)[0]["snippet"] == "Take the ferry."
    assert "secret-token" not in output.out + output.err


def test_failed_api_response_does_not_print_body_with_secrets(store):
    store.put(cli.oauth_key("https://wiki.example"), {"access_token": "private", "expires_at": time.time() + 1000})
    client = cli.APIClient(
        "https://wiki.example",
        store,
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500, text="internal token private"))),
    )
    with pytest.raises(cli.CLIError, match="HTTP 500") as exc:
        client.request("GET", "/api/wikis")
    assert "private" not in str(exc.value)


def test_cli_download_preserves_original_bytes_without_disclosing_credentials(store, monkeypatch, capsys, tmp_path):
    store.set_server("https://wiki.example")
    store.put(cli.oauth_key(store.server()), {"access_token": "secret-token", "expires_at": time.time() + 1000})
    original = b"%PDF-1.7\n\x00\xff\x80\r\noriginal document\n"

    def handle(request):
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.url.path == "/api/wikis/pilot/document"
        assert request.url.params["path"] == "raw/synthetic source.pdf"
        return httpx.Response(200, content=original)

    client = cli.APIClient(store.server(), store, httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(cli, "CredentialStore", lambda: store)
    monkeypatch.setattr(cli, "APIClient", lambda *args: client)
    target = tmp_path / "downloads/source.pdf"
    assert cli.main(["docs", "get", "pilot", "raw/synthetic source.pdf", "--output", str(target)]) == 0
    assert target.read_bytes() == original
    with pytest.raises(cli.CLIError, match="already exists"):
        client.download("pilot", "raw/synthetic source.pdf", target)
    assert target.read_bytes() == original
    output = capsys.readouterr()
    assert "secret-token" not in output.out + output.err


def test_download_refreshes_an_expired_server_session(store, tmp_path):
    server = "https://wiki.example"
    store.put(cli.oauth_key(server), {
        "access_token": "stale-access", "refresh_token": "secret-refresh", "expires_at": time.time() + 1000,
        "token_endpoint": "https://id.example/token", "client_id": "wiki-cli", "resource": server + "/mcp",
    })
    attempts = []

    def handle(request):
        if request.url.path == "/token":
            assert parse_qs(request.content.decode())["grant_type"] == ["refresh_token"]
            return httpx.Response(200, json={"access_token": "fresh-access", "expires_in": 300})
        attempts.append(request.headers["Authorization"])
        return httpx.Response(401) if len(attempts) == 1 else httpx.Response(200, content=b"original")

    client = cli.APIClient(server, store, httpx.Client(transport=httpx.MockTransport(handle)))
    target = tmp_path / "original.txt"
    client.download("pilot", "raw/source.txt", target)
    assert target.read_bytes() == b"original"
    assert attempts == ["Bearer stale-access", "Bearer fresh-access"]


def test_interrupted_download_removes_the_incomplete_file(store, tmp_path):
    class InterruptedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"incomplete document"
            raise httpx.ReadError("connection interrupted")

    server = "https://wiki.example"
    store.put(cli.oauth_key(server), {"access_token": "access", "expires_at": time.time() + 1000})
    client = cli.APIClient(server, store, httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=InterruptedStream())
    )))
    target = tmp_path / "source.pdf"
    with pytest.raises(httpx.ReadError):
        client.download("pilot", "raw/source.pdf", target)
    assert not target.exists()


def git(directory, *arguments):
    return subprocess.run(["git", *arguments], cwd=directory, text=True, check=True, capture_output=True).stdout.strip()


@pytest.fixture
def copies(tmp_path, store):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    writer = tmp_path / "writer"
    writer.mkdir()
    git(writer, "init", "--initial-branch=main")
    git(writer, "config", "user.name", "Test Writer")
    git(writer, "config", "user.email", "writer@example.invalid")
    (writer / "notes.md").write_text("Opening time: nine\n")
    git(writer, "add", "notes.md")
    git(writer, "commit", "-m", "Initial page")
    git(writer, "remote", "add", "origin", str(remote))
    git(writer, "push", "-u", "origin", "main")
    local = tmp_path / "local"
    git(tmp_path, "clone", str(remote), str(local))
    git(local, "config", "user.name", "Test Reader")
    git(local, "config", "user.email", "reader@example.invalid")
    store.put(cli.repo_key(local), {"url": remote.as_uri(), "username": "test-user", "token": "secret-repo-token"})
    return remote, writer, local


def test_sync_commits_local_edits_and_merges_remote_edits(copies, store, capsys):
    remote, writer, local = copies
    (writer / "remote.md").write_text("Remote page\n")
    git(writer, "add", "remote.md")
    git(writer, "commit", "-m", "Remote edit")
    git(writer, "push")
    (local / "local.md").write_text("Local page\n")
    cli.sync(local, store)
    assert (local / "remote.md").read_text() == "Remote page\n"
    assert git(local, "status", "--porcelain") == ""
    assert git(remote, "show", "main:local.md") == "Local page"
    assert git(remote, "show", "main:remote.md") == "Remote page"
    assert "secret-repo-token" not in capsys.readouterr().out


def test_read_only_sync_fetches_without_committing_or_publishing_local_edits(copies, store, capsys):
    remote, writer, local = copies
    credential = store.get(cli.repo_key(local))
    credential["write"] = False
    store.put(cli.repo_key(local), credential)
    (writer / "remote.md").write_text("Updated source-backed page\n")
    git(writer, "add", "remote.md")
    git(writer, "commit", "-m", "Remote update")
    git(writer, "push")
    accepted = git(remote, "rev-parse", "main")
    (local / "private-note.md").write_text("This local note must remain unpublished.\n")
    cli.sync(local, store)
    assert git(local, "rev-parse", "HEAD") == accepted
    assert git(remote, "rev-parse", "main") == accepted
    assert (local / "remote.md").read_text() == "Updated source-backed page\n"
    assert "private-note.md" in git(local, "status", "--porcelain")
    assert "Read-only local copy updated" in capsys.readouterr().out


def test_sync_stops_on_conflict_without_pushing(copies, store):
    remote, writer, local = copies
    (writer / "notes.md").write_text("Opening time: ten\n")
    git(writer, "add", "notes.md")
    git(writer, "commit", "-m", "Remote change")
    git(writer, "push")
    original_remote = git(remote, "rev-parse", "main")
    (local / "notes.md").write_text("Opening time: eleven\n")
    with pytest.raises(cli.CLIError, match="Git stopped"):
        cli.sync(local, store)
    assert git(remote, "rev-parse", "main") == original_remote
    assert "<<<<<<<" in (local / "notes.md").read_text()
    with pytest.raises(cli.CLIError, match="existing merge conflicts"):
        cli.sync(local, store)


def test_git_credentials_use_ephemeral_askpass_not_arguments(tmp_path, monkeypatch):
    observed = {}

    def run(arguments, **kwargs):
        observed.update(arguments=arguments, **kwargs)
        helper = Path(kwargs["env"]["GIT_ASKPASS"])
        assert helper.exists()
        assert "secret-repo-token" not in helper.read_text()
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.git_run(["fetch", "origin"], tmp_path, {"url": "https://git.example.test/repo",
                                            "username": "test-user", "token": "secret-repo-token"})
    assert "secret-repo-token" not in " ".join(observed["arguments"])
    assert observed["env"]["WIKI_GIT_TOKEN"] == "secret-repo-token"
    assert not Path(observed["env"]["GIT_ASKPASS"]).exists()


def test_client_askpass_refuses_foreign_lfs_hosts(tmp_path, monkeypatch):
    native_run = subprocess.run

    def fake_git(arguments, **kwargs):
        helper = Path(kwargs["env"]["GIT_ASKPASS"]).parent / "askpass.py"
        denied = native_run([sys.executable, str(helper), "Password for 'https://untrusted.example.test': "],
                            env=kwargs["env"], text=True, capture_output=True)
        assert denied.returncode != 0 and "synthetic-secret" not in denied.stdout + denied.stderr
        allowed = native_run([sys.executable, str(helper), "Password for 'https://git.example.test': "],
                             env=kwargs["env"], text=True, capture_output=True)
        assert allowed.returncode == 0 and allowed.stdout.strip() == "synthetic-secret"
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_git)
    cli.git_run(["fetch", "origin"], tmp_path, {"url": "https://git.example.test/repo",
                                             "username": "fixture-user", "token": "synthetic-secret"})
