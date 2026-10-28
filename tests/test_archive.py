"""Archive barriers preserve evidence without reviving outstanding writes."""

import asyncio
import base64
import json
import sqlite3
import threading

import httpx
import pytest
from fastmcp import Client
from test_http_mcp import FixtureVerifier

from wiki_hub import cli
from wiki_hub.forgejo import ForgejoError
from wiki_hub.gitstore import ContentError
from wiki_hub.mcp import create_mcp
from wiki_hub.server import create_app
from wiki_hub.service import Denied
from wiki_hub.state import State


async def test_archive_preserves_content_grants_and_copy_policy(service, monkeypatch):
    await service.create("owner", "handbook", "Handbook")
    await service.create("owner", "current", "Current")
    await service.share("owner", "handbook", "writer", "write")
    await service.share("owner", "handbook", "reader", "read")
    job = await service.ingest("owner", "handbook", "note.txt", base64.b64encode(b"Workshop").decode())
    await asyncio.gather(*service.tasks)
    history = await service.history("owner", "handbook")
    page = await service.read_page("reader", "handbook", "wiki/workshop.md")
    grants = service.state.grants("handbook")
    events = []
    monkeypatch.setattr(service.forgejo, "set_archived", lambda *a: events.append(("archive", a[-1])))
    monkeypatch.setattr(service.forgejo, "revoke_copies", lambda *a: events.append(("revoke", a[-1])))
    archived = await service.archive("owner", "handbook", True)
    assert archived == {"wiki": "handbook", "status": "archived", "archived": True,
                        "read_only": True, "pending": False}
    assert events == [("archive", True), ("revoke", "owner"), ("revoke", "reader"), ("revoke", "writer")]
    assert [r["slug"] for r in await service.list_wikis("owner")] == ["current"]
    assert len(await service.list_wikis("owner", include_archived=True)) == 2
    assert await service.read_page("reader", "handbook", "wiki/workshop.md") == page
    assert await service.history("owner", "handbook") == history
    assert service.state.grants("handbook") == grants
    assert (await service.job("reader", job["id"]))["status"] == "completed"
    assert (await service.copy_access("writer", "handbook"))["role"] == "read"
    assert (await service.clone("writer", "handbook"))["write"] is False
    assert (await service.clone("reader", "handbook"))["write"] is False
    assert service.state.copies_allowed("owner", "handbook")
    for operation in (service.ingest("writer", "handbook", "new.txt", "bmV3"),
                      service.rollback("owner", "handbook", history[-1]["revision"])):
        with pytest.raises(Denied, match="read-only"):
            await operation
    with pytest.raises(ContentError, match="wiki reopen"):
        await service.create("owner", "handbook", "New title")
    assert (await service.clone("owner", "current"))["write"] is True
    await service.wiki_settings("owner", "handbook", False)
    await service.archive("owner", "handbook", False)
    assert events[-4:] == [("revoke", "owner"), ("revoke", "reader"), ("revoke", "writer"), ("archive", False)]
    assert service.state.grants("handbook") == grants
    assert not service.state.copies_allowed("owner", "handbook")
    assert service.allowed("writer", "handbook", "write")
    assert await service.history("owner", "handbook") == history


async def test_only_human_wiki_admins_archive(service):
    await service.create("owner", "handbook", "Handbook")
    agent = await service.add_agent("owner", "Reader workflow")
    await service.share("owner", "handbook", agent["id"], "contribute")
    for subject, role in (("reader", "read"), ("contributor", "contribute"), ("writer", "write")):
        await service.share("owner", "handbook", subject, role)
    for subject in ("stranger", "reader", "contributor", "writer", agent["id"]):
        for archived in (None, True, False):
            with pytest.raises(Denied):
                await service.archive(subject, "handbook", archived)
    await service.share("owner", "handbook", "delegate", "admin")
    await service.archive("delegate", "handbook", True)
    assert await service.pages(agent["id"], "handbook")
    with pytest.raises(Denied):
        await service.ingest(agent["id"], "handbook", "note.txt", "bmV3")
    await service.archive("delegate", "handbook", False)


async def test_agent_grants_can_be_prepared_while_archived_without_enabling_ingest(service):
    await service.create("owner", "handbook", "Handbook")
    agent = await service.add_agent("owner", "Source filing")
    await service.archive("owner", "handbook", True)
    await service.share("owner", "handbook", agent["id"], "contribute")
    assert service.state.role(agent["id"], "handbook") == "contribute"
    with pytest.raises(Denied, match="read-only"):
        await service.ingest(agent["id"], "handbook", "note.txt", "bmV3")
    await service.archive("owner", "handbook", False)
    assert service.allowed(agent["id"], "handbook", "contribute")


