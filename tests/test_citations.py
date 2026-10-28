import asyncio
import base64

import pytest

from wiki_hub.citations import validate_edits
from wiki_hub.gitstore import ContentError


@pytest.mark.parametrize("text", [
    "Source: [original](../raw/a%20b.pdf), [reference](../reference/source.txt#page-2)",
    "Source: `raw/a b.pdf`; reference/source.txt",
    "Source: [original](<../raw/a b.pdf>); reference/source.txt",
])
def test_local_evidence_conventions_resolve_without_external_fetches(text):
    validate_edits({"wiki/page.md": text}, {}, {"raw/a b.pdf", "reference/source.txt"}, "reference/source.txt")


def test_new_broken_evidence_is_rejected_without_blocking_old_legacy_links():
    previous = {"wiki/page.md": "Legacy evidence: raw/missing.pdf\n"}
    validate_edits({"wiki/page.md": previous["wiki/page.md"] + "An unrelated correction.\n"}, previous, set())
    with pytest.raises(ContentError, match="does not resolve"):
        validate_edits({"wiki/page.md": previous["wiki/page.md"] + "Source: reference/new-missing.txt"},
                       previous, set())
    validate_edits({"wiki/topics.md": "# Topics\n\n- [Workshop](workshop.md)\n"}, {}, set(), "reference/source.txt")
    with pytest.raises(ContentError, match="needs an original"):
        validate_edits({"wiki/page.md": "The workshop opens at 18:00."}, {}, set(), "reference/source.txt")


async def test_nonexistent_model_citation_never_publishes(service):
    await service.create("owner", "starter", "Starter")
    baseline = (await service.history("owner", "starter"))[0]["revision"]

    async def broken(work, *_):
        (work / "wiki/page.md").write_text("The workshop opens at 18:00. Source: raw/nonexistent.txt\n")

    service.runner = broken
    job = await service.ingest("owner", "starter", "source.txt", base64.b64encode(b"Opens at 09:30").decode())
    await asyncio.gather(*service.tasks)
    result = await service.job("owner", job["id"])
    assert result["status"] == "failed" and "citation does not resolve" in result["error"]
    assert (await service.history("owner", "starter"))[0]["revision"] == baseline
