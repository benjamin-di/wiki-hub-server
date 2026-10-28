import asyncio
import base64
import sqlite3

import pytest
from test_gitstore import remote_edit

from wiki_hub.gitstore import GitError
from wiki_hub.search import SearchIndex
from wiki_hub.service import Denied


async def populated(service):
    await service.create("owner", "starter", "Starter")
    job = await service.ingest("owner", "starter", "source.txt", base64.b64encode(b"Opens 09:30").decode())
    await asyncio.gather(*service.tasks)
    return await service.job("owner", job["id"])


async def test_index_reuses_revision_and_tracks_direct_git_push_and_rollback(service, monkeypatch):
    job = await populated(service)
    texts = service.git.texts
    builds = []

    def counted(*args):
        builds.append(args[2])
        return texts(*args)

    monkeypatch.setattr(service.git, "texts", counted)
    first = await service.search("owner", "starter", "workshop")
    assert first["results"] and first["revision"] == job["revision"]
    assert (await service.search("owner", "starter", "workshop")) == first
    assert builds == [job["revision"]]
    remote = service.forgejo.directory / "starter.git"
    revision = remote_edit(service.git, remote, "wiki/workshop.md", "# Workshop\n\nNow opens 10:00.\n")
    changed = await service.search("owner", "starter", "10:00")
    assert changed["results"] and changed["revision"] == revision
    assert all(hit["revision"] == revision for hit in changed["results"])
    rollback = await service.rollback("owner", "starter", job["revision"])
    restored = await service.search("owner", "starter", "09:30")
    assert restored["results"] and restored["revision"] == rollback["revision"]
    assert builds == [job["revision"], revision, rollback["revision"]]


async def test_corrupt_index_falls_back_without_mislabelling_revision_and_rebuilds(service):
    job = await populated(service)
    assert (await service.search("owner", "starter", "09:30"))["results"]
    index = SearchIndex(service.settings, "starter")
    index.path.write_bytes(b"A synthetic damaged derived database")
    fallback = await service.search("owner", "starter", "09:30")
    assert fallback["results"] and fallback["revision"] == job["revision"]
    assert not index.path.exists()
    rebuilt = await service.search("owner", "starter", "09:30")
    assert rebuilt["results"] and rebuilt["revision"] == job["revision"]
    with sqlite3.connect(index.path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT revision FROM indexed_revision").fetchone()[0] == job["revision"]


async def test_rebuild_failure_cannot_undo_accepted_job_or_git_data(service, monkeypatch):
    job = await populated(service)

    def unavailable(*args):
        raise GitError("Synthetic batch read failure")

    monkeypatch.setattr(service.git, "texts", unavailable)
    result = await service.search("owner", "starter", "09:30")
    assert result["results"] and result["revision"] == job["revision"]
    assert (await service.job("owner", job["id"]))["status"] == "completed"
    assert (await service.history("owner", "starter"))[0]["revision"] == job["revision"]


async def test_warm_index_never_bypasses_current_permissions_or_wiki_isolation(service):
    await populated(service)
    await service.share("owner", "starter", "reader", "read")
    assert (await service.search("reader", "starter", "09:30"))["results"]
    await service.share("owner", "starter", "reader", "none")
    with pytest.raises(Denied):
        await service.search("reader", "starter", "09:30")
    await service.create("owner", "other", "Other")
    assert not SearchIndex(service.settings, "other").path.exists()
    with pytest.raises(Denied):
        await service.search("reader", "other", "09:30")
    assert not SearchIndex(service.settings, "other").path.exists()


async def test_batch_index_preserves_utf8_and_unusual_git_filenames(service):
    await service.create("owner", "starter", "Starter")
    repo = service.git.repo("starter")
    paths = ["wiki/space name.md", "reference/new\nline.txt", "wiki/café.md"]
    for name in paths:
        (repo / name).write_text("A café opens at 09:30.\n", encoding="utf-8")
    service.git.run(repo, "add", "--", *paths)
    service.git.run(repo, "commit", "-m", "Preserve synthetic unusual names")
    service.git.run(repo, "push", "origin", "HEAD:main")
    result = await service.search("owner", "starter", "café")
    assert {hit["path"] for hit in result["results"]} == set(paths)
    assert all("café opens" in hit["snippet"] for hit in result["results"])
    assert SearchIndex(service.settings, "starter").path.exists()
