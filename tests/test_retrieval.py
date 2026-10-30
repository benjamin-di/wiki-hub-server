import sqlite3
from types import SimpleNamespace

import pytest
from fastmcp import Client

from wiki_hub.gitstore import ContentError, GitError
from wiki_hub.mcp import READ_BUDGET, create_mcp
from wiki_hub.search import Query, SearchIndex, outline, summary
from wiki_hub.service import Denied

HANDBOOK = {
    "wiki/index.md": "# Handbook\n\nSynthetic travel preferences and documents for retrieval tests.\n\n"
                     "- [Travel](travel.md): seats and meals\n",
    "wiki/travel.md": "# Travel\n\n## Seats\n\nPrefers an aisle seat on long flights.\n\n"
                      "## Meals\n\nOrders vegetarian meals when flying.\n",
    "wiki/passport.md": "# Passport\n\nThe synthetic passport expires in March 2031.\n",
    "reference/visa.txt": "Visa notes\n\nA tourist visa is not required for short stays.\n",
}


def push(service, slug, files, message="Synthetic edit"):
    writer = service.forgejo.directory / (slug + "-writer")
    if not writer.exists():
        service.git.run(service.forgejo.directory, "clone", str(service.forgejo.directory / (slug + ".git")),
                        str(writer))
        service.git.run(writer, "config", "user.name", "Test Writer")
        service.git.run(writer, "config", "user.email", "writer@example.invalid")
    service.git.run(writer, "pull", "--ff-only", "origin", "main")
    for name, content in files.items():
        target = writer / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        service.git.run(writer, "add", "--", name)
    service.git.run(writer, "commit", "-m", message)
    service.git.run(writer, "push", "origin", "HEAD:main")
    return service.git.revision(writer)


async def handbook(service, slug="handbook"):
    await service.create("owner", slug, slug.title())
    return push(service, slug, HANDBOOK)


def test_query_compiles_terms_phrases_and_exclusions_without_operators():
    query = Query('aisle "long flights" -window NEAR( DEC-0054 a')
    assert query.fts() == ('("aisle"* AND "long flights" AND "NEAR("* AND "DEC-0054" AND "a") NOT ("window")')
    assert query.fts(any_term=True).startswith('("aisle"* OR "long flights" OR')
    assert not Query("-only -negative").include
    assert Query('"unbalanced').fts() == '"""unbalanced"*'
    assert Query('say"hi').fts() == '"say""hi"'


def test_outline_ignores_fenced_headings_and_summary_reads_the_overview():
    text = "# Title\n\nIntro.\n\n```\n# not a heading\n```\n\n## Section ##\n~~~\n## also code\n~~~\n### Deep\n"
    assert outline(text) == [{"line": 1, "level": 1, "heading": "Title"},
                             {"line": 9, "level": 2, "heading": "Section"},
                             {"line": 13, "level": 3, "heading": "Deep"}]
    assert summary(HANDBOOK["wiki/index.md"]) == "Synthetic travel preferences and documents for retrieval tests."
    assert summary("# Only a title\n\n- a list\n") == ""
    metadata = ("---\ntitle: Synthetic index\ntags:\n  - reference\n---\n# Index\n\n"
                "Recorded preferences and documents.\n")
    assert summary(metadata) == "Recorded preferences and documents."
    assert summary("---\ntitle: Incomplete metadata\n") == ""


async def test_search_requires_all_terms_stems_and_falls_back_to_any(service):
    revision = await handbook(service)
    result = await service.search("owner", "handbook", "vegetarian flying")
    assert result["match"] == "all" and result["revision"] == revision
    hit = result["results"][0]
    assert (hit["wiki"], hit["path"], hit["heading"]) == ("handbook", "wiki/travel.md", "Meals")
    assert hit["match_line"] == 9 and "vegetarian" in hit["snippet"] and hit["revision"] == revision
    stemmed = await service.search("owner", "handbook", "flight seats")
    assert [h["path"] for h in stemmed["results"]] == ["wiki/travel.md"]
    loose = await service.search("owner", "handbook", "passport submarine")
    assert loose["match"] == "any" and [h["path"] for h in loose["results"]] == ["wiki/passport.md"]
    assert (await service.search("owner", "handbook", "travel -vegetarian"))["results"][0]["path"] != "wiki/travel.md"
    phrase = await service.search("owner", "handbook", '"tourist visa"')
    assert [h["path"] for h in phrase["results"]] == ["reference/visa.txt"]
    assert (await service.search("owner", "handbook", "-negative"))["results"] == []


