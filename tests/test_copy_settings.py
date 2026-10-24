import asyncio

import pytest

from wiki_hub.forgejo import ForgejoError
from wiki_hub.service import Denied
from wiki_hub.state import State


async def test_copy_policies_control_git_without_removing_mcp_read_access(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "write")
    projections, revoked = [], []
    monkeypatch.setattr(service.forgejo, "set_collaborator", lambda *args: projections.append(args))
    monkeypatch.setattr(service.forgejo, "revoke_copies", lambda *args: revoked.append(args))
    assert await service.clone("reader", "starter")
    result = await service.wiki_settings("owner", "starter", False)
    assert result == {"wiki": "starter", "local_copies": False, "pending": False}
    assert {args[-1] for args in revoked} == {"owner", "reader"}
    assert projections[-1][-2:] == ("reader", "none")
    for user in ("owner", "reader"):
        with pytest.raises(Denied, match="Local Git copies"):
            await service.clone(user, "starter", write=False)
        assert await service.pages(user, "starter")
        assert await service.read_page(user, "starter", "wiki/index.md")
    await service.user_settings("owner", "reader", False)
    await service.wiki_settings("owner", "starter", True)
    assert await service.clone("owner", "starter")
    with pytest.raises(Denied):
        await service.clone("reader", "starter", write=False)
    await service.user_settings("owner", "reader", True)
    assert projections[-1][-2:] == ("reader", "write")
    assert await service.clone("reader", "starter")


async def test_user_copy_policy_applies_to_all_current_and_future_wikis(service):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "read")
    await service.user_settings("owner", "reader", False)
    await service.create("owner", "later", "Later")
    await service.share("owner", "later", "reader", "write")
    assert [w["slug"] for w in await service.list_wikis("reader")] == ["later", "starter"]
    for slug in ("starter", "later"):
        with pytest.raises(Denied):
            await service.clone("reader", slug, write=False)


async def test_delegated_admin_is_wiki_scoped_and_can_remove_their_own_access(service):
    await service.create("owner", "starter", "Starter")
    await service.create("owner", "private", "Private")
    await service.share("owner", "starter", "admin", "admin")
    await service.share("admin", "starter", "reader", "read")
    assert (await service.access("admin", "starter"))["owner"] == "owner"
    await service.wiki_settings("admin", "starter", False)
    for operation in (service.user_settings("admin", "reader", False),
                      service.share("admin", "private", "reader", "read"),
                      service.add_agent("admin", "outside-admin-scope")):
        with pytest.raises(Denied):
            await operation
    result = await service.share("admin", "starter", "admin", "none")
    assert all(row["subject"] != "admin" for row in result["grants"])
    with pytest.raises(Denied):
        await service.access("admin", "starter")
    assert await service.pages("owner", "starter")


async def test_failed_copy_revocation_remains_pending_across_restart_and_can_be_retried(service, monkeypatch):
    await service.create("owner", "starter", "Starter")

    def unavailable(*args):
        raise ForgejoError("Synthetic unavailable backend")

    monkeypatch.setattr(service.forgejo, "revoke_copies", unavailable)
    with pytest.raises(ForgejoError):
        await service.wiki_settings("owner", "starter", False)
    assert State(service.settings.state_dir).copy_setting("wiki", "starter") == {
        "local_copies": False, "pending": True}
    assert await service.pages("owner", "starter")
    with pytest.raises(Denied):
        await service.clone("owner", "starter")
    monkeypatch.setattr(service.forgejo, "revoke_copies", lambda *args: None)
    assert not (await service.wiki_settings("owner", "starter", False))["pending"]


async def test_clone_cannot_overtake_an_in_progress_copy_revocation(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    entered, release = asyncio.Event(), asyncio.Event()
    project = service._project_copies

    async def blocked(*args):
        entered.set()
        await release.wait()
        await project(*args)

    monkeypatch.setattr(service, "_project_copies", blocked)
    change = asyncio.create_task(service.wiki_settings("owner", "starter", False))
    await entered.wait()
    clone = asyncio.create_task(service.clone("owner", "starter"))
    await asyncio.sleep(0)
    assert not clone.done()
    release.set()
    await change
    with pytest.raises(Denied):
        await clone


async def test_user_copy_deny_also_cleans_up_a_previously_pending_share(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    service.state.set_grant("starter", "reader", "read")
    revoked = []
    monkeypatch.setattr(service.forgejo, "revoke_copies", lambda *args: revoked.append(args))
    await service.user_settings("owner", "reader", False)
    assert revoked and revoked[-1][-1] == "reader"
    assert service.state.role("reader", "starter") is None


def test_legacy_schema_upgrade_preserves_grants_jobs_agents_and_accepts_admin(service):
    state = service.state
    state.add_wiki("starter", "Starter", "owner", "backend", 1)
    state.set_grant("starter", "reader", "read")
    state.projected("starter", "reader")
    # Model the released role constraint, preserving all existing grant data.
    with state.connect() as db:
        db.execute("ALTER TABLE grants RENAME TO legacy_grants")
        db.execute("""CREATE TABLE grants(wiki TEXT,subject TEXT,
            role TEXT CHECK(role IN ('read','contribute','write','none')),pending INTEGER,updated TEXT,
            PRIMARY KEY(wiki,subject))""")
        db.execute("INSERT INTO grants SELECT * FROM legacy_grants")
        db.execute("DROP TABLE legacy_grants")
    state.add_agent("agent:fixture", "owner", "Fixture", "synthetic-key")
    state.add_job("fixture-job", "starter", "owner", "note.txt", "/fixture", "")
    upgraded = State(service.settings.state_dir)
    assert upgraded.role("reader", "starter") == "read"
    assert upgraded.token_agent("synthetic-key") == "agent:fixture"
    assert upgraded.job("fixture-job")["filename"] == "note.txt"
    upgraded.set_grant("starter", "admin", "admin")
    upgraded.projected("starter", "admin")
    assert State(service.settings.state_dir).role("admin", "starter") == "admin"
