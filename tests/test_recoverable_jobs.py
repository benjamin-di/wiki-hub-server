import asyncio
import base64
from dataclasses import replace

import pytest

from wiki_hub.gitstore import ContentError, GitError
from wiki_hub.service import Busy, Denied, Service


def source(value="The workshop opens at 09:30."):
    return base64.b64encode(value.encode()).decode()


async def finish(service):
    await asyncio.gather(*tuple(service.tasks))


async def test_concurrent_submission_replay_and_restart_never_repeat_worker(service):
    await service.create("owner", "starter", "Starter")
    calls, runner = [], service.runner

    async def counted(*args):
        calls.append(args[1]["id"])
        await runner(*args)

    service.runner = counted
    jobs = await asyncio.gather(*[
        service.ingest("owner", "starter", "note.txt", source(), submission_id="one-submission") for _ in range(5)
    ])
    assert len({job["id"] for job in jobs}) == 1
    await finish(service)
    restarted = Service(service.settings, service.forgejo, runner=counted)
    again = await restarted.ingest("owner", "starter", "note.txt", source(), submission_id="one-submission")
    assert again["id"] == jobs[0]["id"] and again["status"] == "completed"
    assert calls == [again["id"]] and restarted.tasks == set()
    for filename, data, instructions in [("other.txt", source(), ""), ("note.txt", source("different"), ""),
                                         ("note.txt", source(), "different")]:
        with pytest.raises(ContentError, match="different input"):
            await restarted.ingest("owner", "starter", filename, data, instructions, "one-submission")
    await service.share("owner", "starter", "reader", "contribute")
    other = await service.ingest("reader", "starter", "note.txt", source(), submission_id="one-submission")
    assert other["id"] != again["id"]
    await finish(service)
    await service.share("owner", "starter", "reader", "none")
    with pytest.raises(Denied):
        await service.ingest("reader", "starter", "note.txt", source(), submission_id="one-submission")


@pytest.mark.parametrize("operation", ["ingest", "rollback"])
async def test_accepted_push_with_lost_acknowledgement_recovers_exact_revision(service, monkeypatch, operation):
    await service.create("owner", "starter", "Starter")
    baseline = (await service.history("owner", "starter"))[0]["revision"]
    run = service.git.run

    def lose_reply(path, *args, **kwargs):
        result = run(path, *args, **kwargs)
        if path.parent == service.settings.workspace_dir and args[0] == "push":
            raise GitError("Synthetic lost reply after acceptance")
        return result

    monkeypatch.setattr(service.git, "run", lose_reply)
    if operation == "ingest":
        job = await service.ingest("owner", "starter", "note.txt", source())
        await finish(service)
        result = await service.job("owner", job["id"])
        assert result["status"] == "completed"
    else:
        result = await service.rollback("owner", "starter", baseline)
        job = await service.job("owner", result["job_id"])
        assert job["status"] == "completed" and job["kind"] == "rollback"
    remote = service.forgejo.directory / "starter.git"
    assert result["revision"] == run(remote, "rev-parse", "main").decode().strip()


