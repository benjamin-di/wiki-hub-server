"""Network drive: per-person views, editable pages, attributed publication and device accounts."""
import asyncio
import base64
import json
import os
import stat
import time
from dataclasses import replace

import pytest
from conftest import LocalForgejo

from wiki_hub.drive import BUILD_XATTR, PERSON_GID_BASE, DriveError, DriveUnavailable, read_acl_groups
from wiki_hub.service import Denied, Service

pytestmark = pytest.mark.skipif(not hasattr(os, "setxattr"), reason="Linux ACL support is required")


class FakeAgent:
    """Applies accounts.tsv like the Samba agent: consumes secrets and reports state."""

    def __init__(self, control):
        self.control = control
        self.passdb = {}
        self.connected = set()
        self.running = True

    def apply(self):
        desired = self.control / "accounts.tsv"
        if not desired.exists():
            return
        generation, wanted = None, {}
        for line in desired.read_text().splitlines():
            fields = line.split("\t")
            if fields[0] == "generation":
                generation = int(fields[1])
            elif fields[0] == "device":
                wanted[fields[2]] = fields[4]
        for account, state in wanted.items():
            secret = self.control / "secrets" / account
            if secret.exists():
                self.passdb[account] = secret.read_text().strip()
                secret.unlink()
            if account in self.passdb and state == "disabled":
                self.connected.discard(account)
        for account in set(self.passdb) - set(wanted):
            del self.passdb[account]
            self.connected.discard(account)
        lines = [f"generation\t{generation}"] + [
            f"device\t{account}\t1\t{int(wanted.get(account) == 'active')}\t{int(account in self.connected)}"
            for account in sorted(self.passdb)]
        temporary = self.control / ".observed"
        temporary.write_text("\n".join(lines) + "\n")
        temporary.replace(self.control / "observed.tsv")

    async def run(self):
        while self.running:
            self.apply()
            await asyncio.sleep(0.02)


@pytest.fixture
def drive_settings(settings, tmp_path):
    return replace(settings, upload_limit=1024 * 1024, drive_dir=tmp_path / "drive",
                   drive_control_dir=tmp_path / "control", drive_server="files.example.test",
                   drive_confirm_seconds=1, drive_inbox_share="inbox")


@pytest.fixture
def drive_service(drive_settings, tmp_path):
    async def runner(work, _job, raw, reference):
        (work / "wiki/workshop.md").write_text(
            f"# Workshop\n\nOpens at 09:30.\n\nSource: [{raw}](../{raw})\n\nReference: [{reference}](../{reference})\n")

    return Service(drive_settings, LocalForgejo(tmp_path), runner=runner)


@pytest.fixture
async def agent(drive_settings):
    fake = FakeAgent(drive_settings.drive_control_dir)
    task = asyncio.create_task(fake.run())
    yield fake
    fake.running = False
    await task


def person(service, subject):
    return next(p["number"] for p in service.state.drive_people() if p["subject"] == subject)


def view(settings, service, subject):
    return settings.drive_dir / "views" / f"wp{person(service, subject)}"


def commit(service, slug, path, text, message="Accepted change"):
    """An accepted change made outside the drive (another writer's Git copy)."""
    repo = service.git.sync(service.state.wiki(slug))
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    service.git.run(repo, "add", "--all")
    service.git.run(repo, "commit", "-m", message)
    service.git.run(repo, "push", "origin", "HEAD:main")


async def accepted(service, slug, path):
    return (await service.read_page("owner", slug, path))["content"]


async def prepare(service, agent):
    await service.create("owner", "starter", "Starter handbook")
    await service.create("owner", "private", "Private")
    await service.share("owner", "starter", "reader", "read")
    credential = await service.add_drive_device("reader", "Laptop")
    for slug in ("starter", "private"):
        await service.refresh_drive(slug)
    return credential


async def test_people_see_only_readable_wikis_as_read_only_links(drive_service, agent, drive_settings):
    credential = await prepare(drive_service, agent)
    assert credential["username"] == "wd1" and len(credential["password"]) >= 40
    assert agent.passdb["wd1"] == credential["password"]
    assert not list((drive_settings.drive_control_dir / "secrets").iterdir())
    root = view(drive_settings, drive_service, "reader")
    assert sorted(p.name for p in root.iterdir()) == ["starter"]
    assert read_acl_groups(root) == {person(drive_service, "reader")}
    assert stat.S_IMODE(os.stat(root).st_mode) & 0o007 == 0
    index = root / "starter" / "wiki" / "index.md"
    shared = drive_settings.drive_dir / "shared" / "starter" / "wiki" / "index.md"
    assert index.read_text().startswith("# Starter handbook")
    assert stat.S_IMODE(index.stat().st_mode) == 0o444 and index.stat().st_ino == shared.stat().st_ino
    assert "Starter handbook" in (root / "starter" / "desktop.ini").read_bytes().decode("utf-16-le")
    assert not (root / "starter" / "AGENTS.md").exists()
    status = await drive_service.drive_status("reader")
    assert [(w["slug"], w["pages"]) for w in status["wikis"]] == [("starter", "read")]
    # Shared trees are never served directly.
    assert stat.S_IMODE(os.stat(drive_settings.drive_dir / "shared").st_mode) == 0o700


async def test_unshare_withdraws_the_wiki_and_held_folders_stop_resolving(drive_service, agent, drive_settings):
    await prepare(drive_service, agent)
    live = view(drive_settings, drive_service, "reader") / "starter"
    handle = os.open(live / "wiki", os.O_RDONLY | os.O_DIRECTORY)
    try:
        await drive_service.share("owner", "starter", "reader", "none")
        assert not live.exists()
        assert os.fstat(handle).st_nlink == 0
        with pytest.raises(FileNotFoundError):
            os.open("index.md", os.O_RDONLY, dir_fd=handle)
    finally:
        os.close(handle)
    await drive_service.share("owner", "starter", "reader", "read")
    assert (live / "wiki" / "index.md").exists()


