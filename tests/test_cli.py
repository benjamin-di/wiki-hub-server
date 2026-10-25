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
    class MemoryKeyring:
        values = {}

        def get_password(self, service, name):
            return self.values.get((service, name))

        def set_password(self, service, name, value):
            self.values[service, name] = value

        def delete_password(self, service, name):
            self.values.pop((service, name), None)

    instance.keyring = MemoryKeyring()
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
def copies(tmp_path, store, monkeypatch):
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
    git(local, "config", "wikihub.server", "https://wiki.example")
    git(local, "config", "wikihub.wiki", "pilot")
    store.put(cli.repo_key(local), {"url": str(remote), "username": "test-user", "token": "secret-repo-token"})

    def policy(self, method, path):
        assert method == "GET" and path == "/api/wikis/pilot/copy-access"
        return {"wiki": "pilot", "role": "write", "local_copies": True}

    monkeypatch.setattr(cli.APIClient, "request", policy)
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


def test_sync_keeps_excluded_application_work_local(copies, store):
    remote, _, local = copies
    (local / ".autocommit-pathspec").write_text(".\n:(exclude,glob)*.py\n")
    (local / "held.py").write_text("print('unreleased application')\n")
    (local / "page.md").write_text("Accepted local knowledge\n")
    cli.sync(local, store)
    assert git(remote, "show", "main:page.md") == "Accepted local knowledge"
    assert "held.py" not in git(remote, "ls-tree", "--name-only", "main").splitlines()
    assert "held.py" in git(local, "status", "--porcelain")
    git(local, "add", "held.py")
    before = git(remote, "rev-parse", "main")
    with pytest.raises(cli.CLIError, match="staged changes are excluded"):
        cli.sync(local, store)
    assert git(remote, "rev-parse", "main") == before
    assert git(local, "diff", "--cached", "--name-only") == "held.py"


def test_sync_installs_local_lfs_filters_and_transfers_original_bytes(copies, store):
    import hashlib

    remote, _, local = copies
    original = b"Original binary evidence\x00\xff\x80\n"
    (local / ".gitattributes").write_text("raw/** filter=lfs diff=lfs merge=lfs -text\n")
    (local / "raw").mkdir()
    (local / "raw/source.bin").write_bytes(original)
    cli.sync(local, store)
    oid = hashlib.sha256(original).hexdigest()
    assert f"oid sha256:{oid}" in git(remote, "show", "main:raw/source.bin")
    assert (remote / "lfs/objects" / oid[:2] / oid[2:4] / oid).read_bytes() == original
    assert (local / "raw/source.bin").read_bytes() == original


def test_sync_uses_current_role_without_upgrading_a_read_only_copy(copies, store, monkeypatch):
    remote, _, local = copies
    def request(self, method, path, **kwargs):
        if method == "GET":
            return {"role": "read", "local_copies": True}
        assert path.endswith("/clone") and kwargs["json"] == {"write": False}
        return {"url": str(remote), "username": "test-user", "token": "fresh-read-token", "write": False}

    monkeypatch.setattr(cli.APIClient, "request", request)
    before = git(remote, "rev-parse", "main")
    (local / "local.md").write_text("Local edits remain local after a downgrade\n")
    assert cli.sync(local, store) == "read-only"
    assert git(local, "rev-parse", "HEAD") == before
    assert "local.md" in git(local, "status", "--porcelain")
    assert git(remote, "rev-parse", "main") == before


