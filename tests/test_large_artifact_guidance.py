"""Read errors distinguish indexed text from static artifacts outside search."""

import pytest

from wiki_hub.gitstore import ContentError


@pytest.mark.parametrize("path, advice", [
    ("wiki/large.md", "search"),
    ("reference/large.txt", "search"),
    ("output/audit-capture.json", "local copy"),
    ("reference/large.csv", "local copy"),
])
async def test_large_text_read_gives_an_available_retrieval_path(service, path, advice):
    await service.create("owner", "handbook", "Handbook")
    repo = service.git.repo("handbook")
    target = repo / path
    target.parent.mkdir(exist_ok=True)
    target.write_text("Synthetic accepted text\n" + "x" * 2_000_001)
    service.git.run(repo, "add", "--", path)
    service.git.run(repo, "commit", "-m", "Retain synthetic accepted evidence")
    service.git.run(repo, "push", "origin", "HEAD:main")
    with pytest.raises(ContentError, match=advice):
        await service.read_page("owner", "handbook", path)