async def test_copy_policy_and_suspension_control_the_drive(drive_service, agent, drive_settings):
    await prepare(drive_service, agent)
    live = view(drive_settings, drive_service, "reader") / "starter"
    reader = person(drive_service, "reader")
    await drive_service.user_settings("owner", "reader", False)
    assert not live.exists()
    await drive_service.user_settings("owner", "reader", True)
    assert live.exists()
    await drive_service.wiki_settings("owner", "starter", False)
    assert not live.exists()
    await drive_service.wiki_settings("owner", "starter", True)
    await drive_service.user_status("owner", "reader", True)
    assert not live.exists()
    accounts = (drive_settings.drive_control_dir / "accounts.tsv").read_text()
    assert f"device\t600001\twd1\t{PERSON_GID_BASE + reader}\tdisabled" in accounts
    await drive_service.user_status("owner", "reader", False)
    assert live.exists() and "\tactive" in (drive_settings.drive_control_dir / "accounts.tsv").read_text()


async def test_failed_drive_revocation_keeps_changes_pending(drive_service, agent, monkeypatch):
    await prepare(drive_service, agent)

    def broken(*_args):
        raise OSError("synthetic filesystem failure")

    monkeypatch.setattr(drive_service.drive, "withdraw", broken)
    for change in (drive_service.share("owner", "starter", "reader", "none"),
                   drive_service.wiki_settings("owner", "starter", False),
                   drive_service.user_settings("owner", "reader", False),
                   drive_service.user_status("owner", "reader", True)):
        with pytest.raises(DriveError):
            await change
    assert (await drive_service.access("owner", "starter"))["grants"][0]["pending"] == 1
    assert (await drive_service.wiki_settings("owner", "starter"))["pending"] is True
    assert (await drive_service.user_settings("owner", "reader"))["pending"] is True
    assert (await drive_service.user_status("owner", "reader"))["pending"] is True
    with pytest.raises(Denied):
        await drive_service.pages("reader", "starter")


async def test_revoking_last_device_removes_account_and_view(drive_service, agent, drive_settings):
    credential = await prepare(drive_service, agent)
    second = await drive_service.add_drive_device("reader", "Desktop")
    root = view(drive_settings, drive_service, "reader")
    first = await drive_service.revoke_drive_device("reader", credential["device"]["id"])
    assert first == {"id": credential["device"]["id"], "revoked": True, "pending": False}
    assert "wd1" not in agent.passdb and (root / "starter").exists()
    with pytest.raises(Denied):
        await drive_service.revoke_drive_device("someone", second["device"]["id"])
    assert (await drive_service.revoke_drive_device("owner", second["device"]["id"]))["pending"] is False
    assert not (root / "starter").exists() and agent.passdb == {}
    await drive_service._prune_drive_people()
    assert not root.exists()


async def test_new_revision_projects_originals_and_relinks_views(drive_service, agent, drive_settings):
    await prepare(drive_service, agent)
    await drive_service.share("owner", "starter", "reader", "contribute")
    shared = drive_settings.drive_dir / "shared" / "starter"
    index_inode = os.stat(shared / "wiki" / "index.md").st_ino
    original = b"Synthetic workshop schedule: opens at 09:30.\n"
    job = await drive_service.ingest("reader", "starter", "schedule.txt", base64.b64encode(original).decode())
    await asyncio.gather(*drive_service.tasks)
    await drive_service.refresh_drive("starter")
    live = view(drive_settings, drive_service, "reader") / "starter"
    raw = live / "raw" / f"{job['id']}-schedule.txt"
    assert raw.read_bytes() == original and stat.S_IMODE(raw.stat().st_mode) == 0o444
    assert (live / "wiki" / "workshop.md").exists()
    assert os.stat(shared / "wiki" / "index.md").st_ino == index_inode
    store = [p for p in (drive_settings.drive_dir / "lfs" / "starter").rglob("*") if p.is_file()]
    assert len(store) == 1 and store[0].stat().st_ino == raw.stat().st_ino


async def test_inconsistent_manifest_forces_a_complete_rebuild(drive_service, agent, drive_settings):
    await prepare(drive_service, agent)
    shared = drive_settings.drive_dir / "shared" / "starter"
    os.setxattr(shared, BUILD_XATTR, b"unrelated")
    inode = os.stat(shared / "wiki" / "index.md").st_ino
    await drive_service.refresh_drive("starter")
    assert os.stat(shared / "wiki" / "index.md").st_ino != inode
    live = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md"
    assert live.stat().st_ino == (shared / "wiki" / "index.md").stat().st_ino


async def test_files_explorer_would_act_on_are_not_projected(drive_service, agent, drive_settings, caplog):
    await prepare(drive_service, agent)
    for name in ("wiki/Desktop.INI", "wiki/notes/shortcut.lnk", "wiki/site.url", "wiki/ok.md"):
        commit(drive_service, "starter", name, "[.ShellClassInfo]\nIconResource=\\\\host.example.test\\x\n")
    await drive_service.refresh_drive("starter")
    live = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    assert (live / "ok.md").exists()
    assert not any((live / name).exists() for name in ("Desktop.INI", "notes/shortcut.lnk", "site.url"))
    # A periodic refresh at the same revision neither lists the tree again nor repeats warnings.
    caplog.clear()
    await drive_service.refresh_drive("starter")
    assert "Explorer would act on" not in caplog.text


# Editable pages -------------------------------------------------------------


async def writer(service, agent, role="write"):
    await prepare(service, agent)
    await service.share("owner", "starter", "reader", role)
    return person(service, "reader")


async def test_only_writers_get_a_private_writable_copy_of_pages(drive_service, agent, drive_settings):
    number = await writer(drive_service, agent, role="contribute")
    live = view(drive_settings, drive_service, "reader") / "starter"
    assert stat.S_IMODE((live / "wiki" / "index.md").stat().st_mode) == 0o444
    await drive_service.share("owner", "starter", "reader", "write")
    page = live / "wiki" / "index.md"
    shared = drive_settings.drive_dir / "shared" / "starter" / "wiki" / "index.md"
    assert page.stat().st_ino != shared.stat().st_ino
    assert read_acl_groups(page) == {number} and read_acl_groups(live / "wiki") == {number}
    assert os.getxattr(live / "wiki", "system.posix_acl_default")
    # Originals, references and the wiki folder itself stay read-only.
    assert read_acl_groups(live) == set() and stat.S_IMODE(os.stat(live).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(live / "raw").st_mode) == 0o755
    assert (await drive_service.drive_status("reader"))["wikis"][0]["pages"] == "write"
    # Archiving makes pages read-only again for everyone.
    await drive_service.archive("owner", "starter", True)
    assert stat.S_IMODE((live / "wiki" / "index.md").stat().st_mode) == 0o444


