import hashlib
import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from wiki_hub.gitstore import ContentError, GitError, GitStore


@pytest.fixture
def empty_store(tmp_path):
    if not shutil.which("git-lfs"):
        pytest.skip("Git LFS is required for repository integration tests.")
    remote = tmp_path / "remote.git"
    settings = SimpleNamespace(
        state_dir=tmp_path / "state",
        workspace_dir=tmp_path / "work",
        forgejo_username="test-user",
        forgejo_password="private-test-credential",
        upload_limit=4096,
    )
    forgejo = SimpleNamespace(repository_url=lambda owner, slug: remote.as_uri())
    store = GitStore(settings, forgejo)
    store.run(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    wiki = {"slug": "pilot", "title": "Pilot wiki", "repo_owner": "owner"}
    return store, wiki, remote


@pytest.fixture
def setup_store(empty_store):
    store, wiki, remote = empty_store
    store.seed(wiki)
    return store, wiki, remote


def remote_edit(store, remote, filename, content):
    writer = remote.parent / "writer"
    store.run(remote.parent, "clone", remote.as_uri(), str(writer))
    store.run(writer, "config", "user.name", "Test Writer")
    store.run(writer, "config", "user.email", "writer@example.invalid")
    target = writer / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    store.run(writer, "add", "--", filename)
    store.run(writer, "commit", "-m", "Laptop edit")
    store.run(writer, "push", "origin", "HEAD:main")
    return store.revision(writer)


def test_seed_retries_a_previously_rejected_initial_push(empty_store, monkeypatch):
    store, wiki, remote = empty_store
    run = store.run

    def reject_push(path, *args, **kwargs):
        if args[0] == "push":
            raise GitError("Test initial push rejected.")
        return run(path, *args, **kwargs)

    monkeypatch.setattr(store, "run", reject_push)
    with pytest.raises(GitError, match="initial push"):
        store.seed(wiki)
    repo = store.repo(wiki["slug"])
    assert store.revision(repo)
    assert not store.run(remote, "rev-parse", "--verify", "main", check=False)
    monkeypatch.setattr(store, "run", run)
    store.seed(wiki)
    assert store.revision(repo) == store.run(remote, "rev-parse", "main").decode().strip()


def test_publish_advances_only_accepted_canonical_tree(setup_store):
    store, wiki, remote = setup_store
    repo, work, base = store.worktree(wiki, "source-one")
    (work / "wiki/source.md").write_text("# A source\n", encoding="utf-8")
    revision = store.publish(wiki, repo, work, base, "test-owner", "source-one")
    assert store.revision(repo) == revision
    assert store.run(remote, "rev-parse", "main").decode().strip() == revision
    assert store.read(repo, "wiki/source.md") == b"# A source\n"
    assert "source-one" in store.run(repo, "log", "-1", "--format=%B").decode()
    store.cleanup(repo, work)
    assert not work.exists()
    assert store.run(repo, "status", "--porcelain") == b""


def test_publish_merges_concurrent_laptop_edits_and_accepted_root_instructions(setup_store):
    store, wiki, remote = setup_store
    repo, work, base = store.worktree(wiki, "source-two")
    (work / "wiki/source.md").write_text("A new source.\n", encoding="utf-8")
    remote_edit(store, remote, "AGENTS.md", "# Updated owner instructions\n")
    revision = store.publish(wiki, repo, work, base, "test-owner", "source-two")
    assert store.read(repo, "wiki/source.md") == b"A new source.\n"
    assert store.run(repo, "show", "HEAD:AGENTS.md") == b"# Updated owner instructions\n"
    assert len(store.run(repo, "rev-list", "--parents", "-1", revision).split()) == 3


def test_publish_conflict_preserves_job_and_never_overwrites_remote(setup_store):
    store, wiki, remote = setup_store
    repo, work, base = store.worktree(wiki, "conflicting")
    (work / "wiki/index.md").write_text("# Agent version\n", encoding="utf-8")
    accepted = remote_edit(store, remote, "wiki/index.md", "# Laptop version\n")
    with pytest.raises(GitError, match="Git merge failed"):
        store.publish(wiki, repo, work, base, "test-owner", "conflicting")
    assert store.run(remote, "rev-parse", "main").decode().strip() == accepted
    assert store.revision(repo) == base
    assert work.exists()
    assert store.run(work, "ls-files", "-u")
    assert "<<<<<<<" in (work / "wiki/index.md").read_text()


def test_failed_push_leaves_unpublished_commit_out_of_canonical_state(setup_store, monkeypatch):
    store, wiki, remote = setup_store
    repo, work, base = store.worktree(wiki, "failed-push")
    (work / "wiki/private-draft.md").write_text("Unaccepted\n", encoding="utf-8")
    run = store.run

    def reject_push(path, *args, **kwargs):
        if args[0] == "push":
            raise GitError("Test remote rejected push.")
        return run(path, *args, **kwargs)

    monkeypatch.setattr(store, "run", reject_push)
    with pytest.raises(GitError, match="rejected"):
        store.publish(wiki, repo, work, base, "test-owner", "failed-push")
    assert store.revision(repo) == base
    assert store.run(remote, "rev-parse", "main").decode().strip() == base
    assert "wiki/private-draft.md" not in store.list_files(repo)
    assert store.revision(work) != base
    assert (work / "wiki/private-draft.md").read_text() == "Unaccepted\n"


def test_accepted_push_returns_revision_when_canonical_refresh_fails(setup_store, monkeypatch):
    store, wiki, remote = setup_store
    repo, work, base = store.worktree(wiki, "accepted-refresh-failed")
    (work / "wiki/accepted.md").write_text("Accepted remotely\n", encoding="utf-8")
    run = store.run

    def reject_canonical_fetch(path, *args, **kwargs):
        if path == repo and args[0] == "fetch":
            raise GitError("Test refresh temporarily unavailable.")
        return run(path, *args, **kwargs)

    monkeypatch.setattr(store, "run", reject_canonical_fetch)
    revision = store.publish(wiki, repo, work, base, "test-owner", "accepted-refresh-failed")
    assert revision != base
    assert store.revision(repo) == base
    assert store.run(remote, "rev-parse", "main").decode().strip() == revision
    monkeypatch.setattr(store, "run", run)
    assert store.revision(store.sync(wiki)) == revision
    assert store.read(repo, "wiki/accepted.md") == b"Accepted remotely\n"


def test_rollback_creates_new_commit_and_keeps_history(setup_store):
    store, wiki, remote = setup_store
    repo, work, initial = store.worktree(wiki, "published-before-rollback")
    (work / "wiki/index.md").write_text("# Revised\n", encoding="utf-8")
    updated = store.publish(wiki, repo, work, initial, "test-owner", "published-before-rollback")
    store.cleanup(repo, work)
    result = store.rollback(wiki, initial, "test-owner")
    assert result["restored"] == initial
    assert result["revision"] not in {initial, updated}
    assert store.run(repo, "rev-parse", "HEAD^").decode().strip() == updated
    assert store.run(repo, "rev-parse", "HEAD^{tree}") == store.run(repo, "rev-parse", initial + "^{tree}")
    assert not list(store.settings.workspace_dir.iterdir())


def test_failed_rollback_push_preserves_canonical_and_recovery_worktree(setup_store, monkeypatch):
    store, wiki, remote = setup_store
    repo, work, initial = store.worktree(wiki, "before-failed-rollback")
    (work / "wiki/index.md").write_text("# Accepted\n", encoding="utf-8")
    updated = store.publish(wiki, repo, work, initial, "test-owner", "before-failed-rollback")
    store.cleanup(repo, work)
    run = store.run

    def reject_push(path, *args, **kwargs):
        if args[0] == "push":
            raise GitError("Test remote rejected rollback.")
        return run(path, *args, **kwargs)

    monkeypatch.setattr(store, "run", reject_push)
    with pytest.raises(GitError, match="rejected rollback"):
        store.rollback(wiki, initial, "test-owner")
    assert store.revision(repo) == updated
    assert store.read(repo, "wiki/index.md") == b"# Accepted\n"
    assert store.run(remote, "rev-parse", "main").decode().strip() == updated
    retained = list(store.settings.workspace_dir.iterdir())
    assert len(retained) == 1
    assert retained[0].name.startswith("rollback-")


def test_accepted_rollback_returns_revision_even_when_cleanup_needs_retry(setup_store, monkeypatch):
    store, wiki, remote = setup_store
    initial = store.revision(store.repo(wiki["slug"]))

    def failed_cleanup(*args):
        raise GitError("Test cleanup unavailable.")

    monkeypatch.setattr(store, "cleanup", failed_cleanup)
    result = store.rollback(wiki, initial, "test-owner")
    assert result["revision"] != initial
    assert store.run(remote, "rev-parse", "main").decode().strip() == result["revision"]
    assert list(store.settings.workspace_dir.iterdir())


def test_lfs_source_roundtrip_fetches_verified_bytes_without_dirtying_canonical(setup_store):
    store, wiki, remote = setup_store
    repo, work, base = store.worktree(wiki, "document")
    original = b"%PDF-1.7\n\x00\xffpreserved original document\n"
    (work / "raw/source.pdf").write_bytes(original)
    (work / "wiki/source.md").write_text("Source: raw/source.pdf\n", encoding="utf-8")
    store.publish(wiki, repo, work, base, "test-owner", "document")
    pointer = store.read(repo, "raw/source.pdf")
    digest = hashlib.sha256(original).hexdigest()
    assert pointer.startswith(b"version https://git-lfs.github.com/spec/v1\n")
    assert digest.encode() in pointer
    store.cleanup(repo, work)
    # Remove only this fixture's cache to prove that download uses remote LFS storage.
    shutil.rmtree(repo / ".git/lfs/objects")
    downloaded = store.original(wiki, "raw/source.pdf")
    assert downloaded.read_bytes() == original
    assert downloaded.parent != repo / "raw"
    assert store.run(repo, "status", "--porcelain") == b""


@pytest.mark.parametrize("invalid", ["root-staged", "symlink", "large", "encoding", "deleted-root"])
def test_validation_rejects_unsafe_changed_content(setup_store, invalid, tmp_path):
    store, wiki, remote = setup_store
    repo, work, base = store.worktree(wiki, "invalid")
    if invalid == "root-staged":
        (work / "AGENTS.md").write_text("Replaced instructions\n", encoding="utf-8")
        store.run(work, "add", "AGENTS.md")
    elif invalid == "symlink":
        (work / "wiki/link.md").symlink_to(tmp_path / "outside")
    elif invalid == "large":
        (work / "wiki/large.md").write_bytes(b"x" * (store.settings.upload_limit + 1))
    elif invalid == "encoding":
        (work / "wiki/encoding.md").write_bytes(b"\xff\xfe")
    else:
        (work / "AGENTS.md").unlink()
    with pytest.raises(ContentError):
        store.publish(wiki, repo, work, base, "test-owner", "invalid")
    assert store.revision(repo) == base
    assert work.exists()


def test_cleanup_cannot_remove_a_directory_outside_application_workspaces(setup_store, tmp_path):
    store, wiki, remote = setup_store
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    with pytest.raises(ContentError, match="application worktrees"):
        store.cleanup(store.repo(wiki["slug"]), unrelated)
    assert unrelated.exists()


def test_git_askpass_never_sends_credentials_to_a_repository_supplied_host(setup_store):
    store, wiki, remote = setup_store
    environment = {**os.environ, "WIKI_GIT_USER": "fixture-user", "WIKI_GIT_PASSWORD": "synthetic-secret",
                   "WIKI_GIT_USERNAME": "fixture-user", "WIKI_GIT_TOKEN": "synthetic-secret",
                   "WIKI_GIT_ALLOWED_URLS": json.dumps(["https://git.example.test"])}
    denied = subprocess.run([str(store.askpass), "Password for 'https://untrusted.example.test/data': "],
                            env=environment, capture_output=True, text=True)
    assert denied.returncode != 0
    assert "synthetic-secret" not in denied.stdout + denied.stderr
    allowed = subprocess.run([str(store.askpass), "Password for 'https://fixture-user@git.example.test/repo': "],
                             env=environment, capture_output=True, text=True)
    assert allowed.returncode == 0 and allowed.stdout.strip() == "synthetic-secret"


def test_nested_git_repository_cannot_publish_a_gitlink_as_wiki_content(setup_store):
    store, wiki, remote = setup_store
    repo, work, base = store.worktree(wiki, "nested-repository")
    nested = work / "wiki/embedded"
    store.run(work, "init", str(nested))
    store.run(nested, "config", "user.name", "Fixture")
    store.run(nested, "config", "user.email", "fixture@example.invalid")
    (nested / "note.md").write_text("A nested repository is not a wiki page.\n")
    store.run(nested, "add", "note.md")
    store.run(nested, "commit", "-m", "Synthetic nested repository")
    with pytest.raises(ContentError, match="regular files"):
        store.publish(wiki, repo, work, base, "test-owner", "nested-repository")
    assert store.run(remote, "rev-parse", "main").decode().strip() == base


def test_repository_lfs_override_cannot_redirect_privileged_downloads_or_uploads(setup_store):
    store, wiki, remote = setup_store
    repo = store.repo(wiki["slug"])
    (repo / ".lfsconfig").write_text(
        "[lfs]\nurl = https://untrusted.example.test/objects\n"
        "pushurl = https://untrusted.example.test/upload\n"
        "[remote \"origin\"]\nlfsurl = https://untrusted.example.test/remote\n"
    )
    store.forgejo.repository_url = lambda owner, slug: f"https://git.example.test/{owner}/{slug}.git"
    output = store.lfs(repo, wiki, "env").decode()
    assert "Endpoint=https://git.example.test/owner/pilot.git/info/lfs" in output
    assert "untrusted.example.test" not in output


def test_large_existing_original_download_uses_bounded_application_memory(setup_store):
    import tracemalloc

    store, wiki, remote = setup_store
    writer = remote.parent / "bulk-writer"
    store.run(remote.parent, "clone", remote.as_uri(), str(writer))
    store.run(writer, "config", "user.name", "Bulk source fixture")
    store.run(writer, "config", "user.email", "fixture@example.invalid")
    store.run(writer, "lfs", "install", "--local", "--skip-smudge", "--skip-repo")
    (writer / "raw").mkdir(exist_ok=True)
    block = bytes(range(256)) * 4096
    original = writer / "raw/large.bin"
    expected = hashlib.sha256()
    with original.open("wb") as stream:
        for _ in range(33):
            stream.write(block)
            expected.update(block)
    store.run(writer, "add", "raw/large.bin")
    store.run(writer, "commit", "-m", "Bulk original exceeding source submission limit")
    store.lfs(writer, wiki, "push", "origin", "HEAD")
    store.run(writer, "push", "origin", "HEAD:main")
    repo = store.repo(wiki["slug"])
    tracemalloc.start()
    try:
        downloaded = store.original(wiki, "raw/large.bin")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    assert downloaded.stat().st_size == 33 * len(block)
    with downloaded.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == expected.hexdigest()
    assert store.run(repo, "status", "--porcelain") == b""


@pytest.mark.parametrize("failure", ["mismatch", "backend"])
def test_failed_original_transfer_keeps_verified_file_and_removes_temporary_bytes(setup_store, monkeypatch, failure):
    store, wiki, _remote = setup_store
    repo, work, base = store.worktree(wiki, "verified-original")
    original = b"Exact original evidence\x00\xff\n"
    (work / "raw/source.bin").write_bytes(original)
    (work / "wiki/source.md").write_text("[Original](../raw/source.bin)\n")
    store.publish(wiki, repo, work, base, "test-owner", "verified-original")
    store.cleanup(repo, work)
    verified = store.original(wiki, "raw/source.bin")

    def failed(*_args, output_file, **_kwargs):
        output_file.write(b"partial or corrupt source")
        if failure == "backend":
            raise GitError("Synthetic failed source transfer")
        return b""

    monkeypatch.setattr(store, "lfs", failed)
    with pytest.raises((GitError, ContentError)):
        store.original(wiki, "raw/source.bin")
    assert verified.read_bytes() == original
    assert list(verified.parent.iterdir()) == [verified]


def test_git_commands_bound_packing_memory(empty_store):
    store, _wiki, remote = empty_store
    assert store.run(remote, "config", "--get", "pack.threads").strip() == b"2"
    assert store.run(remote, "config", "--get", "core.bigFileThreshold").strip() == b"16m"