@pytest.mark.parametrize("filler_count", [0, 6])
async def test_title_matches_rank_above_body_mentions(service, filler_count):
    await service.create("owner", "ranking", "Ranking")
    # Ranking must survive both tiny collections and corpora where the term is rare.
    filler = {f"wiki/topic-{n}.md": f"# Topic {n}\n\nUnrelated synthetic notes.\n" for n in range(filler_count)}
    push(service, "ranking", {"wiki/passport.md": "# Passport\n\nDetails.\n",
                              "wiki/notes.md": "# Notes\n\nMentions a passport once among other words.\n", **filler})
    paths = [h["path"] for h in (await service.search("owner", "ranking", "passport"))["results"]]
    assert paths == ["wiki/passport.md", "wiki/notes.md"]


async def test_cross_wiki_search_covers_only_current_grants(service):
    await handbook(service)
    await service.create("owner", "private", "Private")
    push(service, "private", {"wiki/secret.md": "# Secret\n\nThe aisle preference is private here.\n"})
    owner = await service.search("owner", None, "aisle")
    assert {h["wiki"] for h in owner["results"]} == {"handbook", "private"}
    assert set(owner["revisions"]) == {"handbook", "private", "starter"} - {"starter"}
    await service.share("owner", "handbook", "reader", "read")
    reader = await service.search("reader", None, "aisle")
    assert {h["wiki"] for h in reader["results"]} == {"handbook"} and "private" not in reader["revisions"]
    await service.share("owner", "handbook", "reader", "none")
    assert (await service.search("reader", None, "aisle"))["results"] == []


async def test_cross_wiki_search_reports_one_failing_wiki_without_hiding_others(service, monkeypatch):
    await handbook(service)
    await service.create("owner", "broken", "Broken")
    search_wiki = service._search_wiki

    failure = GitError("Synthetic backend failure")

    async def failing(subject, wiki, *args):
        if wiki == "broken":
            raise failure
        return await search_wiki(subject, wiki, *args)

    monkeypatch.setattr(service, "_search_wiki", failing)
    result = await service.search("owner", None, "aisle")
    assert result["unavailable"] == ["broken"] and result["results"][0]["wiki"] == "handbook"
    with pytest.raises(GitError):
        await service.search("owner", "broken", "aisle")
    failure = Denied("Synthetic revocation during search")
    result = await service.search("owner", None, "aisle")
    assert "unavailable" not in result and "broken" not in result["revisions"]


async def test_old_index_schema_is_rebuilt_and_fallback_scan_honours_the_query(service):
    await handbook(service)
    index = SearchIndex(service.settings, "handbook")
    index.path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(index.path) as db:
        db.execute("CREATE TABLE indexed_revision (revision TEXT NOT NULL)")
        db.execute("CREATE VIRTUAL TABLE documents USING fts5(path UNINDEXED, content)")
    assert (await service.search("owner", "handbook", "aisle"))["results"][0]["path"] == "wiki/travel.md"
    index.path.write_bytes(b"A synthetic damaged derived database")
    fallback = await service.search("owner", "handbook", "vegetarian flying -aisle")
    assert fallback["results"] == []
    fallback = await service.search("owner", "handbook", "vegetarian flying")
    assert [h["path"] for h in fallback["results"]] == ["wiki/travel.md"]
    assert fallback["results"][0]["heading"] == "Meals"


async def test_pages_lists_titles_and_references_and_wikis_describe_themselves(service):
    revision = await handbook(service)
    pages = await service.pages("owner", "handbook")
    assert pages["revision"] == revision and "reference/visa.txt" not in pages["pages"]
    files = {f["path"]: f for f in pages["files"]}
    assert files["wiki/travel.md"] == {"path": "wiki/travel.md", "title": "Travel", "lines": 9}
    assert files["reference/visa.txt"]["title"] == "Visa notes"
    described = {w["slug"]: w for w in await service.list_wikis("owner")}
    assert described["handbook"]["description"].startswith("Synthetic travel preferences")


