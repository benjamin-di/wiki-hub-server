import asyncio
import base64
import sqlite3

import pytest

from wiki_hub.forgejo import ForgejoError
from wiki_hub.gitstore import ContentError
from wiki_hub.service import Denied
from wiki_hub.state import State


async def test_roles_allow_only_their_intended_operations(service):
    await service.create("owner", "starter", "Starter")
    await service.create("owner", "private", "Private")
    await service.share("owner", "starter", "reader", "read")
    assert [r["slug"] for r in await service.list_wikis("reader")] == ["starter"]
    assert await service.pages("reader", "starter")
    assert await service.read_page("reader", "starter", "wiki/index.md")
    assert await service.history("reader", "starter")
    assert await service.clone("reader", "starter", write=False)
    for operation in (
        service.ingest("reader", "starter", "note.txt", base64.b64encode(b"Source").decode()),
        service.rollback("reader", "starter", "a" * 40),
        service.clone("reader", "starter", write=True),
        service.pages("reader", "private"),
        service.access("reader", "starter"),
        service.share("reader", "starter", "other", "write"),
    ):
        with pytest.raises(Denied):
            await operation
    await service.share("owner", "starter", "reader", "contribute")
    job = await service.ingest("reader", "starter", "note.txt", base64.b64encode(b"Source").decode())
    await asyncio.gather(*service.tasks)
    assert (await service.job("reader", job["id"]))["status"] == "completed"
    with pytest.raises(Denied):
        await service.clone("reader", "starter", write=True)
    await service.share("owner", "starter", "reader", "write")
    assert await service.clone("reader", "starter", write=True)
    revision = (await service.history("reader", "starter"))[-1]["revision"]
    assert await service.rollback("reader", "starter", revision)


async def test_removing_share_denies_every_read_path_and_job(service):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "contribute")
    job = await service.ingest("reader", "starter", "note.txt", base64.b64encode(b"Source").decode())
    await asyncio.gather(*service.tasks)
    await service.share("owner", "starter", "reader", "none")
    assert await service.list_wikis("reader") == []
    assert (await service.access("owner", "starter"))["grants"] == []
    for operation in (
        service.pages("reader", "starter"), service.read_page("reader", "starter", "wiki/index.md"),
        service.search("reader", "starter", "Source"), service.history("reader", "starter"),
        service.job("reader", job["id"]), service.clone("reader", "starter", write=False),
    ):
        with pytest.raises(Denied):
            await operation
    assert await service.pages("owner", "starter")


async def test_revocation_while_model_runs_prevents_publication(service):
    await service.create("owner", "starter", "Starter")
    before = await service.history("owner", "starter")
    await service.share("owner", "starter", "writer", "write")
    entered, release = asyncio.Event(), asyncio.Event()
    runner = service.runner

    async def blocked(*args):
        entered.set()
        await release.wait()
        await runner(*args)

    service.runner = blocked
    job = await service.ingest("writer", "starter", "note.txt", base64.b64encode(b"Source").decode())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await service.share("owner", "starter", "writer", "read")
    finally:
        release.set()
        await asyncio.gather(*service.tasks)
    assert (await service.job("owner", job["id"]))["status"] == "failed"
    assert await service.history("owner", "starter") == before


async def test_failed_git_projection_denies_api_and_survives_restart_until_retry(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "writer", "write")
    project = service.forgejo.set_collaborator

    def unavailable(*args):
        raise ForgejoError("Synthetic unavailable backend")

    monkeypatch.setattr(service.forgejo, "set_collaborator", unavailable)
    with pytest.raises(ForgejoError):
        await service.share("owner", "starter", "writer", "none")
    assert (await service.access("owner", "starter"))["grants"][0]["pending"] == 1
    assert State(service.settings.state_dir).role("writer", "starter") is None
    with pytest.raises(Denied):
        await service.clone("writer", "starter", write=False)
    monkeypatch.setattr(service.forgejo, "set_collaborator", project)
    await service.share("owner", "starter", "writer", "none")
    assert (await service.access("owner", "starter"))["grants"] == []


@pytest.mark.parametrize("recipient,role", [("owner", "none"), ("reader", "unknown"), ("a b", "read"), ("", "read")])
async def test_invalid_grants_never_reach_forgejo(service, monkeypatch, recipient, role):
    await service.create("owner", "starter", "Starter")

    def unexpected(*args):
        pytest.fail("Invalid grant reached backend")

    monkeypatch.setattr(service.forgejo, "set_collaborator", unexpected)
    with pytest.raises(ContentError):
        await service.share("owner", "starter", recipient, role)


def test_additive_schema_preserves_existing_owner_and_job(tmp_path):
    path = tmp_path / "wiki-hub.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE wikis(slug TEXT PRIMARY KEY, title TEXT, owner TEXT, repo_owner TEXT,
                               repo_id INTEGER, status TEXT, created TEXT);
            CREATE TABLE jobs(id TEXT PRIMARY KEY, wiki TEXT, subject TEXT, filename TEXT, source_path TEXT,
                instructions TEXT, status TEXT, revision TEXT, error TEXT, created TEXT, updated TEXT);
            INSERT INTO wikis VALUES('starter','Starter','owner','backend',1,'ready','earlier');
            INSERT INTO jobs VALUES('job','starter','owner','note.txt','/source','',
                                    'completed','abc',NULL,'then','then');
        """)
    state = State(tmp_path)
    assert state.list_wikis("owner")[0]["repo_owner"] == "backend"
    assert state.job("job")["revision"] == "abc"
    assert state.list_wikis("stranger") == []
    state.set_grant("starter", "reader", "read")
    assert state.list_wikis("reader") == []
    state.projected("starter", "reader")
    assert state.list_wikis("reader")[0]["slug"] == "starter"
