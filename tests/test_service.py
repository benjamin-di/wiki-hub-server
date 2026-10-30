import asyncio
import base64
import json
import threading
from dataclasses import replace

import httpx
import pytest

from wiki_hub.gitstore import ContentError, GitError
from wiki_hub.service import Denied, Service


def encoded(text):
    return base64.b64encode(text.encode()).decode()


async def complete(service, job):
    await asyncio.gather(*service.tasks)
    return await service.job("owner", job["id"])


async def test_source_to_git_search_read_and_rollback(service):
    created = await service.create("owner", "starter", "Starter")
    assert created["status"] == "ready"
    baseline = (await service.history("owner", "starter"))[0]["revision"]
    job = await service.ingest("owner", "starter", "workshop.txt", encoded("The workshop opens at 09:30."))
    finished = await complete(service, job)
    assert finished["status"] == "completed", finished
    assert finished["revision"] != baseline
    assert "source_path" not in finished
    result = await service.search("owner", "starter", "workshop 09:30")
    assert result["results"]
    assert result["revision"] == finished["revision"]
    page = await service.read_page("owner", "starter", "wiki/workshop.md")
    assert "09:30" in page["content"]
    assert "raw/" in page["content"] and "reference/" in page["content"]
    assert not (service.settings.workspace_dir / job["id"]).exists()
    restored = await service.rollback("owner", "starter", baseline)
    assert restored["revision"] not in (baseline, finished["revision"])
    assert (await service.pages("owner", "starter"))["pages"] == ["wiki/index.md"]
    assert not (await service.search("owner", "starter", "09:30"))["results"]


async def test_accepted_wiki_stays_readable_during_model_ingestion(service):
    await service.create("owner", "starter", "Starter")
    before = await service.read_page("owner", "starter", "wiki/index.md")
    entered, release = asyncio.Event(), asyncio.Event()
    runner = service.runner

    async def blocked_model(work, *args):
        (work / "wiki/index.md").write_text("# Unpublished draft\n", encoding="utf-8")
        entered.set()
        await release.wait()
        await runner(work, *args)

    service.runner = blocked_model
    job = await service.ingest("owner", "starter", "note.txt", encoded("The workshop opens at 09:30."))
    try:
        # These bounds detect deadlock, not a latency target. Git subprocesses
        # and index creation need scheduling time on contended test runners.
        await asyncio.wait_for(entered.wait(), timeout=30)
        accepted = await asyncio.wait_for(
            service.read_page("owner", "starter", "wiki/index.md"),
            timeout=30,
        )
        assert accepted == before
        search = await asyncio.wait_for(service.search("owner", "starter", "Unpublished"), timeout=30)
        assert search["results"] == []
    finally:
        release.set()
        await asyncio.gather(*service.tasks)
    finished = await service.job("owner", job["id"])
    assert finished["status"] == "completed"
    assert (await service.read_page("owner", "starter", "wiki/index.md"))["revision"] == finished["revision"]


async def test_unknown_identity_sees_nothing_and_cannot_retrieve_jobs(service):
    await service.create("owner", "starter", "Starter")
    job = await service.ingest("owner", "starter", "note.txt", encoded("private synthetic source"))
    await complete(service, job)
    assert await service.list_wikis("stranger") == []
    for operation in (
        service.pages("stranger", "starter"),
        service.search("stranger", "starter", "source"),
        service.read_page("stranger", "starter", "wiki/index.md"),
        service.history("stranger", "starter"),
        service.clone("stranger", "starter"),
        service.job("stranger", job["id"]),
    ):
        with pytest.raises(Denied):
            await operation
    with pytest.raises(Denied):
        await service.create("stranger", "other", "Other")


async def test_original_source_change_is_not_published(service):
    await service.create("owner", "starter", "Starter")
    before = (await service.history("owner", "starter"))[0]["revision"]

    async def corrupt(work, job, raw, reference):
        (work / raw).write_text("replaced original")

    service.runner = corrupt
    job = await service.ingest("owner", "starter", "note.txt", encoded("original"))
    finished = await complete(service, job)
    assert finished["status"] == "failed"
    assert "original source changed" in finished["error"]
    assert (await service.history("owner", "starter"))[0]["revision"] == before
    assert (service.settings.workspace_dir / job["id"]).is_dir()