async def test_ranged_reads_validate_lines_and_suggest_close_paths(service):
    revision = await handbook(service)
    page = await service.read_page("owner", "handbook", "wiki/travel.md", None, 3, 5)
    assert page["content"] == "## Seats\n\nPrefers an aisle seat on long flights.\n"
    assert (page["start_line"], page["end_line"], page["total_lines"], page["revision"]) == (3, 5, 9, revision)
    assert (await service.read_page("owner", "handbook", "wiki/travel.md", None, 8))["end_line"] == 9
    for start, end in ((0, None), (5, 4), (10, None), ("2", None)):
        with pytest.raises(ContentError):
            await service.read_page("owner", "handbook", "wiki/travel.md", None, start, end)
    with pytest.raises(ContentError, match="Close matches: wiki/travel.md"):
        await service.read_page("owner", "handbook", "wiki/travle.md")
    whole = await service.read_page("owner", "handbook", "wiki/travel.md")
    assert whole == {"wiki": "handbook", "path": "wiki/travel.md", "revision": revision,
                     "content": HANDBOOK["wiki/travel.md"]}


async def test_mcp_tools_number_lines_and_return_an_outline_for_long_files(service):
    await handbook(service)
    body = "# Long\n\n" + "".join(f"## Part {n}\n\n" + "filler text\n" * 200 for n in range(20))
    push(service, "handbook", {"wiki/long.md": body}, "Long page")
    server = create_mcp(service, subject_provider=lambda: "owner")
    assert "Consult it whenever" in server.instructions
    async with Client(server) as client:
        search = (await client.call_tool("wiki_search", {"query": "aisle"})).structured_content
        assert search["results"][0]["snippet"].startswith("     3\t## Seats")
        ranged = (await client.call_tool("wiki_read", {"wiki": "handbook", "path": "wiki/travel.md",
                                                       "start_line": 5, "end_line": 5})).structured_content
        assert ranged["content"] == "     5\tPrefers an aisle seat on long flights.\n" and "truncated" not in ranged
        long = (await client.call_tool("wiki_read", {"wiki": "handbook", "path": "wiki/long.md"})).structured_content
        assert long["truncated"] and len(long["content"]) < READ_BUDGET + 8 * (long["end_line"] + 1)
        assert long["outline"][1] == {"line": 3, "level": 2, "heading": "Part 0"}
        follow = (await client.call_tool("wiki_read", {"wiki": "handbook", "path": "wiki/long.md",
                                                       "start_line": long["next_start_line"]})).structured_content
        assert follow["start_line"] == long["end_line"] + 1 and "outline" not in follow
        pages = (await client.call_tool("wiki_pages", {"wiki": "handbook"})).structured_content
        assert "pages" not in pages and {"path": "wiki/long.md", "title": "Long", "lines": 4042} in pages["files"]


def test_rest_api_serves_cross_wiki_search_and_line_ranges(settings, service):
    from starlette.testclient import TestClient
    from test_http_mcp import FixtureVerifier

    from wiki_hub.server import create_app

    with TestClient(create_app(settings, service, FixtureVerifier())) as client:
        headers = {"Authorization": "Bearer owner-token"}
        client.portal.call(handbook, service)
        found = client.get("/api/search", params={"q": "aisle"}, headers=headers).json()
        assert found["results"][0]["wiki"] == "handbook"
        page = client.get("/api/wikis/handbook/page", params={"path": "wiki/travel.md", "start_line": "5",
                                                             "end_line": "5"}, headers=headers).json()
        assert page["content"] == "Prefers an aisle seat on long flights.\n"
        bad = client.get("/api/wikis/handbook/page", params={"path": "wiki/travel.md", "start_line": "x"},
                         headers=headers)
        assert bad.status_code == 400 and "whole line number" in bad.json()["error"]


async def test_unicode_terms_match_the_index_tokenizer_and_exclusions_are_exact(service):
    await service.create("owner", "words", "Words")
    push(service, "words", {"wiki/street.md": "# Street\n\nLives on Hauptstraße in İstanbul.\n",
                            "wiki/pets.md": "# Pets\n\nOne dog and a category list of toys.\n"})
    assert [h["path"] for h in (await service.search("owner", "words", "Hauptstraße"))["results"]] == [
        "wiki/street.md"]
    assert (await service.search("owner", "words", "İstanbul"))["results"]
    assert [h["path"] for h in (await service.search("owner", "words", "dog -cat"))["results"]] == ["wiki/pets.md"]


