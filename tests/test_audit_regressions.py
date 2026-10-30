"""Synthetic regressions for source names and incomplete drive observations."""
import asyncio
import base64
import errno
import os
from pathlib import Path

import pytest
from test_drive import (  # noqa: F401 - shared disposable-drive fixtures
    agent,
    commit,
    drive_service,
    drive_settings,
    person,
    view,
    writer,
)

from wiki_hub.drive import DriveError
from wiki_hub.gitstore import ContentError


@pytest.mark.parametrize("filename", ["文" * 74 + ".txt", "文" * 90 + ".txt", "😀" * 56 + ".txt"],
                         ids=["prefix-overflow", "staging-overflow", "four-byte-characters"])
@pytest.mark.parametrize("attachment", [False, True])
async def test_source_names_that_cannot_be_preserved_are_refused_before_admission(service, filename, attachment):
    await service.create("owner", "starter", "Starter")
    data = base64.b64encode(b"Synthetic source.").decode()
    kwargs = {"filename": filename, "content_base64": data}
    if attachment:
        kwargs = {"filename": "message.txt", "content_base64": data, "attachments": [kwargs]}
    try:
        with pytest.raises(ContentError, match="filename"):
            await service.ingest("owner", "starter", **kwargs)
    finally:
        await asyncio.gather(*tuple(service.tasks))
    assert service.state.jobs("starter") == []
    assert not (service.settings.state_dir / "sources").exists()


async def test_unicode_source_name_with_room_for_the_job_prefix_is_preserved(service):
    await service.create("owner", "starter", "Starter")
    filename = "文" * 72 + "ab.txt"  # Exactly 222 UTF-8 bytes; 255 with the job prefix.
    source = b"The workshop opens at 09:30."
    job = await service.ingest("owner", "starter", filename, base64.b64encode(source).decode())
    await asyncio.gather(*tuple(service.tasks))
    result = await service.job("owner", job["id"])
    assert result["status"] == "completed", result
    original = service.git.original(service.state.wiki("starter"), f"raw/{job['id']}-{filename}")
    assert original.read_bytes() == source


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="Linux ACL support is required")
@pytest.mark.parametrize("blocked_folder", ["wiki", "wiki/section"])
async def test_incomplete_recovery_scan_with_missing_metadata_preserves_the_view(
        drive_service, agent, drive_settings, monkeypatch, blocked_folder):  # noqa: F811
    await writer(drive_service, agent)
    commit(drive_service, "starter", "wiki/section/note.md", "# Accepted note\n")
    await drive_service.refresh_drive("starter")
    number = person(drive_service, "reader")
    root = view(drive_settings, drive_service, "reader") / "starter"
    note = root / "wiki/section/note.md"
    note.write_text("# Unpublished local note\n")
    drive_service.drive._state_path(number, "starter").unlink()
    real_scandir = os.scandir

    def unreadable(path):
        if not isinstance(path, int) and Path(path) == root / blocked_folder:
            raise PermissionError(errno.EACCES, "Synthetic unreadable folder", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr("wiki_hub.drive.os.scandir", unreadable)
    wiki = drive_service.state.wiki("starter")
    with pytest.raises(DriveError, match="scan"):
        drive_service.drive.materialize(number, wiki, "write")
    assert note.read_text() == "# Unpublished local note\n"
    assert not drive_service.drive._state_path(number, "starter").exists()
    monkeypatch.setattr("wiki_hub.drive.os.scandir", real_scandir)
    assert drive_service.drive.materialize(number, wiki, "write")
    [retained] = list(drive_service.drive.unpublished.rglob("note.md"))
    assert retained.read_text() == "# Unpublished local note\n"
    assert note.read_text() == "# Accepted note\n"


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="Linux ACL support is required")
async def test_stale_file_server_report_cannot_confirm_revocation(drive_service, monkeypatch):  # noqa: F811
    device = drive_service.state.add_drive_device("drive-test", "owner", "Computer")
    drive_service.state.revoke_drive_device(device["id"])
    generation = drive_service.state.drive_generation()
    observation = {"age": 120, "generation": generation, "devices": {}}
    monkeypatch.setattr(drive_service.drive, "observed", lambda: observation)
    result = await drive_service.revoke_drive_device("owner", device["id"])
    assert result["pending"] is True
    assert drive_service.state.drive_device(device["id"])["revoked"] == 1
    observation["age"] = 0
    assert not (await drive_service.revoke_drive_device("owner", device["id"]))["pending"]