async def test_archived_agent_grant_still_obeys_its_owners_role_and_suspension(service):
    await service.create("owner", "handbook", "Handbook")
    await service.share("owner", "handbook", "reader", "read")
    service.state.add_agent("agent:reader", "reader", "Reader workflow", "synthetic-reader-token")
    await service.archive("owner", "handbook", True)
    await service.share("owner", "handbook", "agent:reader", "read")
    with pytest.raises(Denied):
        await service.share("owner", "handbook", "agent:reader", "contribute")
    assert service.state.role("agent:reader", "handbook") == "read"
    await service.share("owner", "handbook", "reader", "contribute")
    await service.share("owner", "handbook", "agent:reader", "contribute")
    await service.user_status("owner", "reader", True)
    with pytest.raises(Denied):
        await service.share("owner", "handbook", "agent:reader", "read")


@pytest.mark.parametrize("failure", ["set_archived", "revoke_copies"])
async def test_backend_failure_stays_read_only_across_restart_and_explicit_retry(service, monkeypatch, failure):
    await service.create("owner", "handbook", "Handbook")
    original = getattr(service.forgejo, failure)

    def unavailable(*_a):
        raise ForgejoError("Synthetic backend failure")

    monkeypatch.setattr(service.forgejo, failure, unavailable)
    with pytest.raises(ForgejoError):
        await service.archive("owner", "handbook", True)
    assert State(service.settings.state_dir).wiki("handbook")["status"] == "archiving"
    assert (await service.archive("owner", "handbook"))["pending"]
    assert (await service.clone("owner", "handbook"))["write"] is False
    monkeypatch.setattr(service.forgejo, failure, original)
    await service.archive("owner", "handbook", True)
    monkeypatch.setattr(service.forgejo, failure, unavailable)
    with pytest.raises(ForgejoError):
        await service.archive("owner", "handbook", False)
    assert State(service.settings.state_dir).wiki("handbook")["status"] == "reopening"
    with pytest.raises(Denied):
        service.allowed("owner", "handbook", "write")
    monkeypatch.setattr(service.forgejo, failure, original)
    await service.archive("owner", "handbook", False)
    assert service.allowed("owner", "handbook", "write")


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_quick_reopen_never_revives_running_or_queued_jobs(service, outcome):
    await service.create("owner", "handbook", "Handbook")
    before = await service.history("owner", "handbook")
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*_a):
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise RuntimeError("Synthetic model failure")

    service.runner = delayed
    running = await service.ingest("owner", "handbook", "one.txt", "b25l")
    await asyncio.wait_for(entered.wait(), 5)
    queued = await service.ingest("owner", "handbook", "two.txt", "dHdv")
    pending = tuple(service.tasks)
    try:
        await asyncio.wait_for(service.archive("owner", "handbook", True), 5)
        await service.archive("owner", "handbook", False)
        if outcome == "cancel":
            for task in pending:
                task.cancel()
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
    for job in (running, queued):
        result = await service.job("owner", job["id"])
        assert result["status"] == "interrupted"
        assert result["error"].startswith("Wiki archived.")
        assert not result["revision"]
    assert await service.history("owner", "handbook") == before
    assert (service.settings.workspace_dir / running["id"]).is_dir()


async def test_archive_waits_for_publication_but_not_for_model(service, monkeypatch):
    await service.create("owner", "handbook", "Handbook")
    entered, release = threading.Event(), threading.Event()
    publish = service.git.publish

    def held(*a):
        entered.set()
        assert release.wait(10)
        return publish(*a)

    monkeypatch.setattr(service.git, "publish", held)
    job = await service.ingest("owner", "handbook", "note.txt", "bm90ZQ==")
    archive = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        archive = asyncio.create_task(service.archive("owner", "handbook", True))
        await asyncio.sleep(0)
        assert not archive.done()
    finally:
        release.set()
        await asyncio.gather(*service.tasks)
        if archive:
            await archive
    assert (await service.job("owner", job["id"]))["status"] == "completed"


async def test_cancelled_backend_archive_cannot_overtake_reopen(service, monkeypatch):
    await service.create("owner", "handbook", "Handbook")
    entered, release = threading.Event(), threading.Event()
    events = []

    def blocked(_owner, _slug, archived):
        if archived:
            entered.set()
            assert release.wait(10)
        events.append(archived)

    monkeypatch.setattr(service.forgejo, "set_archived", blocked)
    archive = asyncio.create_task(service.archive("owner", "handbook", True))
    reopen = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        archive.cancel()
        reopen = asyncio.create_task(service.archive("owner", "handbook", False))
        await asyncio.sleep(0)
        assert not archive.done() and not reopen.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await archive
        if reopen:
            await reopen
    assert events == [True, False]
    assert service.state.wiki("handbook")["status"] == "ready"


