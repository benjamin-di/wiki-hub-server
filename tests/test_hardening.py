import asyncio
import base64

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from wiki_hub.gitstore import ContentError
from wiki_hub.mcp import create_mcp
from wiki_hub.service import Denied, Service


def encoded(text):
    return base64.b64encode(text.encode()).decode()


async def ingest(service, wiki, name, text):
    job = await service.ingest("owner", wiki, name, encoded(text))
    await asyncio.gather(*service.tasks)
    return await service.job("owner", job["id"])


async def test_refused_undo_retains_no_worktree(service, settings):
    await service.create("owner", "starter", "Starter")
    first = await ingest(service, "starter", "one.txt", "First synthetic source")
    await ingest(service, "starter", "two.txt", "Second synthetic source")
    for _ in range(2):
        with pytest.raises(ContentError, match="wiki changed"):
            await service.undo_ingest("owner", first["id"])
    assert not [path for path in settings.workspace_dir.iterdir() if path.name.startswith("undo-")]


async def test_completed_ingest_removes_its_staged_source(service, settings):
    await service.create("owner", "starter", "Starter")
    job = await ingest(service, "starter", "note.txt", "Synthetic source")
    assert job["status"] == "completed"
    assert not (settings.state_dir / "sources" / job["id"]).exists()


async def test_ingest_refuses_new_files_under_raw_or_reference(settings, tmp_path):
    from conftest import LocalForgejo

    async def forging(work, _job, raw, reference):
        (work / "wiki/index.md").write_text(
            f"# Starter\n\nSee [forged](../reference/forged.txt) and [{raw}](../{raw}).\n")
        (work / "reference/forged.txt").write_text("Invented evidence.\n")

    service = Service(settings, LocalForgejo(tmp_path), runner=forging)
    await service.create("owner", "starter", "Starter")
    job = await ingest(service, "starter", "note.txt", "Synthetic source")
    assert job["status"] == "failed" and "added files" in job["error"]


async def test_list_wikis_drops_a_wiki_revoked_while_describing(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "read")
    describe = service.describe

    def revoke_during(slug, deadline):
        result = describe(slug, deadline)
        monkeypatch.setattr(service, "allowed", lambda *_args: (_ for _ in ()).throw(Denied("No access.")))
        return result

    monkeypatch.setattr(service, "describe", revoke_during)
    assert await service.list_wikis("reader") == []


async def test_remote_mcp_masks_unexpected_errors():
    class Failing:
        async def list_wikis(self, _subject):
            raise OSError("/private/state/sources/secret")

    async with Client(create_mcp(Failing(), subject_provider=lambda: "owner")) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool("wiki_list", {})
    assert "/private/state" not in str(error.value)


async def test_an_undecodable_git_file_name_does_not_break_pages_or_search(service):
    from test_retrieval import handbook

    await handbook(service)
    writer = service.forgejo.directory / "handbook-writer"
    blob = service.git.run(writer, "hash-object", "-w", "--stdin", input_data=b"# Cafe\n\naisle\n").decode().strip()
    name = b"wiki/caf\xe9.md"
    index = b"100644 blob " + blob.encode() + b"\t" + name + b"\0"
    service.git.run(writer, "update-index", "-z", "--index-info", input_data=index)
    service.git.run(writer, "commit", "-m", "Direct push with a Latin-1 file name")
    service.git.run(writer, "push", "origin", "HEAD:main")
    assert "wiki/travel.md" in {f["path"] for f in (await service.pages("owner", "handbook"))["files"]}
    assert "wiki/travel.md" in [h["path"] for h in (await service.search("owner", None, "aisle"))["results"]]