async def test_saved_page_is_published_as_the_writer_after_a_quiet_period(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    page = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md"
    page.write_text("# Starter handbook\n\nEdited in Explorer.\n")
    assert await drive_service.drive_edit_cycle() == []
    status = await drive_service.drive_status("reader")
    assert status["wikis"][0]["unpublished_pages"] == ["wiki/index.md"]
    [result] = await drive_service.drive_edit_cycle(force=True)
    assert result["published"] == ["wiki/index.md"] and result["revision"]
    assert "Edited in Explorer." in await accepted(drive_service, "starter", "wiki/index.md")
    history = await drive_service.history("owner", "starter")
    assert history[0]["message"] == "Edit wiki pages from the network drive"
    repo = drive_service.git.sync(drive_service.state.wiki("starter"))
    assert b"Principal: reader" in drive_service.git.run(repo, "log", "-1", "--format=%B")
    job = drive_service.state.job(result["job"])
    assert job["kind"] == "drive-edit" and job["status"] == "completed" and job["subject"] == "reader"
    await asyncio.gather(*drive_service.tasks)
    assert await drive_service.drive_edit_cycle(force=True) == []


async def test_other_files_and_raw_cannot_be_used_to_ingest(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    live = view(drive_settings, drive_service, "reader") / "starter"
    (live / "wiki" / "report.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (live / "wiki" / "~$draft.md").write_text("lock file")
    (live / "wiki" / ".index.md.swp").write_text("swap")
    assert await drive_service.drive_edit_cycle(force=True) == []
    status = (await drive_service.drive_status("reader"))["wikis"][0]
    assert status["not_published"] == ["wiki/report.pdf"] and status["unpublished_pages"] == []
    assert not drive_service.state.jobs("starter")
    assert "report.pdf" not in str(drive_service.git.list_files(drive_service.git.sync(
        drive_service.state.wiki("starter"))))
    # Read-only folders: the files, not just Samba, refuse writes by others.
    assert not os.access(live / "raw", os.W_OK) or os.getuid() == os.stat(live / "raw").st_uid
    assert read_acl_groups(live / "raw") == set() and stat.S_IMODE(os.stat(live / "raw").st_mode) == 0o755


async def test_non_overlapping_edits_merge_with_newer_accepted_changes(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    commit(drive_service, "starter", "wiki/notes.md", "one\ntwo\nthree\nfour\nfive\n")
    await drive_service.refresh_drive("starter")
    page = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "notes.md"
    assert page.read_text().startswith("one")
    page.write_text("ONE\ntwo\nthree\nfour\nfive\n")
    commit(drive_service, "starter", "wiki/notes.md", "one\ntwo\nthree\nfour\nFIVE\n")
    [result] = await drive_service.drive_edit_cycle(force=True)
    assert result["conflicts"] == []
    assert await accepted(drive_service, "starter", "wiki/notes.md") == "ONE\ntwo\nthree\nfour\nFIVE\n"
    assert page.read_text() == "ONE\ntwo\nthree\nfour\nFIVE\n"


async def test_conflicting_edit_is_kept_as_a_copy_and_never_published(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    commit(drive_service, "starter", "wiki/notes.md", "status: draft\n")
    await drive_service.refresh_drive("starter")
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "notes.md").write_text("status: mine\n")
    commit(drive_service, "starter", "wiki/notes.md", "status: theirs\n")
    [result] = await drive_service.drive_edit_cycle(force=True)
    assert result["revision"] is None and len(result["conflicts"]) == 1
    copy = folder.parent / result["conflicts"][0]
    assert copy.read_text() == "status: mine\n" and (folder / "notes.md").read_text() == "status: theirs\n"
    assert await accepted(drive_service, "starter", "wiki/notes.md") == "status: theirs\n"
    job = drive_service.state.job(result["job"])
    assert job["status"] == "failed" and "conflict" in job["error"]
    kept = list((drive_settings.state_dir / "drive-unpublished").rglob("*conflict*"))
    assert kept and kept[0].read_text() == "status: mine\n"
    assert await drive_service.drive_edit_cycle(force=True) == []
    status = (await drive_service.drive_status("reader"))["wikis"][0]
    assert status["conflict_copies"] == result["conflicts"]
    assert status["recent_publications"][0]["id"] == result["job"]
    assert status["recent_publications"][0]["error"] == job["error"]


async def test_deleting_a_page_publishes_and_accepted_changes_follow(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    commit(drive_service, "starter", "wiki/old.md", "old\n")
    commit(drive_service, "starter", "wiki/keep.md", "keep\n")
    await drive_service.refresh_drive("starter")
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "old.md").unlink()
    (folder / "keep.md").write_text("keep, edited\n")
    commit(drive_service, "starter", "wiki/other.md", "accepted elsewhere\n")
    await drive_service.refresh_drive("starter")
    # Accepted changes arrive while the person's unpublished edits stay.
    assert (folder / "other.md").read_text() == "accepted elsewhere\n"
    assert (folder / "keep.md").read_text() == "keep, edited\n" and not (folder / "old.md").exists()
    [result] = await drive_service.drive_edit_cycle(force=True)
    assert result["published"] == ["wiki/keep.md", "wiki/old.md"]
    files = drive_service.git.list_files(drive_service.git.sync(drive_service.state.wiki("starter")))
    assert "wiki/old.md" not in files and await accepted(drive_service, "starter", "wiki/keep.md") == "keep, edited\n"


async def test_losing_write_access_keeps_unpublished_edits(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    page = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md"
    page.write_text("# Unpublished\n")
    await drive_service.share("owner", "starter", "reader", "read")
    assert stat.S_IMODE(page.stat().st_mode) == 0o444 and page.read_text().startswith("# Starter handbook")
    kept = [p for p in (drive_settings.state_dir / "drive-unpublished").rglob("index.md")]
    assert kept and kept[0].read_text() == "# Unpublished\n"
    assert await drive_service.drive_edit_cycle(force=True) == []
    assert (await accepted(drive_service, "starter", "wiki/index.md")).startswith("# Starter handbook")


async def test_unconfirmed_device_is_revoked_and_its_secret_removed(drive_service, drive_settings):
    await drive_service.create("owner", "starter", "Starter")
    with pytest.raises(DriveUnavailable):
        await drive_service.add_drive_device("owner", "Desktop")
    assert not list((drive_settings.drive_control_dir / "secrets").iterdir())
    device = drive_service.state.drive_devices("owner")[0]
    assert device["revoked"] == 1
    assert "device\t" not in (drive_settings.drive_control_dir / "accounts.tsv").read_text()


async def test_agents_and_disabled_servers_have_no_drive(drive_service, service, agent):
    await drive_service.create("owner", "starter", "Starter")
    for operation in (drive_service.drive_status("agent:abc"), drive_service.add_drive_device("agent:abc", "x")):
        with pytest.raises(Denied):
            await operation
    with pytest.raises(Exception, match="does not offer a network drive"):
        await service.drive_status("owner")
    with pytest.raises(Denied):
        await drive_service.drive_devices("reader", "owner")


async def test_device_count_is_bounded(drive_service, agent, monkeypatch):
    import wiki_hub.service as module

    monkeypatch.setattr(module, "MAX_DRIVE_DEVICES", 1)
    await drive_service.add_drive_device("owner", "One")
    with pytest.raises(Exception, match="limit is 1"):
        await drive_service.add_drive_device("owner", "Two")


async def test_restart_revokes_devices_whose_enrolment_never_finished(drive_settings, tmp_path):
    first = Service(drive_settings, LocalForgejo(tmp_path))
    device = first.state.add_drive_device("drive-unfinished", "owner", "Laptop")
    first.drive.write_secret(device["number"], "x" * 43)
    second = Service(drive_settings, LocalForgejo(tmp_path))
    assert second.state.drive_device("drive-unfinished")["revoked"] == 1
    assert not (drive_settings.drive_control_dir / "secrets" / "wd1").exists()


async def test_status_reports_an_agent_that_stopped_applying_changes(drive_service, agent, drive_settings):
    await prepare(drive_service, agent)
    assert (await drive_service.drive_status("reader"))["file_server"] == "available"
    agent.running = False
    await asyncio.sleep(0.1)
    drive_service.state.drive_generation(bump=True)
    assert (await drive_service.drive_status("reader"))["file_server"] == "lagging"


async def test_inbox_is_partitioned_by_owner_and_revoked_agent_is_refused(drive_service, agent):
    service = drive_service
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "contribute")
    intake = await service.add_workload_agent("owner", "Intake", "intake-reader", owner="reader")
    await service.share("owner", "starter", intake["id"], "contribute")
    own = await service.inbox("owner")
    other = await service.inbox(intake["id"])
    assert own["folder"] != other["folder"]
    assert other["wikis"] == ["starter"]
    path = service.drive.inbox / other["folder"]
    number = int(other["folder"][2:])
    assert read_acl_groups(path) == {number}
    assert os.getxattr(path, "system.posix_acl_default") == os.getxattr(path, "system.posix_acl_access")
    await service.revoke_agent("reader", intake["id"])
    with pytest.raises(Denied):
        await service.inbox(intake["id"])


async def test_inbox_is_offered_only_when_the_file_server_serves_it(drive_settings, tmp_path):
    service = Service(replace(drive_settings, drive_inbox_share=""), LocalForgejo(tmp_path))
    fake = FakeAgent(drive_settings.drive_control_dir)
    task = asyncio.create_task(fake.run())
    try:
        assert "inbox_unc" not in await service.drive_status("owner")
        await service.add_drive_device("owner", "Laptop")
        with pytest.raises(Exception, match="does not offer an inbox share"):
            await service.inbox("owner")
        assert not list((drive_settings.drive_dir / "inbox").iterdir())
    finally:
        fake.running = False
        await task


# Audit regressions ----------------------------------------------------------


async def test_a_save_during_publication_is_kept_and_published_next(drive_service, agent, drive_settings,
                                                                    monkeypatch):
    await writer(drive_service, agent)
    page = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md"
    page.write_text("first save\n")
    push = drive_service.git._push_commit

    def save_while_pushing(*args, **kwargs):
        page.write_text("second save\n")
        return push(*args, **kwargs)

    monkeypatch.setattr(drive_service.git, "_push_commit", save_while_pushing)
    [first] = await drive_service.drive_edit_cycle(force=True)
    monkeypatch.undo()
    assert page.read_text() == "second save\n"
    assert await accepted(drive_service, "starter", "wiki/index.md") == "first save\n"
    [second] = await drive_service.drive_edit_cycle(force=True)
    assert second["revision"] and await accepted(drive_service, "starter", "wiki/index.md") == "second save\n"


async def test_invalid_pages_do_not_block_other_edits_or_repeat_jobs(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / ".trash").mkdir()
    (folder / ".trash" / "old.md").write_text("deleted elsewhere\n")
    (folder / "latin.md").write_bytes("caf\xe9\n".encode("cp1252"))
    (folder / "good.md").write_text("good\n")
    (folder / os.fsdecode(b"caf\xe9.md")).write_text("name is not UTF-8\n")
    [result] = await drive_service.drive_edit_cycle(force=True)
    assert result["published"] == ["wiki/good.md"]
    assert await drive_service.drive_edit_cycle(force=True) == []
    assert len(drive_service.state.jobs("starter")) == 1
    status = (await drive_service.drive_status("reader"))["wikis"][0]
    assert sorted(status["pages_needing_attention"]) == ["wiki/caf\ufffd.md", "wiki/latin.md"]
    json.dumps(status, ensure_ascii=False).encode("utf-8")
    files = drive_service.git.list_files(drive_service.git.sync(drive_service.state.wiki("starter")))
    assert "wiki/latin.md" not in files and not any(".trash" in f for f in files)


async def test_a_link_swapped_in_for_a_page_is_never_read_or_published(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    secret = drive_settings.state_dir / "secret.txt"
    secret.write_text("STATE SECRET\n")
    page = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md"
    page.unlink()
    page.symlink_to(secret)
    assert await drive_service.drive_edit_cycle(force=True) == []
    assert "STATE SECRET" not in await accepted(drive_service, "starter", "wiki/index.md")
    with pytest.raises(OSError):
        drive_service.drive.read_page(person(drive_service, "reader"), "starter", "wiki/index.md")


@pytest.mark.parametrize("linked", ["page", "wiki_folder"])
async def test_a_git_pushed_link_cannot_redirect_drive_publication(
        drive_service, agent, drive_settings, tmp_path, linked):
    import shutil

    await writer(drive_service, agent)
    outside = tmp_path / "outside-job"
    outside.mkdir()
    protected = outside / "draft.md"
    protected.write_text("Protected server file\n")
    if linked == "page":
        # Git blobs do not include the mode: a regular file and a symlink
        # can share an object ID, making the remote change look unchanged.
        commit(drive_service, "starter", "wiki/draft.md", str(protected))
        await drive_service.refresh_drive("starter")
    page = view(drive_settings, drive_service, "reader") / "starter/wiki/draft.md"
    page.write_text("Unpublished page edit\n")
    repo = drive_service.git.sync(drive_service.state.wiki("starter"))
    target = repo / ("wiki/draft.md" if linked == "page" else "wiki")
    if linked == "page":
        target.unlink()
        target.symlink_to(protected)
    else:
        shutil.rmtree(target)
        target.symlink_to(outside, target_is_directory=True)
    drive_service.git.run(repo, "add", "--all")
    drive_service.git.run(repo, "commit", "-m", "Direct Git push replaces content with a link")
    drive_service.git.run(repo, "push", "origin", "HEAD:main")
    revision = drive_service.git.revision(repo)

    assert await drive_service.drive_edit_cycle(force=True) == []
    assert protected.read_text() == "Protected server file\n"
    assert page.read_text() == "Unpublished page edit\n"
    assert drive_service.git.revision(drive_service.git.sync(drive_service.state.wiki("starter"))) == revision
    job = drive_service.state.jobs("starter")[0]
    assert job["status"] == "failed" and not job["candidate_revision"]


async def test_a_persons_file_where_a_folder_arrives_is_set_aside(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "guides").write_text("my notes without an extension\n")
    commit(drive_service, "starter", "wiki/guides/a.md", "accepted guide\n")
    await drive_service.refresh_drive("starter")
    assert (folder / "guides" / "a.md").read_text() == "accepted guide\n"
    kept = [p for p in folder.iterdir() if p.name.startswith("guides (your file, conflict")]
    assert kept and kept[0].read_text() == "my notes without an extension\n"
    await drive_service.share("owner", "starter", "someone", "read")


async def test_publication_waits_for_a_running_ingest_instead_of_blocking(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    (view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md").write_text("edit\n")
    async with drive_service.write_lock("starter"):
        assert await asyncio.wait_for(drive_service.drive_edit_cycle(force=True), 5) == []
    assert len(await drive_service.drive_edit_cycle(force=True)) == 1


async def test_uncertain_push_does_not_deadlock_and_cleans_its_worktree(drive_service, agent, drive_settings,
                                                                        monkeypatch):
    from wiki_hub.gitstore import GitError
    await writer(drive_service, agent)
    (view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md").write_text("edit\n")

    def lost_reply(wiki, repo, work, base, prepared=None, **_kwargs):
        prepared("0" * 40)
        raise GitError("synthetic lost push reply")

    monkeypatch.setattr(drive_service.git, "_push_commit", lost_reply)
    assert await asyncio.wait_for(drive_service.drive_edit_cycle(force=True), 20) == []
    job = drive_service.state.jobs("starter")[0]
    assert job["kind"] == "drive-edit" and job["status"] == "uncertain"
    status = (await drive_service.drive_status("reader"))["wikis"][0]
    assert status["recent_publications"][0]["status"] == "uncertain"
    assert status["recent_publications"][0]["candidate_revision"] == "0" * 40
    assert not [p for p in drive_settings.workspace_dir.iterdir() if p.name.startswith("drive-")]
    async with drive_service.lock("starter"):
        pass  # Not left held.


async def test_refused_content_waits_until_the_page_changes(drive_service, agent, drive_settings, monkeypatch):
    from wiki_hub.gitstore import ContentError
    await writer(drive_service, agent)
    page = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md"
    page.write_text("refused\n")

    def refuse(_work):
        raise ContentError("synthetic validation refusal")

    monkeypatch.setattr(drive_service.git, "validate", refuse)
    assert await drive_service.drive_edit_cycle(force=True) == []
    assert await drive_service.drive_edit_cycle(force=True) == []
    assert len(drive_service.state.jobs("starter")) == 1
    status = (await drive_service.drive_status("reader"))["wikis"][0]
    assert status["recent_publications"][0]["status"] == "failed"
    assert status["recent_publications"][0]["error"] == "synthetic validation refusal"
    monkeypatch.undo()
    page.write_text("changed\n")
    [result] = await drive_service.drive_edit_cycle(force=True)
    assert result["revision"]


def test_conflict_copies_never_overwrite_each_other(drive_service, tmp_path):
    drive = drive_service.drive
    drive._ensure_person(1)
    (drive.person_root(1) / "starter" / "wiki").mkdir(parents=True)
    first = drive.conflict_copy(1, "starter", "wiki/page.md", b"one\n", "2026-10-29 120000")
    second = drive.conflict_copy(1, "starter", "wiki/page.md", b"two\n", "2026-10-29 120000")
    assert first != second
    assert drive.read_page(1, "starter", first) == b"one\n" and drive.read_page(1, "starter", second) == b"two\n"


async def test_a_save_during_a_merged_publish_merges_on_the_next_cycle(drive_service, agent, drive_settings,
                                                                       monkeypatch):
    await writer(drive_service, agent)
    commit(drive_service, "starter", "wiki/notes.md", "one\ntwo\nthree\nfour\nfive\n")
    await drive_service.refresh_drive("starter")
    page = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "notes.md"
    page.write_text("ONE\ntwo\nthree\nfour\nfive\n")
    commit(drive_service, "starter", "wiki/notes.md", "one\ntwo\nthree\nfour\nFIVE\n")
    push = drive_service.git._push_commit

    def save_while_pushing(*args, **kwargs):
        page.write_text("ONE\ntwo\nTHREE\nfour\nfive\n")
        return push(*args, **kwargs)

    monkeypatch.setattr(drive_service.git, "_push_commit", save_while_pushing)
    [first] = await drive_service.drive_edit_cycle(force=True)
    monkeypatch.undo()
    assert first["revision"]
    [second] = await drive_service.drive_edit_cycle(force=True)
    assert second["revision"] and not second["conflicts"]
    assert await accepted(drive_service, "starter", "wiki/notes.md") == "ONE\ntwo\nTHREE\nfour\nFIVE\n"


async def test_a_long_page_name_conflict_keeps_a_copy_within_name_limits(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    name = "x" * 240 + ".md"
    commit(drive_service, "starter", "wiki/" + name, "status: draft\n")
    await drive_service.refresh_drive("starter")
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / name).write_text("status: mine\n")
    commit(drive_service, "starter", "wiki/" + name, "status: theirs\n")
    [result] = await drive_service.drive_edit_cycle(force=True)
    [copy] = result["conflicts"]
    assert len(copy.rsplit("/", 1)[-1].encode()) <= 255
    assert (folder.parent / copy).read_text() == "status: mine\n"
    assert (folder / name).read_text() == "status: theirs\n"


async def test_a_save_during_a_conflict_reset_is_not_published_over_the_accepted_change(
        drive_service, agent, drive_settings, monkeypatch):
    await writer(drive_service, agent)
    commit(drive_service, "starter", "wiki/notes.md", "status: draft\n")
    await drive_service.refresh_drive("starter")
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "notes.md").write_text("status: mine\n")
    commit(drive_service, "starter", "wiki/notes.md", "status: theirs\n")
    copy = drive_service.drive.conflict_copy

    def save_meanwhile(number, slug, path, data, stamp):
        result = copy(number, slug, path, data, stamp)
        (folder / "notes.md").write_text("status: mine again\n")
        return result

    monkeypatch.setattr(drive_service.drive, "conflict_copy", save_meanwhile)
    await drive_service.drive_edit_cycle(force=True)
    monkeypatch.undo()
    [second] = await drive_service.drive_edit_cycle(force=True)
    assert second["conflicts"] and not second["revision"]
    assert await accepted(drive_service, "starter", "wiki/notes.md") == "status: theirs\n"


async def test_a_large_file_in_a_view_is_hashed_in_chunks_not_read_whole(drive_service, agent, drive_settings,
                                                                          monkeypatch):
    from wiki_hub import drive as drive_module

    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "video.bin").write_bytes(b"x" * (drive_settings.upload_limit + 10))
    (folder / "big.md").write_bytes(b"y" * (drive_settings.upload_limit + 10))
    assert isinstance(drive_module.read_beneath(folder.parent, "wiki/video.bin", limit=drive_settings.upload_limit),
                      drive_module.LargeFile)
    assert await drive_service.drive_edit_cycle(force=True) == []
    number = person(drive_service, "reader")
    scan = drive_service.drive.scan(number, "starter", drive_service.drive.view_state(number, "starter"))
    assert "wiki/big.md" in scan["invalid"] and "wiki/video.bin" in scan["unpublishable"]


async def test_losing_write_access_keeps_every_unpublished_file(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "latin.md").write_bytes("caf\xe9 notes\n".encode("cp1252"))  # Not UTF-8: never publishable
    (folder / "report.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (folder / "good.md").write_text("good unpublished\n")
    await drive_service.archive("owner", "starter", True)
    kept = {p.name for p in (drive_settings.state_dir / "drive-unpublished").rglob("*") if p.is_file()}
    assert {"latin.md", "report.pdf", "good.md", "REASON.txt"} <= kept


async def test_keeping_or_widening_write_access_keeps_the_writers_folder(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "index.md").write_text("# Saved moments ago\n")
    (folder / "draft.pdf").write_bytes(b"%PDF-1.4 my attachment")
    await drive_service.share("owner", "starter", "reader", "admin")
    await drive_service.wiki_settings("owner", "starter", True)
    await drive_service.user_settings("owner", "reader", True)
    assert (folder / "index.md").read_text() == "# Saved moments ago\n" and (folder / "draft.pdf").exists()
    assert not list((drive_settings.state_dir / "drive-unpublished").rglob("*.md"))
    await drive_service.drive_edit_cycle(force=True)
    assert await accepted(drive_service, "starter", "wiki/index.md") == "# Saved moments ago\n"


async def test_parked_pages_are_retained_when_access_does_not_return(drive_service, agent, drive_settings,
                                                                      monkeypatch):
    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "index.md").write_text("# Saved before the unshare\n")
    await drive_service.share("owner", "starter", "reader", "none")
    assert not folder.exists()
    monkeypatch.setattr("wiki_hub.service.PARK_SECONDS", 0)
    await drive_service.drive_cycle()
    kept = list((drive_settings.state_dir / "drive-unpublished").rglob("index.md"))
    assert kept and kept[0].read_text() == "# Saved before the unshare\n"
    assert not drive_service.drive.parked_views() and not any(drive_service.drive.parked.iterdir())
    # Access granted later starts from accepted content, never the old edits.
    await drive_service.share("owner", "starter", "reader", "write")
    assert folder.joinpath("index.md").read_text().startswith("# Starter handbook")


async def test_a_save_after_the_first_look_restarts_the_quiet_period(drive_settings, tmp_path, agent):
    service = Service(replace(drive_settings, drive_edit_quiet_seconds=1), LocalForgejo(tmp_path))
    await writer(service, agent)
    page = view(drive_settings, service, "reader") / "starter" / "wiki" / "index.md"
    page.write_text("# Starter handbook\n\nComplete first save.\n")
    await service.drive_edit_cycle()
    await asyncio.sleep(1.2)
    scan = service.drive.scan
    calls = []

    def scan_then_save(*args):
        result = scan(*args)
        if not calls:
            calls.append(1)
            page.write_text("# Starter handbook\n\nHALF WRIT")
        return result

    service.drive.scan = scan_then_save
    assert await service.drive_edit_cycle() == []
    assert "HALF WRIT" not in await accepted(service, "starter", "wiki/index.md")


async def test_busy_files_do_not_delay_a_quiet_page_or_deletion(drive_service, agent, drive_settings, monkeypatch):
    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    clock = [time.time()]
    monkeypatch.setattr("wiki_hub.drive.time.time", lambda: clock[0])
    (folder / "index.md").unlink()
    (folder / "done.md").write_text("Finished page\n")
    (folder / "busy.md").write_text("First draft\n")
    assert await drive_service.drive_edit_cycle() == []
    clock[0] += drive_settings.drive_edit_quiet_seconds + 1
    (folder / "busy.md").write_text("Still editing\n")
    (folder / "attachment.bin").write_bytes(b"Not a page")
    [result] = await drive_service.drive_edit_cycle()
    assert result["published"] == ["wiki/done.md", "wiki/index.md"]
    assert await accepted(drive_service, "starter", "wiki/done.md") == "Finished page\n"
    files = drive_service.git.list_files(drive_service.git.sync(drive_service.state.wiki("starter")))
    assert "wiki/index.md" not in files and "wiki/busy.md" not in files
    clock[0] += drive_settings.drive_edit_quiet_seconds + 1
    [result] = await drive_service.drive_edit_cycle()
    assert result["published"] == ["wiki/busy.md"]


@pytest.mark.parametrize("metadata", [None, "{broken", "{}"])
async def test_rebuilding_without_view_metadata_retains_files_for_manual_recovery(
        drive_service, agent, drive_settings, tmp_path, metadata):
    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "index.md").write_text("Unpublished correction\n")
    (folder / "draft.pdf").write_bytes(b"%PDF-1.4 pending source")
    (folder / ".draft.md").write_text("Hidden draft\n")
    secret = tmp_path / "outside.txt"
    secret.write_text("Must not be copied\n")
    (folder / "link.md").symlink_to(secret)
    state_path = drive_service.drive._state_path(person(drive_service, "reader"), "starter")
    if metadata is None:
        state_path.unlink()
    else:
        state_path.write_text(metadata)
    await drive_service.refresh_drive("starter")
    retained = list(drive_service.drive.unpublished.rglob("index.md"))
    assert len(retained) == 1 and retained[0].read_text() == "Unpublished correction\n"
    recovery = retained[0].parent.parent
    assert (recovery / "wiki/draft.pdf").read_bytes() == b"%PDF-1.4 pending source"
    assert (recovery / "wiki/.draft.md").read_text() == "Hidden draft\n"
    assert not (recovery / "wiki/link.md").exists()
    assert "manual recovery" in (recovery / "REASON.txt").read_text()
    assert json.loads((recovery / "STATE.json").read_text()) is None
    assert folder.joinpath("index.md").read_text().startswith("# Starter handbook")
    assert await drive_service.drive_edit_cycle(force=True) == []


async def test_missing_parked_metadata_is_discovered_and_retained(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "index.md").write_text("Parked correction\n")
    await drive_service.share("owner", "starter", "reader", "none")
    number = person(drive_service, "reader")
    drive_service.drive._park_state_path(number, "starter").unlink()
    assert (number, "starter") in drive_service.drive.parked_views()
    await drive_service.drive_cycle()
    retained = list(drive_service.drive.unpublished.rglob("index.md"))
    assert len(retained) == 1 and retained[0].read_text() == "Parked correction\n"
    assert not drive_service.drive.parked_views()
    await drive_service.share("owner", "starter", "reader", "write")
    assert folder.joinpath("index.md").read_text().startswith("# Starter handbook")


async def test_restart_keeps_pending_edits_and_their_base(drive_service, agent, drive_settings, tmp_path):
    await writer(drive_service, agent)
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "index.md").write_text("Pending across restart\n")
    await drive_service.drive_edit_cycle()
    number = person(drive_service, "reader")
    before = drive_service.drive.view_state(number, "starter")
    restarted = Service(drive_settings, LocalForgejo(tmp_path))
    await restarted.refresh_drive("starter")
    assert folder.joinpath("index.md").read_text() == "Pending across restart\n"
    after = restarted.drive.view_state(number, "starter")
    assert after["base"] == before["base"] and after["pending"] == before["pending"]
    assert (await accepted(restarted, "starter", "wiki/index.md")).startswith("# Starter handbook")
    [result] = await restarted.drive_edit_cycle(force=True)
    assert result["published"] == ["wiki/index.md"]


async def test_recovery_copies_never_overwrite_an_earlier_attempt(drive_service, agent, drive_settings, monkeypatch):
    await writer(drive_service, agent)
    number = person(drive_service, "reader")
    page = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md"
    monkeypatch.setattr("wiki_hub.drive.time.strftime", lambda *_args: "2026-10-30 120000")
    for text in ("First retained save\n", "Second retained save\n"):
        page.write_text(text)
        drive_service.drive.retain(number, "starter", "Recovery test")
    retained = list(drive_service.drive.unpublished.rglob("index.md"))
    assert {p.read_text() for p in retained} == {"First retained save\n", "Second retained save\n"}


async def test_retention_records_pending_deletions_and_their_base(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    number = person(drive_service, "reader")
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    base = drive_service.drive.view_state(number, "starter")["base"]
    folder.joinpath("index.md").unlink()
    drive_service.drive.retain(number, "starter", "Retain a deletion")
    [snapshot] = list(drive_service.drive.unpublished.rglob("STATE.json"))
    state = json.loads(snapshot.read_text())
    assert state["base"] == base and state["pending"]["wiki/index.md"][0] is None


async def test_restart_retains_a_writer_left_in_staging(drive_service, agent, drive_settings, tmp_path):
    await writer(drive_service, agent)
    number = person(drive_service, "reader")
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    folder.joinpath("index.md").write_text("Only copy during interrupted restoration\n")
    await drive_service.share("owner", "starter", "reader", "none")
    staged = drive_service.drive.staging / f"view-{number}-starter-{'a' * 32}"
    staged.mkdir()
    (drive_service.drive._park_path(number, "starter") / "wiki").rename(staged / "wiki")
    drive_service.drive._park_state_path(number, "starter").unlink()
    restarted = Service(drive_settings, LocalForgejo(tmp_path))
    [retained] = list(restarted.drive.unpublished.rglob("index.md"))
    assert retained.read_text() == "Only copy during interrupted restoration\n"
    assert not staged.exists()
    assert "manual recovery" in retained.parent.parent.joinpath("REASON.txt").read_text()


async def test_failed_retention_does_not_discard_an_unknown_view(drive_service, agent, drive_settings, monkeypatch):
    from wiki_hub import drive as drive_module

    await writer(drive_service, agent)
    number = person(drive_service, "reader")
    page = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "index.md"
    page.write_text("Only copy of this work\n")
    drive_service.drive._state_path(number, "starter").unlink()
    original = drive_module.open_beneath

    def fail_retention(root, relative):
        if relative == "wiki/index.md":
            raise OSError("Synthetic read failure")
        return original(root, relative)

    monkeypatch.setattr(drive_module, "open_beneath", fail_retention)
    with pytest.raises(DriveError, match="could not be retained"):
        drive_service.drive.materialize(number, drive_service.state.wiki("starter"), "write")
    assert page.read_text() == "Only copy of this work\n"


async def test_drive_status_only_reports_the_callers_publications(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    reader_folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    reader_folder.joinpath("index.md").write_text("Reader's page\n")
    [reader_result] = await drive_service.drive_edit_cycle(force=True)
    await drive_service.add_drive_device("owner", "Owner desktop")
    owner_folder = view(drive_settings, drive_service, "owner") / "starter" / "wiki"
    owner_folder.joinpath("owner.md").write_text("Owner's page\n")
    [owner_result] = await drive_service.drive_edit_cycle(force=True)
    await asyncio.gather(*drive_service.tasks)
    for subject, result in (("reader", reader_result), ("owner", owner_result)):
        status = await drive_service.drive_status(subject)
        wiki = next(row for row in status["wikis"] if row["slug"] == "starter")
        assert [job["id"] for job in wiki["recent_publications"]] == [result["job"]]
        assert wiki["recent_publications"][0]["revision"] == result["revision"]
        assert "subject" not in wiki["recent_publications"][0]
        assert wiki["revision"] == drive_service.drive.view_state(person(drive_service, subject), "starter")["revision"]


@pytest.mark.parametrize("change", ["grant", "copies"])
async def test_drive_status_rechecks_access_after_scanning(drive_service, agent, change):
    await writer(drive_service, agent)
    original = drive_service._thread

    async def scan_then_revoke(function, *args):
        result = await original(function, *args)
        if function == drive_service.drive.scan:
            if change == "grant":
                drive_service.state.set_grant("starter", "reader", "none")
            else:
                drive_service.state.set_copy_setting("wiki", "starter", False)
        return result

    drive_service._thread = scan_then_revoke
    assert (await drive_service.drive_status("reader"))["wikis"] == []


async def test_a_page_whose_folder_became_a_link_is_not_deleted(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    commit(drive_service, "starter", "wiki/topic/a.md", "accepted page a\n")
    await drive_service.refresh_drive("starter")
    topic = view(drive_settings, drive_service, "reader") / "starter" / "wiki" / "topic"
    for entry in topic.iterdir():
        entry.unlink()
    topic.rmdir()
    topic.symlink_to(drive_settings.state_dir)
    assert await drive_service.drive_edit_cycle(force=True) == []
    assert await accepted(drive_service, "starter", "wiki/topic/a.md") == "accepted page a\n"


async def test_a_folder_that_becomes_a_file_does_not_block_publication(drive_service, agent, drive_settings):
    await writer(drive_service, agent)
    commit(drive_service, "starter", "wiki/topic/a.md", "accepted page a\n")
    await drive_service.refresh_drive("starter")
    folder = view(drive_settings, drive_service, "reader") / "starter" / "wiki"
    (folder / "topic" / "a.md").write_text("my edit of a\n")
    repo = drive_service.git.sync(drive_service.state.wiki("starter"))
    drive_service.git.run(repo, "rm", "-r", "-q", "wiki/topic")
    (repo / "wiki" / "topic").write_text("now a file\n")
    drive_service.git.run(repo, "add", "--all")
    drive_service.git.run(repo, "commit", "-m", "folder becomes file")
    drive_service.git.run(repo, "push", "origin", "HEAD:main")
    await drive_service.refresh_drive("starter")
    assert (folder / "topic").read_text() == "now a file\n"
    aside = [p for p in folder.iterdir() if p.name.startswith("topic (your file, conflict")]
    assert len(aside) == 1 and (aside[0] / "a.md").read_text() == "my edit of a\n"
    (folder / "index.md").write_text("# An unrelated edit\n")
    [result] = await drive_service.drive_edit_cycle(force=True)
    assert result["published"] == ["wiki/index.md"] and result["revision"]
    files = drive_service.git.list_files(drive_service.git.sync(drive_service.state.wiki("starter")))
    assert "wiki/topic" in files and "wiki/topic/a.md" not in files


def test_a_page_saved_between_the_check_and_the_swap_is_put_back(drive_settings, monkeypatch, tmp_path):
    from wiki_hub import drive as drive_module

    folder = tmp_path / "view"
    folder.mkdir()
    page = folder / "notes.md"
    page.write_bytes(b"checked\n")
    checked = drive_module.blob_oid(b"checked\n")
    real = drive_module.exchange
    calls = []

    def save_first(source, target):
        if not calls:
            page.write_bytes(b"saved meanwhile\n")
        calls.append(source)
        real(source, target)

    monkeypatch.setattr(drive_module, "exchange", save_first)
    drive = drive_module.Drive.__new__(drive_module.Drive)
    states = iter([checked])
    monkeypatch.setattr(drive, "page_oid", lambda *_args: next(states, None) or drive_module.current_oid(
        folder, "notes.md"), raising=False)
    monkeypatch.setattr(drive, "person_root", lambda _number: tmp_path, raising=False)
    monkeypatch.setattr(drive, "_make_parents", lambda *_args: None, raising=False)
    monkeypatch.setattr(drive, "_set_writable", lambda *_args: None, raising=False)
    assert drive.write_page(1, "view", "notes.md", b"published\n", expected=checked) is False
    assert page.read_bytes() == b"saved meanwhile\n" and len(calls) == 2
    assert drive.write_page(1, "view", "notes.md", b"published\n", expected=drive_module.blob_oid(
        b"saved meanwhile\n")) is True
    assert page.read_bytes() == b"published\n" and not [p for p in folder.iterdir() if p.name != "notes.md"]