@pytest.mark.parametrize("operation", ["clone", "rollback"])
async def test_cancelled_git_write_is_drained_before_archive(service, monkeypatch, operation):
    await service.create("owner", "handbook", "Handbook")
    entered, release = threading.Event(), threading.Event()
    events = []

    def blocked(*a, **kwargs):
        entered.set()
        assert release.wait(10)
        events.append(operation)
        if operation == "rollback":
            return {"revision": "b" * 40, "restored": "a" * 40}
        return {"url": "synthetic", "username": "synthetic", "token": "synthetic"}

    monkeypatch.setattr(service.forgejo, "set_archived", lambda *a: events.append("archive"))
    monkeypatch.setattr(service.forgejo if operation == "clone" else service.git,
                        "clone_credential" if operation == "clone" else "rollback", blocked)
    writing = asyncio.create_task(service.clone("owner", "handbook") if operation == "clone" else
                                  service.rollback("owner", "handbook", "a" * 40))
    archive = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        writing.cancel()
        archive = asyncio.create_task(service.archive("owner", "handbook", True))
        await asyncio.sleep(0)
        assert not writing.done() and not archive.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing
        if archive:
            await archive
    assert events == [operation, "archive"]


async def test_archive_http_validation_and_existing_mcp_session(settings, service):
    await service.create("owner", "handbook", "Handbook")
    app = create_app(settings, service, FixtureVerifier())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_url,
                                headers={"Authorization": "Bearer owner-token"}) as client:
        path = "/api/wikis/handbook/archive"
        for invalid in (None, 0, 1, "false", []):
            assert (await client.post(path, json={"archived": invalid})).status_code == 400
        async with Client(create_mcp(service, subject_provider=lambda: "owner")) as mcp:
            assert len((await mcp.call_tool("wiki_list", {})).data) == 1
            assert (await client.post(path, json={"archived": True})).status_code == 200
            assert (await mcp.call_tool("wiki_list", {})).data == []
            assert not (await mcp.call_tool("wiki_pages", {"wiki": "handbook"})).is_error
            result = await mcp.call_tool("wiki_ingest", {"wiki": "handbook", "filename": "note.txt",
                                                       "content_base64": "bmV3"}, raise_on_error=False)
            assert result.is_error
        assert (await client.get(path)).json()["archived"]
        assert len((await client.get("/api/wikis?include_archived=true")).json()) == 1
        assert (await client.get("/api/wikis?include_archived=1")).status_code == 400


def test_copied_state_keeps_archive_status_without_schema_changes(service, tmp_path):
    state = service.state
    state.add_wiki("handbook", "Handbook", "owner", "backend", 1)
    state.ready("handbook")
    state.set_grant("handbook", "reader", "read")
    state.projected("handbook", "reader")
    copied = tmp_path / "copy"
    copied.mkdir()
    with state.connect() as db, sqlite3.connect(copied / "wiki-hub.db") as target:
        schema = list(db.execute("SELECT sql FROM sqlite_master ORDER BY name"))
        db.backup(target)
    upgraded = State(copied)
    upgraded.archive_transition("handbook", "archiving")
    upgraded.archive_projected("handbook", True)
    assert State(copied).wiki("handbook")["status"] == "archived"
    assert state.wiki("handbook")["status"] == "ready"
    assert upgraded.grants("handbook") == state.grants("handbook")
    with upgraded.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT sql FROM sqlite_master ORDER BY name")] == [
            tuple(r) for r in schema]


@pytest.mark.parametrize("action", ["archive", "reopen"])
def test_cli_archive_uses_human_api(tmp_path, monkeypatch, capsys, action):
    store = cli.CredentialStore(tmp_path / "config")
    store.keyring = None
    store.set_server("https://wiki.example.test")
    store.put(cli.oauth_key(store.server()), {"access_token": "synthetic-secret", "expires_at": 1e20})

    def handle(request):
        assert request.method == "POST" and request.url.path == "/api/wikis/handbook/archive"
        assert json.loads(request.content) == {"archived": action == "archive"}
        return httpx.Response(200, json={"wiki": "handbook"})

    client = cli.APIClient(store.server(), store, httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(cli, "CredentialStore", lambda: store)
    monkeypatch.setattr(cli, "APIClient", lambda *_a: client)
    assert cli.main([action, "handbook"]) == 0
    assert "synthetic-secret" not in capsys.readouterr().out