def test_sync_archive_and_reopen_refresh_credentials_without_losing_local_edits(copies, store, monkeypatch):
    remote, _, local = copies
    role, renewed = "read", []

    def request(self, method, path, **kwargs):
        if method == "GET":
            return {"role": role, "local_copies": True}
        write = kwargs["json"]["write"]
        renewed.append(write)
        return {"url": str(remote), "username": "test-user", "token": "fresh-token", "write": write}

    monkeypatch.setattr(cli.APIClient, "request", request)
    before = git(remote, "rev-parse", "main")
    (local / "local.md").write_text("Offline work must survive archival\n")
    assert cli.sync(local, store) == "read-only"
    assert git(remote, "rev-parse", "main") == before
    assert store.get(cli.repo_key(local))["requested_write"] is True
    role = "admin"
    assert cli.sync(local, store) == "synced"
    assert git(remote, "show", "main:local.md") == "Offline work must survive archival"
    assert renewed == [False, True]
    # An explicit read-only clone stays read-only, including after renewal.
    credential = store.get(cli.repo_key(local))
    credential.update(write=False, requested_write=False)
    store.put(cli.repo_key(local), credential)
    (local / "other.md").write_text("Keep this local\n")
    assert cli.sync(local, store, refresh_credential=True) == "read-only"
    assert renewed == [False, True, False]
    assert "other.md" in git(local, "status", "--porcelain")


def test_sync_refuses_changed_credential_remote_before_committing(copies, store, monkeypatch):
    remote, _, local = copies
    def request(self, method, path, **kwargs):
        if method == "GET":
            return {"role": "write", "local_copies": True}
        return {"url": "https://other.example.test/other.git", "write": True}

    monkeypatch.setattr(cli.APIClient, "request", request)
    (local / "local.md").write_text("Preserve without committing\n")
    before = git(remote, "rev-parse", "main")
    with pytest.raises(cli.CLIError, match="different Git-copy details"):
        cli.sync(local, store, refresh_credential=True)
    assert git(local, "rev-parse", "HEAD") == before
    assert "local.md" in git(local, "status", "--porcelain")


@pytest.mark.parametrize("response", [{"role": "write", "local_copies": False}, {"local_copies": True}])
def test_denied_or_invalid_copy_policy_does_not_commit_local_work(copies, store, monkeypatch, response):
    remote, _, local = copies
    monkeypatch.setattr(cli.APIClient, "request", lambda *a: response)
    (local / "local.md").write_text("Do not publish without fresh permission\n")
    before = git(local, "rev-parse", "HEAD")
    with pytest.raises(cli.CLIError, match="Local work was preserved"):
        cli.sync(local, store)
    assert git(local, "rev-parse", "HEAD") == git(remote, "rev-parse", "main") == before
    assert git(local, "diff", "--cached", "--name-only") == ""


def test_sync_does_not_publish_to_a_changed_push_remote(copies, store):
    remote, _, local = copies
    before = git(remote, "rev-parse", "main")
    git(local, "config", "remote.origin.pushurl", str(remote.parent / "different.git"))
    (local / "local.md").write_text("Preserve work\n")
    with pytest.raises(cli.CLIError, match="remote differs"):
        cli.sync(local, store)
    assert git(local, "rev-parse", "HEAD") == before
    assert git(local, "diff", "--cached", "--name-only") == ""


def test_sync_stops_during_a_rebase_or_on_a_different_branch(copies, store):
    _, _, local = copies
    state = local / ".git/rebase-merge"
    state.mkdir()
    try:
        with pytest.raises(cli.CLIError, match="existing Git operation"):
            cli.sync(local, store)
    finally:
        state.rmdir()
    git(local, "switch", "-c", "unrelated-work")
    with pytest.raises(cli.CLIError, match="on main tracking"):
        cli.sync(local, store)


def test_manual_sync_uses_the_same_lock_as_a_scheduled_run(copies, store):
    from wiki_hub.copy_lock import copy_lock

    _, _, local = copies
    with copy_lock(local / ".git/wiki-hub-sync.lock"):
        with pytest.raises(cli.CLIError, match="Another wiki sync"):
            cli.sync(local, store)
    assert cli.sync(local, store) == "synced"