async def test_undecodable_or_odd_git_entries_do_not_disable_listing_or_search(service):
    await handbook(service)
    writer = service.forgejo.directory / "handbook-writer"
    (writer / "wiki/latin1.md").write_bytes("# Caf\xe9\n\naisle\n".encode("latin-1"))
    service.git.run(writer, "add", "wiki/latin1.md")
    blob = service.git.run(writer, "rev-parse", "HEAD").decode().strip()
    # A gitlink (submodule entry) under wiki/ has no blob to read.
    service.git.run(writer, "update-index", "--add", "--cacheinfo", f"160000,{blob},wiki/module.md")
    service.git.run(writer, "commit", "-m", "Direct push with odd entries")
    service.git.run(writer, "push", "origin", "HEAD:main")
    paths = {f["path"] for f in (await service.pages("owner", "handbook"))["files"]}
    assert "wiki/travel.md" in paths and "wiki/latin1.md" not in paths
    assert [h["path"] for h in (await service.search("owner", None, "aisle"))["results"]] == ["wiki/travel.md"]
    SearchIndex(service.settings, "handbook").path.write_bytes(b"A synthetic damaged derived database")
    assert [h["path"] for h in (await service.search("owner", "handbook", "aisle"))["results"]] == [
        "wiki/travel.md"]


async def test_cross_wiki_search_drops_revoked_results_and_reports_busy_wikis(service, monkeypatch):
    await handbook(service)
    await handbook(service, "second")
    await service.share("owner", "handbook", "reader", "read")
    await service.share("owner", "second", "reader", "read")
    search_wiki = service._search_wiki

    async def revoke_after(subject, wiki, *args):
        result = await search_wiki(subject, wiki, *args)
        if wiki == "handbook":
            await service.share("owner", "handbook", "reader", "none")
        return result

    monkeypatch.setattr(service, "_search_wiki", revoke_after)
    result = await service.search("reader", None, "aisle")
    assert {h["wiki"] for h in result["results"]} == {"second"} and "handbook" not in result["revisions"]
    assert "unavailable" not in result
    monkeypatch.setattr(service, "_search_wiki", search_wiki)
    monkeypatch.setattr("wiki_hub.service.SEARCH_LOCK_WAIT", 0.05)
    async with service.lock("second"):
        busy = await service.search("owner", None, "aisle")
    assert busy["unavailable"] == ["second"] and {h["wiki"] for h in busy["results"]} == {"handbook"}


async def test_ranges_keep_the_artifact_limit_and_mcp_marks_an_overlong_line(service):
    await handbook(service)
    push(service, "handbook", {"wiki/capture.json": "[" + "1," * 1_000_001 + "1]\n",
                               "reference/blob.txt": "x" * (READ_BUDGET + 10) + "\nnext\n"}, "Large inputs")
    with pytest.raises(ContentError, match="authorized local copy"):
        await service.read_page("owner", "handbook", "wiki/capture.json", None, 1, 1)
    async with Client(create_mcp(service, subject_provider=lambda: "owner")) as client:
        read = (await client.call_tool("wiki_read", {"wiki": "handbook", "path": "reference/blob.txt"})
                ).structured_content
    assert read["line_truncated"] == 1 and read["next_start_line"] == 2 and read["truncated"]


async def test_ingest_refuses_a_symlinked_source_folder(service, tmp_path):
    import base64
    await service.create("owner", "linked", "Linked")
    writer = service.forgejo.directory / "linked-writer"
    service.git.run(service.forgejo.directory, "clone", str(service.forgejo.directory / "linked.git"), str(writer))
    service.git.run(writer, "config", "user.name", "Test Writer")
    service.git.run(writer, "config", "user.email", "writer@example.invalid")
    outside = tmp_path / "outside"
    outside.mkdir()
    (writer / "raw").symlink_to(outside)
    service.git.run(writer, "add", "raw")
    service.git.run(writer, "commit", "-m", "Link raw elsewhere")
    service.git.run(writer, "push", "origin", "HEAD:main")
    job = await service.ingest("owner", "linked", "note.txt", base64.b64encode(b"Synthetic").decode())
    import asyncio
    await asyncio.gather(*service.tasks)
    assert (await service.job("owner", job["id"]))["status"] == "failed"
    assert not any(outside.iterdir())


async def test_unreadable_git_paths_cannot_disable_search_or_pollute_file_maps(service):
    await handbook(service)
    unsupported = {"wiki/.hidden.md": "aisle", "wiki/colon:name.md": "aisle",
                   "wiki/back\\slash.md": "aisle", "reference/.private/note.txt": "aisle"}
    push(service, "handbook", unsupported)
    for damaged in (False, True):
        if damaged:
            SearchIndex(service.settings, "handbook").path.write_bytes(b"Synthetic damaged index")
        result = await service.search("owner", "handbook", "aisle")
        assert [hit["path"] for hit in result["results"]] == ["wiki/travel.md"]
        pages = await service.pages("owner", "handbook")
        assert not set(unsupported).intersection(pages["pages"])
        assert not set(unsupported).intersection(f["path"] for f in pages["files"])