async def test_model_failure_is_visible_and_no_commit_is_published(service):
    await service.create("owner", "starter", "Starter")
    before = await service.history("owner", "starter")

    async def fail(*args):
        raise RuntimeError("provider diagnostic with synthetic-secret")

    service.runner = fail
    job = await service.ingest("owner", "starter", "note.txt", encoded("source"))
    finished = await complete(service, job)
    assert finished["status"] == "failed"
    assert "synthetic-secret" not in str(finished)
    assert await service.history("owner", "starter") == before


async def test_restart_marks_unfinished_job_without_automatic_model_retry(service):
    await service.create("owner", "starter", "Starter")
    service.state.add_job("pending", "starter", "owner", "note.txt", "/synthetic/source", "")
    restarted = Service(service.settings, service.forgejo, runner=service.runner)
    job = await restarted.job("owner", "pending")
    assert job["status"] == "interrupted"
    assert restarted.tasks == set()


@pytest.mark.parametrize("path", ["../secret", "raw/../../secret", "/etc/passwd", "AGENTS.md", "wiki/.git/config"])
async def test_content_reads_cannot_escape_allowed_roots(service, path):
    await service.create("owner", "starter", "Starter")
    with pytest.raises(ContentError):
        await service.read_page("owner", "starter", path)


async def test_bad_upload_leaves_no_job_or_publication(service):
    await service.create("owner", "starter", "Starter")
    for name, data in [("../outside.txt", encoded("source")), ("source.txt", "not base64!"), ("empty.txt", "")]:
        with pytest.raises(ContentError):
            await service.ingest("owner", "starter", name, data)
    assert not service.tasks


async def test_creation_retries_after_initial_push_failure(service, monkeypatch):
    run = service.git.run

    def reject_push(path, *args, **kwargs):
        if args[0] == "push":
            raise GitError("Test initial push failed.")
        return run(path, *args, **kwargs)

    monkeypatch.setattr(service.git, "run", reject_push)
    with pytest.raises(GitError):
        await service.create("owner", "starter", "Starter")
    assert service.state.wiki("starter")["status"] == "initializing"
    monkeypatch.setattr(service.git, "run", run)
    assert (await service.create("owner", "starter", "Starter"))["status"] == "ready"
    assert (await service.pages("owner", "starter"))["pages"] == ["wiki/index.md"]


async def test_existing_originals_and_references_remain_unchanged(service):
    await service.create("owner", "starter", "Starter")
    first = await service.ingest("owner", "starter", "first.txt", encoded("First source"))
    assert (await complete(service, first))["status"] == "completed"
    accepted = (await service.history("owner", "starter"))[0]["revision"]

    async def change_previous(work, job, raw, reference):
        previous = work / "reference" / (first["id"] + ".txt")
        previous.write_text("A changed older source", encoding="utf-8")

    service.runner = change_previous
    second = await service.ingest("owner", "starter", "second.txt", encoded("Second source"))
    finished = await complete(service, second)
    assert finished["status"] == "failed"
    assert "preserved source or reference changed" in finished["error"]
    assert (await service.history("owner", "starter"))[0]["revision"] == accepted