def test_batch_sync_reports_each_failure_and_still_updates_other_copies(copies, store, monkeypatch, capsys):
    remote, _, local = copies
    broken = local.parent / "a-incomplete-copy"
    git(local.parent, "clone", str(remote), str(broken))
    git(broken, "config", "wikihub.server", "https://wiki.example")
    (local / "local.md").write_text("Batch sync must reach this copy\n")
    monkeypatch.setattr(cli, "CredentialStore", lambda: store)
    assert cli.main(["sync", "--all", "--root", str(local.parent), "--json"]) == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["ok"] is False
    assert [(Path(row["directory"]).name, row["status"]) for row in report["copies"]] == [
        (broken.name, "failed"), (local.name, "synced")]
    assert git(remote, "show", "main:local.md") == "Batch sync must reach this copy"
    assert "secret-repo-token" not in output.out + output.err


def test_batch_sync_ignores_unmanaged_repos_and_does_not_need_a_default_server(copies, store, monkeypatch, capsys):
    remote, _, local = copies
    unmanaged = local.parent / "ordinary-repository"
    git(local.parent, "clone", str(remote), str(unmanaged))
    (unmanaged / "do-not-commit.md").write_text("Managed elsewhere\n")
    monkeypatch.setattr(cli, "CredentialStore", lambda: store)
    assert cli.main(["sync", "--all", "--root", str(local.parent), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and len(report["copies"]) == 1
    assert git(unmanaged, "diff", "--cached", "--name-only") == ""
    assert "do-not-commit.md" in git(unmanaged, "status", "--porcelain")


@pytest.mark.parametrize("role,read_only", [("read", True), ("contribute", True), ("write", False), ("admin", False)])
def test_discovery_clones_once_with_current_role_and_reuses_a_renamed_copy(copies, store, tmp_path, monkeypatch, capsys,
                                                                         role, read_only):
    remote, _, _ = copies
    root = tmp_path / "discovered"
    root.mkdir()
    requests = []
    url = "https://git.example.test/operator/pilot.git"

    def request(self, method, path, **kwargs):
        requests.append((method, path))
        if path == "/api/wikis":
            return [{"slug": "pilot"}]
        if path.endswith("/copy-access"):
            return {"wiki": "pilot", "role": role, "local_copies": True}
        assert path.endswith("/clone") and kwargs["json"] == {"write": not read_only}
        return {"url": url, "username": "reader", "token": "secret-discovery", "write": not read_only}

    run = cli.git_run

    def local_clone(arguments, directory, credentials=None, **kwargs):
        if arguments[0] == "clone":
            assert credentials["write"] is not read_only
            result = run(["clone", str(remote), arguments[2]], directory)
            run(["config", "remote.origin.url", url], Path(arguments[2]))
            return result
        return run(arguments, directory, credentials, **kwargs)

    monkeypatch.setattr(cli.APIClient, "request", request)
    monkeypatch.setattr(cli, "git_run", local_clone)
    client = cli.APIClient("https://wiki.example", store)
    result = cli.discover_copies(root, client)
    assert [r["status"] for r in result] == ["cloned"]
    assert (root / "pilot/notes.md").read_text() == "Opening time: nine\n"
    assert store.get(cli.repo_key(root / "pilot"))["write"] is not read_only
    assert cli.copy_metadata(root / "pilot") == (client.server, "pilot")
    # Metadata, rather than a parallel registry or folder name, is authoritative.
    (root / "pilot").rename(root / "renamed-local-copy")
    before = len(requests)
    assert cli.discover_copies(root, client) == []
    assert requests[before:] == [("GET", "/api/wikis")]
    assert not (root / "pilot").exists()
    assert capsys.readouterr().out == ""


def test_discovery_preserves_existing_folders_and_continues_after_a_denial(tmp_path, store, monkeypatch):
    root = tmp_path / "copies"
    (root / "occupied").mkdir(parents=True)
    note = root / "occupied/personal-note.txt"
    note.write_text("Keep my local work\n")
    created = []

    def request(self, method, path):
        assert method == "GET"
        if path == "/api/wikis":
            return [{"slug": name} for name in ("occupied", "denied", "disabled", "valid")]
        if path.endswith("denied/copy-access"):
            raise cli.CLIError("HTTP 403")
        return {"role": "read", "local_copies": "/disabled/" not in path}

    monkeypatch.setattr(cli.APIClient, "request", request)
    monkeypatch.setattr(cli, "clone", lambda _client, slug, directory, **kwargs: created.append((slug, kwargs)))
    result = cli.discover_copies(root, cli.APIClient("https://wiki.example", store))
    assert [r["status"] for r in result] == ["failed", "failed", "skipped", "cloned"]
    assert created == [("valid", {"read_only": True, "quiet": True})]
    assert note.read_text() == "Keep my local work\n"
    assert list((root / "occupied").iterdir()) == [note]


def test_discovery_does_not_follow_an_existing_destination_symlink(tmp_path, store, monkeypatch):
    if os.name == "nt":
        pytest.skip("Unprivileged Windows symlinks require Developer Mode")
    target = tmp_path / "keep"
    target.mkdir()
    root = tmp_path / "copies"
    root.mkdir()
    (root / "pilot").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(cli.APIClient, "request", lambda *_args: [{"slug": "pilot"}])
    result = cli.discover_copies(root, cli.APIClient("https://wiki.example", store))
    assert result[0]["status"] == "failed" and "already exists" in result[0]["error"]
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("catalog", [[{"slug": "../escape"}], [{"slug": "pilot"}, {"slug": "BAD"}], [None], {}])
def test_invalid_discovery_catalog_creates_nothing(tmp_path, store, monkeypatch, catalog):
    monkeypatch.setattr(cli.APIClient, "request", lambda *_args: catalog)
    with pytest.raises(cli.CLIError, match="invalid wiki name"):
        cli.discover_copies(tmp_path, cli.APIClient("https://wiki.example", store))
    assert not (tmp_path / "pilot").exists()


def test_discovery_root_lock_prevents_overlapping_clone_attempts(tmp_path, store, monkeypatch):
    from wiki_hub.copy_lock import file_lock

    monkeypatch.setattr(cli.APIClient, "request", lambda *_args: pytest.fail("Must lock before requesting a catalog"))
    with file_lock(tmp_path / ".wiki-hub-discovery.lock"):
        with pytest.raises(cli.CLIError, match="Another discovery"):
            cli.discover_copies(tmp_path, cli.APIClient("https://wiki.example", store))


def test_failed_discovery_still_syncs_existing_copies_and_returns_valid_json(copies, store, monkeypatch, capsys):
    remote, _, local = copies
    original_request = cli.APIClient.request

    def request(self, method, path):
        if path == "/api/wikis":
            raise cli.CLIError("HTTP 503")
        return original_request(self, method, path)

    monkeypatch.setattr(cli.APIClient, "request", request)
    monkeypatch.setattr(cli, "CredentialStore", lambda: store)
    store.set_server("https://wiki.example")
    (local / "local.md").write_text("Offline work survives discovery failure\n")
    assert cli.main(["sync", "--all", "--discover", "--root", str(local.parent), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"]
    assert [r["status"] for r in report["copies"]] == ["synced", "failed"]
    assert git(remote, "show", "main:local.md") == "Offline work survives discovery failure"


def test_discovery_requires_explicit_batch_mode(store, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CredentialStore", lambda: store)
    assert cli.main(["sync", "--discover"]) == 1
    assert "require wiki sync --all" in capsys.readouterr().err


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


def test_timed_out_git_returns_a_safe_error_for_batch_reporting(tmp_path, monkeypatch):
    def timeout(arguments, **kwargs):
        raise subprocess.TimeoutExpired(arguments, kwargs["timeout"], stderr="private diagnostic")

    monkeypatch.setattr(cli.subprocess, "run", timeout)
    with pytest.raises(cli.CLIError, match="Git timed out") as error:
        cli.git_run(["fetch", "origin"], tmp_path)
    assert "private diagnostic" not in str(error.value)


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