async def test_wiki_overview_tracks_direct_git_push_without_an_intervening_read(service):
    await handbook(service)
    before = service.git.revision(service.git.repo("handbook"))
    # Listing alone must discover the accepted map; no pages/search call warms the checkout.
    description = {row["slug"]: row for row in await service.list_wikis("owner")}["handbook"]["description"]
    assert description.startswith("Synthetic travel preferences")
    push(service, "handbook", {"wiki/index.md": "# Handbook\n\nUpdated synthetic overview.\n"})
    description = {row["slug"]: row for row in await service.list_wikis("owner")}["handbook"]["description"]
    assert description == "Updated synthetic overview."
    assert service.git.revision(service.git.repo("handbook")) == before


async def test_busy_or_unavailable_overview_does_not_hide_the_wiki(service, monkeypatch):
    await handbook(service)
    monkeypatch.setattr("wiki_hub.service.OVERVIEW_BUDGET", 0.01)
    async with service.lock("handbook"):
        assert await service.list_wikis("owner") == [
            {k: v for k, v in service.state.wiki("handbook").items()
             if k in ("slug", "title", "status", "created")}]

    def unavailable(*args, **kwargs):
        raise GitError("Synthetic unavailable backend")

    monkeypatch.setattr(service.git, "run", unavailable)
    assert "description" not in (await service.list_wikis("owner"))[0]


async def test_stalled_overview_fetch_is_bounded_and_healthy_wikis_remain_discoverable(service, monkeypatch):
    await service.create("owner", "a-stalled", "Stalled")
    await service.create("owner", "z-healthy", "Healthy")
    calls = []

    def bounded_backend(repo, *args, **kwargs):
        calls.append((repo.name, args[0]))
        assert 0 < kwargs.get("timeout", 120) <= 2
        if repo.name == "a-stalled":
            raise GitError("Synthetic fetch deadline expired")
        return b"# Healthy\n\nSynthetic healthy overview.\n" if args[0] == "show" else b""

    monkeypatch.setattr(service.git, "run", bounded_backend)
    listed = {wiki["slug"]: wiki for wiki in await service.list_wikis("owner")}
    assert set(listed) == {"a-stalled", "z-healthy"}
    assert "description" not in listed["a-stalled"]
    assert listed["z-healthy"]["description"] == "Synthetic healthy overview."
    assert ("a-stalled", "fetch") in calls and ("z-healthy", "show") in calls


async def test_overview_refreshes_share_a_discovery_budget(service, monkeypatch):
    for slug in ("one", "two", "three", "four"):
        await service.create("owner", slug, slug.title())
    now, calls = [0.0], []
    monkeypatch.setattr("wiki_hub.service.time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr("wiki_hub.service.DISCOVERY_BUDGET", 2.0)
    monkeypatch.setattr("wiki_hub.service.OVERVIEW_BUDGET", 1.0)

    def consume_budget(slug, deadline):
        calls.append(slug)
        assert 0 < deadline - now[0] <= 1
        now[0] = deadline
        return "Synthetic bounded overview."

    monkeypatch.setattr(service, "describe", consume_budget)
    listed = await service.list_wikis("owner")
    assert len(listed) == 4 and len(calls) == 2
    assert sum("description" in wiki for wiki in listed) == 2


async def test_cross_wiki_search_does_not_report_revoked_unavailable_wikis(service, monkeypatch):
    await handbook(service)
    await service.share("owner", "handbook", "reader", "read")

    async def revoked_failure(*args):
        await service.share("owner", "handbook", "reader", "none")
        raise GitError("Synthetic failed search")

    monkeypatch.setattr(service, "_search_wiki", revoked_failure)
    result = await service.search("reader", None, "aisle")
    assert result == {"revisions": {}, "match": "all", "results": []}


async def test_fallback_scan_matches_filenames_and_honours_exclusions_in_them(service):
    await service.create("owner", "names", "Names")
    push(service, "names", {"wiki/passport.md": "# Identity\n\nExpires in 2031.\n",
                            "wiki/notes.md": "# Notes\n\nPassport mentioned here.\n"})
    index = SearchIndex(service.settings, "names")
    for query in ("passport 2031", "passport", "2031 -passport"):
        indexed = await service.search("owner", "names", query)
        index.path.write_bytes(b"Synthetic damaged index")
        fallback = await service.search("owner", "names", query)
        assert fallback["match"] == indexed["match"]
        assert {h["path"] for h in fallback["results"]} == {h["path"] for h in indexed["results"]}
