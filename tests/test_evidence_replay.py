import asyncio
import base64

import pytest

from wiki_hub.gitstore import ContentError
from wiki_hub.service import Denied


async def test_pinned_page_reference_and_original_survive_update_and_rollback(service):
    await service.create("owner", "starter", "Starter")
    baseline = (await service.history("owner", "starter"))[0]["revision"]
    original = b"The workshop opens at 09:30."
    first = await service.ingest("owner", "starter", "first.txt", base64.b64encode(original).decode())
    await asyncio.gather(*service.tasks)
    revision = (await service.job("owner", first["id"]))["revision"]
    page = await service.read_page("owner", "starter", "wiki/workshop.md", revision)
    reference = f"reference/{first['id']}.txt"
    raw = f"raw/{first['id']}-first.txt"
    await service.ingest("owner", "starter", "second.txt", base64.b64encode(b"A dated correction").decode())
    await asyncio.gather(*service.tasks)
    assert (await service.read_page("owner", "starter", "wiki/workshop.md", revision)) == page
    await service.rollback("owner", "starter", baseline)
    assert (await service.read_page("owner", "starter", "wiki/workshop.md", revision)) == page
    assert "09:30" in (await service.read_page("owner", "starter", reference, revision))["content"]
    row = service.state.wiki("starter")
    assert service.git.original(row, raw, revision).read_bytes() == original
    await service.share("owner", "starter", "reader", "read")
    assert (await service.read_page("reader", "starter", "wiki/workshop.md", revision)) == page
    await service.share("owner", "starter", "reader", "none")
    with pytest.raises(Denied):
        await service.read_page("reader", "starter", "wiki/workshop.md", revision)


@pytest.mark.parametrize("revision", ["HEAD", "main", "a" * 7, "f" * 40, "a" * 40 + ":wiki/index.md"])
async def test_revision_must_be_a_full_accepted_commit(service, revision):
    await service.create("owner", "starter", "Starter")
    with pytest.raises(ContentError):
        await service.read_page("owner", "starter", "wiki/index.md", revision)


async def test_unpublished_and_other_wiki_commits_are_not_evidence(service):
    await service.create("owner", "starter", "Starter")
    wiki = service.state.wiki("starter")
    repo, work, _ = service.git.worktree(wiki, "unpublished")
    (work / "wiki/private.md").write_text("Unpublished synthetic fact")
    service.git.run(work, "add", ".")
    service.git.run(work, "commit", "-m", "Unaccepted draft")
    draft = service.git.revision(work)
    with pytest.raises(ContentError):
        await service.read_page("owner", "starter", "wiki/private.md", draft)
    await service.create("owner", "other", "Other")
    foreign = (await service.history("owner", "other"))[0]["revision"]
    with pytest.raises(ContentError):
        await service.read_page("owner", "starter", "wiki/index.md", foreign)