async def test_shutdown_interrupts_running_and_queued_ingestions(service):
    await service.create("owner", "starter", "Starter")
    entered = asyncio.Event()

    async def wait_for_stop(*args):
        entered.set()
        await asyncio.Event().wait()

    service.runner = wait_for_stop
    running = await service.ingest("owner", "starter", "running.txt", encoded("First source"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    queued = await service.ingest("owner", "starter", "queued.txt", encoded("Second source"))
    await service.shutdown()
    assert (await service.job("owner", running["id"]))["status"] == "interrupted"
    assert (await service.job("owner", queued["id"]))["status"] == "interrupted"
    assert not service.tasks
    with pytest.raises(ContentError, match="server is stopping"):
        await service.ingest("owner", "starter", "later.txt", encoded("Later source"))


async def test_different_wikis_wait_for_the_single_model_worker_locally(service):
    await service.create("owner", "starter", "Starter")
    await service.create("owner", "second", "Second")
    entered, release = asyncio.Event(), asyncio.Event()
    runner = service.runner
    active = 0
    maximum = 0

    async def one_worker(*args):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.set()
        try:
            await release.wait()
            await runner(*args)
        finally:
            active -= 1

    service.runner = one_worker
    first = await service.ingest("owner", "starter", "one.txt", encoded("Source one"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = await service.ingest("owner", "second", "two.txt", encoded("Source two"))
    try:
        # The first model is blocked while the second wiki's working copy is prepared.
        for _ in range(100):
            if (service.settings.workspace_dir / second["id"] / "reference" / (second["id"] + ".txt")).exists():
                break
            await asyncio.sleep(0.01)
        assert active == maximum == 1
    finally:
        release.set()
        await asyncio.gather(*service.tasks)
    assert (await service.job("owner", first["id"]))["status"] == "completed"
    assert (await service.job("owner", second["id"]))["status"] == "completed"
    assert maximum == 1


async def test_shutdown_drains_publication_and_records_accepted_revision(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    entered, release = threading.Event(), threading.Event()
    publish = service.git.publish

    def delayed_publish(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("Test did not release publication")
        return publish(*args, **kwargs)

    monkeypatch.setattr(service.git, "publish", delayed_publish)
    job = await service.ingest("owner", "starter", "note.txt", encoded("The workshop opens at 09:30."))
    stopping = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        stopping = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0)
        assert not stopping.done()
    finally:
        release.set()
        if stopping:
            await stopping
        else:
            await asyncio.gather(*service.tasks)
    finished = await service.job("owner", job["id"])
    assert finished["status"] == "completed"
    assert finished["revision"] == (await service.history("owner", "starter"))[0]["revision"]


async def test_shutdown_during_cleanup_does_not_downgrade_completed_job(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    cleanup = service.git.cleanup

    def delayed_cleanup(*args):
        entered.set()
        try:
            if not release.wait(timeout=30):
                raise RuntimeError("Test did not release cleanup")
            cleanup(*args)
        finally:
            cleaned.set()

    monkeypatch.setattr(service.git, "cleanup", delayed_cleanup)
    job = await service.ingest("owner", "starter", "note.txt", encoded("Source"))
    try:
        # Reaching cleanup includes a complete Git/LFS publication. This is a
        # synchronization barrier, not a five-second performance requirement.
        assert await asyncio.to_thread(entered.wait, 30)
        await service.shutdown()
        assert (await service.job("owner", job["id"]))["status"] == "completed"
    finally:
        release.set()
        assert await asyncio.to_thread(cleaned.wait, 30)


async def test_accepted_publish_is_completed_when_canonical_refresh_needs_retry(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    repo = service.git.repo("starter")
    run = service.git.run
    pushed = False

    def reject_refresh(path, *args, **kwargs):
        nonlocal pushed
        if pushed and path == repo and args[0] == "fetch":
            raise GitError("Test canonical refresh failed.")
        result = run(path, *args, **kwargs)
        if path.parent == service.settings.workspace_dir and args[0] == "push":
            pushed = True
        return result

    monkeypatch.setattr(service.git, "run", reject_refresh)
    job = await service.ingest("owner", "starter", "note.txt", encoded("Source"))
    finished = await complete(service, job)
    assert pushed
    assert finished["status"] == "completed"
    monkeypatch.setattr(service.git, "run", run)
    assert (await service.history("owner", "starter"))[0]["revision"] == finished["revision"]


@pytest.mark.parametrize(
    "outcome,exit_code,accepted",
    [
        ("success", 0, True),
        ("error", 0, False),
        ("success", 1, False),
    ],
)
async def test_model_relay_contract_uses_job_workspace_and_checks_result(
    service,
    monkeypatch,
    outcome,
    exit_code,
    accepted,
):
    service.settings = replace(service.settings, ingest_url="http://sandbox.example.test:8100")
    job = {"id": "a" * 32, "instructions": "Keep the date"}
    client_type = httpx.AsyncClient

    def receive(request):
        assert request.url == "http://sandbox.example.test:8100/run"
        body = json.loads(request.content)
        assert body["run_id"] == job["id"]
        assert body["vault"] == job["id"]
        assert body["allowed_providers"] == ["claude", "codex"]
        assert body["tier"] == service.settings.ingest_tier
        assert body["timeout_seconds"] == 600
        assert body["log_stdout"] == "ingest-" + job["id"] + ".stdout.log"
        assert "reference/source.txt" in body["prompt"]
        assert "Cite the original and extracted reference" in body["prompt"]
        return httpx.Response(200, json={"outcome": outcome, "exit_code": exit_code})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(receive),
            **kwargs,
        ),
    )
    operation = service.run_model(
        service.settings.workspace_dir / job["id"], job, "raw/source.pdf", "reference/source.txt"
    )
    if accepted:
        await operation
    else:
        with pytest.raises(ContentError, match="did not complete successfully"):
            await operation