async def test_unknown_publication_remains_visible_until_observation_recovers(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    run, accepted = service.git.run, service.git.accepted

    def lose_reply(path, *args, **kwargs):
        result = run(path, *args, **kwargs)
        if path.parent == service.settings.workspace_dir and args[0] == "push":
            raise GitError("Synthetic lost reply")
        return result

    def unavailable(*args):
        raise GitError("Synthetic unavailable backend")

    monkeypatch.setattr(service.git, "run", lose_reply)
    monkeypatch.setattr(service.git, "accepted", unavailable)
    job = await service.ingest("owner", "starter", "note.txt", source(), submission_id="uncertain")
    await finish(service)
    result = await service.job("owner", job["id"])
    assert result["status"] == "uncertain" and result["revision"] is None and result["candidate_revision"]
    replay = await service.ingest("owner", "starter", "note.txt", source(), submission_id="uncertain")
    assert replay["id"] == result["id"] and not service.tasks
    monkeypatch.setattr(service.git, "run", run)
    monkeypatch.setattr(service.git, "accepted", accepted)
    assert (await service.job("owner", job["id"]))["status"] == "completed"


async def test_restart_reconciles_publication_with_later_remote_commits(service):
    await service.create("owner", "starter", "Starter")
    first = await service.ingest("owner", "starter", "first.txt", source())
    await finish(service)
    revision = (await service.job("owner", first["id"]))["revision"]
    await service.ingest("owner", "starter", "second.txt", source("Additional evidence"))
    await finish(service)
    with service.state.connect() as db:
        db.execute("UPDATE jobs SET status='publishing',revision=NULL,candidate_revision=? WHERE id=?",
                   (revision, first["id"]))
    restarted = Service(service.settings, service.forgejo, runner=service.runner)
    assert restarted.state.job(first["id"])["status"] == "uncertain"
    result = await restarted.job("owner", first["id"])
    assert result["status"] == "completed" and result["revision"] == revision and not restarted.tasks


async def test_queue_bound_replay_and_job_listing_are_authorized_and_paginated(service):
    await service.create("owner", "starter", "Starter")
    service.settings = replace(service.settings, max_pending_jobs=1)
    entered, release = asyncio.Event(), asyncio.Event()
    runner = service.runner

    async def wait(*args):
        entered.set()
        await release.wait()
        await runner(*args)

    service.runner = wait
    first = await service.ingest("owner", "starter", "first.txt", source(), submission_id="first")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        replay = await service.ingest("owner", "starter", "first.txt", source(), submission_id="first")
        assert replay["id"] == first["id"]
        before_sources = set((service.settings.state_dir / "sources").iterdir())
        with pytest.raises(Busy):
            await service.ingest("owner", "starter", "second.txt", source(), submission_id="second")
        assert set((service.settings.state_dir / "sources").iterdir()) == before_sources
    finally:
        release.set()
        await finish(service)
    await service.ingest("owner", "starter", "second.txt", source(), submission_id="second")
    await finish(service)
    listing = await service.jobs("owner", "starter", limit=1)
    assert len(listing["jobs"]) == 1 and listing["next_before"]
    remaining = await service.jobs("owner", "starter", limit=1, before=listing["next_before"])
    assert remaining["jobs"][0]["id"] == first["id"] and remaining["next_before"] is None
    assert not {"subject", "source_path", "instructions", "fingerprint"} & listing["jobs"][0].keys()
    with pytest.raises(Denied):
        await service.jobs("stranger", "starter")


async def test_missing_candidate_after_lost_reply_stays_uncertain_until_later_observation(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    run, accepted = service.git.run, service.git.accepted
    calls, runner = [], service.runner

    async def counted(*args):
        calls.append(args[1]["id"])
        await runner(*args)

    def lose_reply(path, *args, **kwargs):
        result = run(path, *args, **kwargs)
        if path.parent == service.settings.workspace_dir and args[0] == "push":
            raise GitError("Synthetic lost response before remote outcome becomes observable")
        return result

    service.runner = counted
    monkeypatch.setattr(service.git, "run", lose_reply)
    monkeypatch.setattr(service.git, "accepted", lambda *args: False)
    job = await service.ingest("owner", "starter", "note.txt", source(), submission_id="delayed-outcome")
    await finish(service)
    assert (await service.job("owner", job["id"]))["status"] == "uncertain"
    replay = await service.ingest("owner", "starter", "note.txt", source(), submission_id="delayed-outcome")
    assert replay["id"] == job["id"] and replay["status"] == "uncertain"
    monkeypatch.setattr(service.git, "run", run)
    monkeypatch.setattr(service.git, "accepted", accepted)
    result = await service.job("owner", job["id"])
    assert result["status"] == "completed" and calls == [job["id"]]
    assert result["revision"] == run(service.forgejo.directory / "starter.git", "rev-parse", "main").decode().strip()


async def test_explicit_non_fast_forward_rejection_is_failed_without_retry(service, monkeypatch):
    from test_gitstore import remote_edit

    await service.create("owner", "starter", "Starter")
    prepare = service.publication_prepared
    remote = service.forgejo.directory / "starter.git"

    def race(job_id):
        prepared = prepare(job_id)

        def callback(revision):
            prepared(revision)
            remote_edit(service.git, remote, "wiki/owner-edit.md", "# Concurrent owner update\n")

        return callback

    monkeypatch.setattr(service, "publication_prepared", race)
    job = await service.ingest("owner", "starter", "note.txt", source(), submission_id="rejected-outcome")
    await finish(service)
    result = await service.job("owner", job["id"])
    assert result["status"] == "failed" and "rejected" in result["error"]
    assert result["revision"] is None and result["candidate_revision"]
    assert service.git.run(remote, "rev-parse", "main").decode().strip() != result["candidate_revision"]
    replay = await service.ingest("owner", "starter", "note.txt", source(), submission_id="rejected-outcome")
    assert replay["id"] == job["id"] and replay["status"] == "failed" and not service.tasks
