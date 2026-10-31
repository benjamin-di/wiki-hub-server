"""Opt-in real browser acceptance against fake OIDC and disposable Git state.

Run `make check-browser` after installing Playwright Chromium and its OS libraries.
"""

import asyncio
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import httpx2
import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from starlette.responses import RedirectResponse
from starlette.routing import Route
from test_drive import FakeAgent
from test_web import FakeIdentity

from wiki_hub.auth import create_verifier
from wiki_hub.forgejo import ForgejoError
from wiki_hub.server import create_app
from wiki_hub.service import Denied, Service

pytestmark = pytest.mark.skipif(not os.environ.get("WIKI_TEST_BROWSER"),
                               reason="Run make check-browser to enable Chromium")


@pytest.fixture
def running_web(settings, service, request):
    if getattr(request, "param", None) == "drive":
        settings = replace(settings, drive_dir=settings.state_dir / "drive",
                           drive_control_dir=settings.state_dir / "control", drive_server="files.example.test",
                           drive_confirm_seconds=1)
        service = Service(settings, service.forgejo, runner=service.runner)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    public = f"http://127.0.0.1:{listener.getsockname()[1]}"
    settings = replace(settings, public_url=public, oidc_audience=public + "/mcp", web_client_id="wiki-web",
                       web_client_secret="synthetic-web-client-secret",
                       web_session_key="synthetic-browser-session-key-32",
                       directory_url="https://id.example.test", directory_api_key="synthetic-directory-key")
    identity = FakeIdentity(settings)
    verifier = create_verifier(settings)
    verifier._fetch_jwks = AsyncMock(return_value={"keys": [identity.key.as_dict(private=False)]})
    def identity_endpoint(request):
        response = identity.handle(request)
        if request.url.path == "/.well-known/openid-configuration":
            value = response.json()
            value["authorization_endpoint"] = public + "/synthetic-authorize"
            return httpx2.Response(200, json=value)
        return response

    async def authorize(request):
        identity.nonce = request.query_params["nonce"]
        return RedirectResponse(public + "/auth/callback?" + urlencode({
            "state": request.query_params["state"], "code": "synthetic-code"}), status_code=303)

    app = create_app(settings, service, verifier, web_transport=httpx2.MockTransport(identity_endpoint))
    if settings.drive_enabled:
        file_server = app.state.file_server = FakeAgent(settings.drive_control_dir)
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with original_lifespan(app):
                task = asyncio.create_task(file_server.run())
                try:
                    yield
                finally:
                    file_server.running = False
                    await task

        app.router.lifespan_context = lifespan
    app.router.routes.insert(0, Route("/synthetic-authorize", authorize))
    asyncio.run(service.create("owner", "starter", "Team handbook"))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("Synthetic web server did not start")
        time.sleep(.02)
    try:
        yield app, identity, public
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


def browser_sign_in(page, identity, public, subject="owner"):
    identity.subject = subject
    page.goto(public)
    page.get_by_role("link", name="Sign in with your identity provider").click()
    expect(page.get_by_role("heading", name="My wikis", exact=True)).to_be_visible()


def test_browser_full_sharing_pending_recovery_mobile_and_keyboard(running_web, monkeypatch):
    app, identity, public = running_web
    service = app.state.service
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 960})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        browser_sign_in(page, identity, public)
        page.get_by_role("link", name="Share", exact=True).click()
        expect(page.get_by_role("heading", name="Manage access")).to_be_visible()
        page.locator("#add-open").click()
        expect(page.locator("#person-search")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#add-dialog")).not_to_be_visible()
        expect(page.locator("#add-open")).to_be_focused()
        page.locator("#add-open").click()
        page.get_by_label("Name or account email").fill("Sam")
        page.get_by_role("button").filter(has_text="Sam Rivera").click()
        page.locator("#new-role").select_option("contribute")
        expect(page.locator("#role-description")).to_contain_text("Accepted ingests save automatically")
        # Selection must survive a real rejection before intent was saved.
        identity.users["reader"]["disabled"] = True
        page.locator("#add-submit").click()
        expect(page.locator("#add-error")).to_contain_text("active person")
        expect(page.locator("#add-dialog")).to_be_visible()
        expect(page.locator("#new-role")).to_have_value("contribute")
        identity.users["reader"]["disabled"] = False
        lost_reply = False

        def lose_first_reply(route):
            nonlocal lost_reply
            if route.request.method == "POST" and not lost_reply:
                route.fetch()  # Server accepted the change, but its reply never reaches the caller.
                lost_reply = True
                route.abort()
            else:
                route.continue_()

        page.route(public + "/ui/api/wikis/starter/access", lose_first_reply)
        page.locator("#add-submit").click()
        member = page.locator('[data-member="reader"]')
        expect(member.get_by_role("combobox")).to_have_value("contribute")
        expect(page.locator("#add-dialog")).not_to_be_visible()
        assert len(service.state.access_activity("starter")) == 1
        member.get_by_role("combobox").select_option("write")
        member.get_by_role("button", name="Save", exact=True).click()
        expect(page.locator("#message")).to_contain_text("confirmed")
        expect(member.get_by_role("combobox")).to_have_value("write")
        # The confirmation from adding this person can still be visible, and
        # the select already contains the unsaved draft. Wait for the saved row
        # before changing the backend used by the in-flight role update.
        expect(member.get_by_role("button", name="Save", exact=True)).not_to_be_visible()
        assert service.state.role("reader", "starter") == "write"
        original = service.forgejo.set_collaborator

        def unavailable(*_args):
            raise ForgejoError("Synthetic backend outage")

        monkeypatch.setattr(service.forgejo, "set_collaborator", unavailable)
        member.get_by_role("button", name="Remove access for Sam Rivera", exact=True).click()
        page.get_by_role("button", name="Remove access", exact=True).click()
        expect(member).to_contain_text("Removal pending")
        assert service.state.role("reader", "starter") is None
        expect(member).to_contain_text("Git or drive access may remain")
        artifacts = Path(".artifacts")
        artifacts.mkdir(exist_ok=True)
        page.screenshot(path=str(artifacts / "web-access-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 360, "height": 800})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(artifacts / "web-access-mobile.png"), full_page=True)
        monkeypatch.setattr(service.forgejo, "set_collaborator", original)
        member.get_by_role("button", name="Retry update").click()
        expect(member).to_have_count(0)
        expect(page.locator("#activity-list")).to_contain_text("removed access")
        assert service.state.grants("starter") == [] and errors == []
        context.close()
        browser.close()


def test_browser_revoked_session_clears_membership_on_focus(running_web):
    app, identity, public = running_web
    service = app.state.service
    asyncio.run(service.share("owner", "starter", "reader", "admin"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public, subject="reader")
        page.get_by_role("link", name="Share", exact=True).click()
        expect(page.locator("#member-list")).to_be_visible()
        identity.subject = "owner"
        response = httpx2.post(public + "/api/wikis/starter/unshare", json={"subject": "reader"},
                               headers={"Authorization": "Bearer " + identity.tokens()["access_token"]})
        assert response.status_code == 200
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.get_by_role("heading", name="Access unavailable")).to_be_visible()
        expect(page.locator("#member-list")).to_have_count(0)
        expect(page.locator("#main")).not_to_contain_text("Team handbook")
        browser.close()


def test_administration_automatically_resolves_names_and_retry_preserves_unsaved_settings(running_web):
    app, identity, public = running_web
    asyncio.run(app.state.service.share("owner", "starter", "reader", "read"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.goto(public + "/administration")
        row = page.locator('[data-account="reader"]')
        expect(row.get_by_role("heading")).to_have_text("Sam Rivera")
        expect(row.locator("[data-account-detail]")).to_have_text("sam@example.com")
        checkbox = row.get_by_label("Allow managed copies and drive access")
        checkbox.set_checked(not checkbox.is_checked())
        draft = checkbox.is_checked()
        identity.directory_down = True
        page.get_by_role("button", name="Refresh names", exact=True).click()
        expect(page.locator("#profile-message")).to_contain_text("could not be refreshed")
        expect(row.get_by_role("heading")).to_have_text("Sam Rivera")
        assert checkbox.is_checked() == draft
        identity.directory_down = False
        identity.users["reader"]["displayName"] = '<script>name is text</script>'
        page.get_by_role("button", name="Refresh names", exact=True).click()
        expect(row.get_by_role("heading")).to_have_text('<script>name is text</script>')
        assert row.locator("script").count() == 0 and checkbox.is_checked() == draft
        assert app.state.service.state.role("reader", "starter") == "read"
        browser.close()


def test_browser_older_refresh_cannot_replace_a_confirmed_role(running_web):
    app, identity, public = running_web
    asyncio.run(app.state.service.share("owner", "starter", "reader", "read"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.get_by_role("link", name="Share", exact=True).click()
        member = page.locator('[data-member="reader"]')
        expect(member.get_by_role("combobox")).to_have_value("read")
        held = []

        def hold_first_snapshot(route):
            if route.request.method == "GET" and not held:
                held.append((route, route.fetch()))
            else:
                route.continue_()

        page.route(public + "/ui/api/wikis/starter/access", hold_first_snapshot)
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(25)
        assert held
        member.get_by_role("combobox").select_option("write")
        member.get_by_role("button", name="Save", exact=True).click()
        expect(page.locator("#message")).to_contain_text("confirmed")
        expect(member.get_by_role("combobox")).to_have_value("write")
        expect(member.get_by_role("button", name="Save", exact=True)).not_to_be_visible()
        route, response = held[0]
        route.fulfill(response=response)
        page.wait_for_load_state("networkidle")
        expect(member.get_by_role("combobox")).to_have_value("write")
        assert app.state.service.state.role("reader", "starter") == "write"
        browser.close()


def test_browser_restored_share_page_rechecks_revoked_access(running_web):
    app, identity, public = running_web
    service = app.state.service
    asyncio.run(service.share("owner", "starter", "reader", "admin"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public, subject="reader")
        page.goto(public + "/wikis/starter/access")
        expect(page.locator('[data-member="reader"]')).to_be_visible()
        page.locator("#add-open").click()
        identity.subject = "owner"
        response = httpx2.post(public + "/api/wikis/starter/unshare", json={"subject": "reader"},
                               headers={"Authorization": "Bearer " + identity.tokens()["access_token"]})
        assert response.status_code == 200
        # A browser restoring a document need not emit focus or visibilitychange.
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.get_by_role("heading", name="Access unavailable")).to_be_visible()
        expect(page.locator("#member-list")).to_have_count(0)
        expect(page.locator("#add-dialog")).to_have_count(0)
        browser.close()


def test_browser_saving_one_member_preserves_another_members_draft(running_web):
    app, identity, public = running_web
    for subject in ("reader", "other"):
        asyncio.run(app.state.service.share("owner", "starter", subject, "read"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.get_by_role("link", name="Share", exact=True).click()
        reader = page.locator('[data-member="reader"]')
        other = page.locator('[data-member="other"]')
        expect(reader.get_by_role("combobox")).to_have_value("read")
        reader.get_by_role("combobox").select_option("write")
        other.get_by_role("combobox").select_option("contribute")
        reader.get_by_role("button", name="Save", exact=True).click()
        expect(page.locator("#message")).to_contain_text("confirmed")
        expect(reader.get_by_role("combobox")).to_have_value("write")
        expect(reader.get_by_role("button", name="Save", exact=True)).not_to_be_visible()
        expect(other.get_by_role("combobox")).to_have_value("contribute")
        expect(other.get_by_role("button", name="Save", exact=True)).to_be_visible()
        assert app.state.service.state.role("other", "starter") == "read"
        browser.close()


def test_browser_agent_secret_is_once_only_and_registration_grants_nothing(running_web):
    app, identity, public = running_web
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 360, "height": 800})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        browser_sign_in(page, identity, public)
        page.get_by_role("link", name="Wiki access", exact=True).click()
        page.get_by_text("Register a service", exact=True).click()
        page.get_by_label("Service name", exact=True).fill("Synthetic helper")
        page.get_by_role("button", name="Register service", exact=True).click()
        expect(page.locator("#credential-dialog")).to_be_visible()
        secret = page.locator("#credential-values dd").nth(1).inner_text()
        assert secret.startswith("whagent_")
        agent = app.state.service.state.token_agent(secret)
        assert app.state.service.state.role(agent, 'starter') is None
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        page.keyboard.press("Escape")
        expect(page.locator("#credential-dialog")).to_be_visible()
        page.get_by_role("button", name="I have saved them").click()
        expect(page.get_by_role("heading", name="Synthetic helper", exact=True)).to_be_visible()
        assert secret not in page.content()
        page.on("dialog", lambda dialog: dialog.accept())
        page.get_by_text("Edit service details", exact=True).click()
        page.get_by_label("Name", exact=True).fill("Workshop filing")
        page.get_by_label("Purpose", exact=True).fill("Files workshop sources")
        page.get_by_role("button", name="Save details", exact=True).click()
        expect(page.get_by_role("heading", name="Workshop filing", exact=True)).to_be_visible()
        expect(page.locator("main")).to_contain_text("Files workshop sources")
        page.get_by_role("button", name="Revoke Workshop filing", exact=True).click()
        expect(page.locator("main")).to_contain_text("Revoked")
        assert app.state.service.state.token_agent(secret) is None
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
        browser.close()


def test_browser_settings_pending_retry_and_revoked_manager_focus(running_web, monkeypatch):
    app, identity, public = running_web
    service = app.state.service
    asyncio.run(service.share('owner', 'starter', 'reader', 'admin'))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public, subject='reader')
        page.goto(public + '/wikis/starter/settings')
        page.on('dialog', lambda dialog: dialog.accept())
        original = service.forgejo.revoke_copies

        def unavailable(*_args):
            raise ForgejoError('Synthetic backend unavailable')

        monkeypatch.setattr(service.forgejo, 'revoke_copies', unavailable)
        page.get_by_label('Allow managed copies and drive access').uncheck()
        page.get_by_role('button', name='Save copy settings', exact=True).click()
        expect(page.locator('#message')).to_contain_text('pending')
        assert service.state.copy_setting('wiki', 'starter')['pending']
        page.get_by_role('link', name='Refresh status', exact=True).click()
        expect(page.get_by_role('button', name='Retry copy settings', exact=True)).to_be_visible()
        monkeypatch.setattr(service.forgejo, 'revoke_copies', original)
        page.get_by_role('button', name='Retry copy settings', exact=True).click()
        expect(page.get_by_role('button', name='Save copy settings', exact=True)).to_be_visible()
        assert not service.state.copy_setting('wiki', 'starter')['pending']
        identity.subject = 'owner'
        response = httpx2.post(public + '/api/wikis/starter/share', json={'subject': 'reader', 'role': 'read'},
                               headers={'Authorization': 'Bearer ' + identity.tokens()['access_token']})
        assert response.status_code == 200
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.get_by_role('heading', name='Access unavailable', exact=True)).to_be_visible()
        expect(page.get_by_label('Allow managed copies and drive access')).to_have_count(0)
        browser.close()


def test_browser_lost_agent_key_requires_review_and_revoke(running_web):
    app, identity, public = running_web
    service = app.state.service
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.goto(public + '/agents')

        def lose_reply(route):
            route.fetch()
            route.abort()

        page.route(public + '/ui/api/agents', lose_reply)
        page.get_by_text('Register a service', exact=True).click()
        page.get_by_label('Service name', exact=True).fill('Lost key helper')
        register = page.get_by_role('button', name='Register service', exact=True)
        register.click()
        expect(page.locator('#message')).to_contain_text('key was not received')
        expect(register).to_be_disabled()
        # Enter or another submit event must not make a duplicate either.
        page.locator('.manage-form').last.evaluate('(form) => form.requestSubmit()')
        assert len(service.state.agents('owner')) == 1
        page.get_by_role('link', name='Review agents', exact=True).click()
        expect(page.get_by_role('heading', name='Lost key helper', exact=True)).to_be_visible()
        page.on('dialog', lambda dialog: dialog.accept())
        page.get_by_role('button', name='Revoke Lost key helper', exact=True).click()
        expect(page.locator('main')).to_contain_text('Revoked')
        assert service.state.agents('owner')[0]['revoked']
        page.unroute(public + '/ui/api/agents', lose_reply)
        page.get_by_text('Register a service', exact=True).click()
        page.get_by_label('Service name', exact=True).fill('Replacement helper')
        page.get_by_role('button', name='Register service', exact=True).click()
        expect(page.locator('#credential-dialog')).to_be_visible()
        assert len(service.state.agents('owner')) == 2
        assert not service.state.grants('starter')
        browser.close()


def test_browser_partial_profile_refresh_reports_directory_without_access_warning(running_web):
    app, identity, public = running_web
    service = app.state.service
    asyncio.run(service.share('owner', 'starter', 'reader', 'read'))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.goto(public + '/wikis/starter/access')
        identity.directory_down = True
        page.get_by_role('button', name='Refresh member names', exact=True).click()
        expect(page.locator('#message')).to_contain_text('Some member names could not be refreshed')
        expect(page.locator('#message')).to_contain_text('wiki access has not changed')
        expect(page.locator('#message')).not_to_contain_text('previous backend state')
        assert service.state.role('reader', 'starter') == 'read'
        identity.directory_down = False
        page.get_by_role('button', name='Refresh member names', exact=True).click()
        expect(page.locator('#message')).to_be_empty()
        expect(page.get_by_role('heading', name='Manage access', exact=True)).to_be_visible()
        browser.close()


@pytest.mark.parametrize('running_web', ['drive'], indirect=True)
def test_browser_device_lost_password_and_connected_revocation(running_web, monkeypatch):
    app, identity, public = running_web
    service, file_server = app.state.service, app.state.file_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 360, 'height': 800})
        browser_sign_in(page, identity, public)
        page.goto(public + '/connections')

        def lose_reply(route):
            route.fetch()
            route.abort()

        page.route(public + '/ui/api/devices', lose_reply)
        page.get_by_label('Device name', exact=True).fill('Lost password laptop')
        page.get_by_role('button', name='Create device credentials', exact=True).click()
        expect(page.locator('#message')).to_contain_text('password was not received')
        expect(page.get_by_role('button', name='Create device credentials')).to_be_disabled()
        assert len(service.state.drive_devices('owner')) == 1
        assert 'wd1' in file_server.passdb
        file_server.connected.add('wd1')
        page.get_by_role('link', name='Review devices', exact=True).click()
        expect(page.get_by_role('heading', name='Lost password laptop', exact=True)).to_be_visible()
        expect(page.locator('main')).to_contain_text('wd1')
        apply = file_server.apply
        monkeypatch.setattr(file_server, 'apply', lambda: None)
        page.on('dialog', lambda dialog: dialog.accept())
        page.get_by_role('button', name='Revoke Lost password laptop', exact=True).click()
        expect(page.locator('#message')).to_contain_text('pending file-server confirmation')
        assert 'wd1' in file_server.passdb
        page.get_by_role('link', name='Refresh status', exact=True).click()
        expect(page.get_by_role('button', name='Retry revocation of Lost password laptop')).to_be_visible()
        monkeypatch.setattr(file_server, 'apply', apply)
        page.get_by_role('button', name='Retry revocation of Lost password laptop').click()
        expect(page.get_by_role('button', name='Retry revocation of Lost password laptop')).to_have_count(0)
        assert 'wd1' not in file_server.passdb and 'wd1' not in file_server.connected
        page.unroute(public + '/ui/api/devices', lose_reply)
        page.get_by_label('Device name', exact=True).fill('Replacement laptop')
        page.get_by_role('button', name='Create device credentials', exact=True).click()
        expect(page.locator('#credential-dialog')).to_be_visible()
        secret = page.locator('#credential-values dd').last.inner_text()
        assert secret in file_server.passdb.values()
        page.get_by_role('button', name='I have saved them', exact=True).click()
        assert secret not in page.content()
        assert page.evaluate('localStorage.length + sessionStorage.length') == 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        browser.close()


def test_browser_owner_suspension_pending_retry_and_existing_session(running_web, monkeypatch):
    app, identity, public = running_web
    service = app.state.service
    asyncio.run(service.share('owner', 'starter', 'reader', 'read'))
    # A workload bound to this person must be blocked along with its owner.
    owned_agent = asyncio.run(service.add_workload_agent('owner', 'Reader helper', 'synthetic-helper', 'reader'))
    asyncio.run(service.share('owner', 'starter', owned_agent['id'], 'read'))
    service.allowed(owned_agent['id'], 'starter')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        owner = browser.new_page(viewport={'width': 360, 'height': 800})
        reader = browser.new_page()
        browser_sign_in(reader, identity, public, subject='reader')
        browser_sign_in(owner, identity, public)
        owner.goto(public + '/administration')
        assert owner.evaluate('document.documentElement.scrollWidth <= innerWidth')
        account = owner.locator('section.panel').filter(has=owner.locator('input[name=subject][value=reader]'))
        expect(account).to_have_count(1)
        owner.on('dialog', lambda dialog: dialog.accept())
        original = service.forgejo.revoke_copies
        monkeypatch.setattr(service.forgejo, 'revoke_copies',
                            lambda *_: (_ for _ in ()).throw(ForgejoError('Synthetic outage')))
        account.get_by_label('Suspend this account and its owned agents').check()
        account.get_by_role('button', name='Save account status', exact=True).click()
        expect(owner.locator('#message')).to_contain_text('account update is pending')
        assert service.state.user_status('reader')['pending']
        with pytest.raises(Denied):
            service.allowed(owned_agent['id'], 'starter')
        reader.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(reader.get_by_role('heading', name='Access unavailable', exact=True)).to_be_visible()
        owner.get_by_role('link', name='Refresh status', exact=True).click()
        expect(account.get_by_role('button', name='Retry account update', exact=True)).to_be_visible()
        monkeypatch.setattr(service.forgejo, 'revoke_copies', original)
        account.get_by_role('button', name='Retry account update', exact=True).click()
        expect(account.get_by_role('button', name='Save account status', exact=True)).to_be_visible()
        assert not service.state.user_status('reader')['pending']
        assert service.state.user_status('reader')['suspended']
        account.get_by_label('Suspend this account and its owned agents').uncheck()
        with owner.expect_navigation():
            account.get_by_role('button', name='Save account status', exact=True).click()
        expect(account.get_by_label('Suspend this account and its owned agents')).not_to_be_checked()
        assert not service.state.user_status('reader')['suspended']
        assert service.state.role('reader', 'starter') == 'read'
        service.allowed(owned_agent['id'], 'starter')
        browser.close()


def test_browser_archive_pending_retry_preserves_read_access(running_web, monkeypatch):
    app, identity, public = running_web
    service = app.state.service
    asyncio.run(service.share('owner', 'starter', 'reader', 'write'))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.goto(public + '/wikis/starter/settings')
        page.on('dialog', lambda dialog: dialog.accept())
        original = service.forgejo.set_archived
        monkeypatch.setattr(service.forgejo, 'set_archived',
                            lambda *_: (_ for _ in ()).throw(ForgejoError('Synthetic outage')))
        page.get_by_label('Content access', exact=True).select_option('true')
        page.get_by_role('button', name='Save archive setting', exact=True).click()
        expect(page.locator('#message')).to_contain_text('Content remains read only')
        service.allowed('reader', 'starter')
        with pytest.raises(Denied):
            service.allowed('reader', 'starter', 'write')
        page.get_by_role('link', name='Refresh status', exact=True).click()
        expect(page.get_by_role('button', name='Retry archive update', exact=True)).to_be_visible()
        monkeypatch.setattr(service.forgejo, 'set_archived', original)
        with page.expect_navigation():
            page.get_by_role('button', name='Retry archive update', exact=True).click()
        assert service.state.wiki('starter')['status'] == 'archived'
        service.allowed('reader', 'starter')
        with pytest.raises(Denied):
            service.allowed('reader', 'starter', 'write')
        page.get_by_label('Content access', exact=True).select_option('false')
        with page.expect_navigation():
            page.get_by_role('button', name='Save archive setting', exact=True).click()
        service.allowed('reader', 'starter', 'write')
        assert service.state.role('reader', 'starter') == 'write'
        browser.close()


def test_browser_lost_copy_setting_reply_refreshes_current_state(running_web):
    app, identity, public = running_web
    service = app.state.service
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.goto(public + '/wikis/starter/settings')
        page.on('dialog', lambda dialog: dialog.accept())

        def lose_reply(route):
            route.fetch()
            route.abort()

        endpoint = public + '/ui/api/wikis/starter/settings'
        page.route(endpoint, lose_reply)
        page.get_by_label('Allow managed copies and drive access').uncheck()
        page.get_by_role('button', name='Save copy settings', exact=True).click()
        expect(page.locator('#message')).to_contain_text('Refresh status before retrying')
        assert not service.state.copy_setting('wiki', 'starter')['local_copies']
        page.get_by_role('link', name='Refresh status', exact=True).click()
        expect(page.get_by_label('Allow managed copies and drive access')).not_to_be_checked()
        assert not service.state.copy_setting('wiki', 'starter')['pending']
        browser.close()


@pytest.mark.parametrize('snapshot_ok', [True, False])
@pytest.mark.parametrize('lost_reply', [False, True])
def test_browser_share_confirmation_waits_for_current_snapshot(running_web, snapshot_ok, lost_reply):
    app, identity, public = running_web
    service = app.state.service
    asyncio.run(service.share('owner', 'starter', 'reader', 'read'))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.goto(public + '/wikis/starter/access')
        member = page.locator('[data-member=reader]')
        expect(member.get_by_role('combobox')).to_have_value('read')
        held = []

        def hold_snapshot(route):
            if route.request.method == 'GET':
                held.append(route)
            elif lost_reply:
                route.fetch()
                route.abort()
            else:
                route.continue_()

        endpoint = public + '/ui/api/wikis/starter/access'
        page.route(endpoint, hold_snapshot)
        member.get_by_role('combobox').select_option('write')
        with page.expect_request(lambda request: request.url == endpoint and request.method == 'GET'):
            member.get_by_role('button', name='Save', exact=True).click()
        if not lost_reply:
            expect(page.locator('#message')).to_contain_text('Refreshing current access')
        expect(page.locator('#message')).not_to_contain_text('confirmed')
        assert service.state.role('reader', 'starter') == 'write'
        assert len(held) == 1
        route = held.pop()
        if snapshot_ok:
            route.continue_()
            expect(page.locator('#message')).to_contain_text('confirmed')
        else:
            route.fulfill(status=503, json={'error': 'Synthetic snapshot outage'})
            expect(page.locator('#message')).to_contain_text('Reload access to check')
            expect(page.locator('#message')).not_to_contain_text('confirmed')
        page.unroute(endpoint, hold_snapshot)
        if not snapshot_ok:
            page.get_by_role('button', name='Reload access', exact=True).click()
            expect(page.locator('#stale-notice')).not_to_be_visible()
        member.get_by_role('button', name='Remove access for Sam Rivera', exact=True).click()
        page.get_by_role('button', name='Remove access', exact=True).click()
        expect(member).to_have_count(0)
        assert not service.state.grants('starter')
        browser.close()


def test_browser_create_first_source_recovery_and_private_mobile(running_web):
    app, identity, public = running_web
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        browser_sign_in(page, identity, public)
        page.get_by_role("button", name="Create wiki").click()
        expect(page.get_by_label("Wiki title")).to_be_focused()
        page.get_by_label("Wiki title").fill("Café plans")
        page.get_by_label("Purpose").fill("Keep our workshop plans together")
        expect(page.locator("#wiki-slug")).to_have_value("cafe-plans")
        expect(page.locator("#create-advanced")).not_to_have_attribute("open", "")
        page.get_by_role("button", name="Create wiki", exact=True).click()
        expect(page).to_have_url(public + "/wikis/cafe-plans")
        expect(page.get_by_role("heading", name="Add your first source")).to_be_visible()
        expect(page.get_by_text("Only you have access", exact=True)).to_be_visible()
        assert app.state.service.state.grants("cafe-plans") == []
        page.get_by_label("Paste text", exact=True).check()
        page.get_by_label("Source name").fill("Planning note")
        text = "Café workshop doors open at 09:30.\nBring a notebook.\n"
        page.get_by_label("Text", exact=True).fill(text)
        lost = False
        def lose_reply(route):
            nonlocal lost
            if route.request.method == "POST" and not lost:
                route.fetch()
                lost = True
                route.abort()
            else:
                route.continue_()
        page.route(public + "/ui/api/wikis/cafe-plans/sources", lose_reply)
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-message")).to_contain_text("could not be confirmed")
        expect(page.get_by_label("Text", exact=True)).to_have_value(text)
        edited = text + "New unsent idea: bring a camera."
        page.get_by_label("Text", exact=True).fill(edited)
        page.get_by_role("button", name="Check submission").click()
        expect(page.locator("#source-message")).to_contain_text("unsent source is still here")
        expect(page.get_by_label("Text", exact=True)).to_have_value(edited)
        expect(page.locator("#source-jobs")).to_contain_text("Saved automatically", timeout=15000)
        assert len(app.state.service.state.jobs("cafe-plans")) == 1
        job = app.state.service.state.jobs("cafe-plans")[0]
        original = app.state.service.git.original(app.state.service.state.wiki("cafe-plans"),
                                                 f"raw/{job['id']}-Planning note.txt").read_bytes()
        assert original.decode() == text
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=".artifacts/first-source-mobile.png", full_page=True)
        assert not errors
        browser.close()


def test_browser_create_collision_advanced_unicode_and_file_upload(running_web):
    app, identity, public = running_web
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.get_by_role("button", name="Create wiki").click()
        page.get_by_label("Wiki title").fill("Starter")
        page.get_by_role("button", name="Create wiki", exact=True).click()
        expect(page.locator("#create-error")).to_contain_text("already in use")
        expect(page.get_by_label("Short name")).to_be_focused()
        page.get_by_label("Short name").fill("custom-notes")
        page.get_by_label("Wiki title").fill("旅行")
        expect(page.get_by_label("Short name")).to_have_value("custom-notes")
        page.get_by_role("button", name="Create wiki", exact=True).click()
        expect(page).to_have_url(public + "/wikis/custom-notes")
        page.get_by_label("File", exact=True).set_input_files({
            "name": "file-note.txt", "mimeType": "text/plain", "buffer": b"Workshop file source"})
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-jobs")).to_contain_text("Saved automatically", timeout=15000)
        assert len(app.state.service.state.jobs("custom-notes")) == 1
        browser.close()


def test_browser_source_failed_job_never_retries_and_connected_demotion_hides_form(running_web):
    app, identity, public = running_web
    service = app.state.service
    async def fail(*args):
        raise RuntimeError("Synthetic model unavailable")
    service.runner = fail
    asyncio.run(service.share("owner", "starter", "reader", "contribute"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public, "reader")
        page.get_by_role("link", name="Team handbook", exact=True).click()
        page.get_by_label("File", exact=True).set_input_files({
            "name": "failed-note.txt", "mimeType": "text/plain", "buffer": b"Workshop source"})
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-jobs")).to_contain_text("Needs review · failed", timeout=15000)
        page.get_by_role("button", name="Refresh status").click()
        expect(page.locator("#source-jobs")).to_contain_text("not be retried automatically")
        assert len(service.state.jobs("starter")) == 1
        identity.subject = "owner"
        owner_headers = {"Authorization": "Bearer " + identity.tokens()["access_token"]}
        response = httpx2.post(public + "/api/wikis/starter/share", json={"subject": "reader", "role": "read"},
                               headers=owner_headers)
        assert response.status_code == 200
        identity.subject = "reader"
        page.get_by_role("button", name="Refresh status").click()
        expect(page.locator("#source-form")).not_to_be_visible()
        response = httpx2.post(public + "/api/wikis/starter/unshare", json={"subject": "reader"},
                               headers=owner_headers)
        assert response.status_code == 200
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.get_by_role("heading", name="Access unavailable")).to_be_visible()
        browser.close()


def test_browser_reload_after_unreceived_source_retries_same_identity(running_web):
    app, identity, public = running_web
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.get_by_role("link", name="Team handbook", exact=True).click()
        page.get_by_label("Paste text", exact=True).check()
        page.get_by_label("Source name").fill("Retry note")
        page.get_by_label("Text", exact=True).fill("Workshop source to retain")
        def refuse(route):
            if route.request.method == "POST":
                route.abort()  # The server never received this source.
            else:
                route.continue_()
        page.route(public + "/ui/api/wikis/starter/sources", refuse)
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-message")).to_contain_text("could not be confirmed")
        page.unroute(public + "/ui/api/wikis/starter/sources", refuse)
        checks = []
        def hold_check(route):
            checks.append(route)
        page.route(public + "/ui/api/wikis/starter/submission?*", hold_check)
        page.reload()
        expect(page.get_by_role("button", name="Add source", exact=True)).to_be_disabled()
        expect(page.get_by_label("File", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Add a different source")).to_be_disabled()
        assert checks
        checks[0].continue_()
        page.unroute(public + "/ui/api/wikis/starter/submission?*", hold_check)
        expect(page.locator("#source-message")).to_contain_text("No saved submission found")
        page.get_by_label("Paste text", exact=True).check()
        page.get_by_label("Source name").fill("Retry note")
        page.get_by_label("Text", exact=True).fill("Different source")
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-message")).to_contain_text("retry with the same name and source")
        page.get_by_label("Text", exact=True).fill("Workshop source to retain")
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-jobs")).to_contain_text("Saved automatically", timeout=15000)
        assert len(app.state.service.state.jobs("starter")) == 1
        browser.close()


@pytest.mark.parametrize("original_kind", ["file", "text"])
def test_browser_confirmed_source_preserves_and_reveals_other_mode_draft(running_web, original_kind):
    app, identity, public = running_web
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.get_by_role("link", name="Team handbook", exact=True).click()
        if original_kind == "text":
            page.get_by_label("Paste text", exact=True).check()
            page.get_by_label("Source name").fill("Original note")
            page.get_by_label("Text", exact=True).fill("Original workshop source")
        else:
            page.get_by_label("File", exact=True).set_input_files({
                "name": "original.txt", "mimeType": "text/plain", "buffer": b"Original workshop source"})
        lost = False
        def lose_reply(route):
            nonlocal lost
            if route.request.method == "POST" and not lost:
                route.fetch()
                lost = True
                route.abort()
            else:
                route.continue_()
        page.route(public + "/ui/api/wikis/starter/sources", lose_reply)
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-message")).to_contain_text("could not be confirmed")
        if original_kind == "file":
            page.get_by_label("Paste text", exact=True).check()
            page.get_by_label("Source name").fill("Unsent note")
            page.get_by_label("Text", exact=True).fill("Keep this unsent draft")
            page.get_by_label("Upload a file", exact=True).check()
        else:
            page.get_by_label("Upload a file", exact=True).check()
            page.get_by_label("File", exact=True).set_input_files({
                "name": "unsent.txt", "mimeType": "text/plain", "buffer": b"Keep this unsent file"})
            page.get_by_label("Paste text", exact=True).check()
        page.get_by_role("button", name="Check submission").click()
        expect(page.locator("#source-message")).to_contain_text("unsent source is still here")
        if original_kind == "file":
            expect(page.get_by_label("Paste text", exact=True)).to_be_checked()
            expect(page.get_by_label("Source name")).to_have_value("Unsent note")
            expect(page.get_by_label("Text", exact=True)).to_have_value("Keep this unsent draft")
            assert page.get_by_label("File", exact=True).input_value() == ""
        else:
            expect(page.get_by_label("Upload a file", exact=True)).to_be_checked()
            assert page.get_by_label("File", exact=True).input_value().endswith("unsent.txt")
            assert page.get_by_label("Source name").input_value() == ""
            assert page.get_by_label("Text", exact=True).input_value() == ""
        assert len(app.state.service.state.jobs("starter")) == 1
        browser.close()


def test_browser_denied_retry_retains_an_earlier_unconfirmed_submission(running_web):
    app, identity, public = running_web
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        page.get_by_role("link", name="Team handbook", exact=True).click()
        expect(page.locator("#source-jobs")).to_contain_text("No sources submitted yet.")
        page.get_by_label("File", exact=True).set_input_files({
            "name": "archive-retry.txt", "mimeType": "text/plain", "buffer": b"Original workshop source"})
        submissions = []
        def lose_first_reply(route):
            if route.request.method == "POST":
                submissions.append(route.request.post_data_json["submission_id"])
                if len(submissions) == 1:
                    route.fetch()
                    route.abort()
                    return
            route.continue_()
        page.route(public + "/ui/api/wikis/starter/sources", lose_first_reply)
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-message")).to_contain_text("could not be confirmed")
        expect(page.locator("#source-jobs")).to_contain_text("Saved automatically", timeout=15000)
        headers = {"Authorization": "Bearer " + identity.tokens()["access_token"]}
        result = httpx2.post(public + "/api/wikis/starter/archive", json={"archived": True}, headers=headers)
        assert result.status_code == 200
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-message")).to_contain_text("earlier submission is still unconfirmed")
        assert page.evaluate("sessionStorage.getItem('wiki-submission:owner:starter') !== null")
        result = httpx2.post(public + "/api/wikis/starter/archive", json={"archived": False}, headers=headers)
        assert result.status_code == 200
        page.get_by_role("button", name="Add source", exact=True).click()
        expect(page.locator("#source-message")).to_contain_text("Source received")
        assert len(submissions) == 3 and len(set(submissions)) == 1
        assert len(app.state.service.state.jobs("starter")) == 1
        browser.close()


def test_browser_lost_reply_queues_refresh_after_older_empty_snapshot(running_web):
    app, identity, public = running_web
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_sign_in(page, identity, public)
        held = []
        reads = []
        submissions = []

        def requests(route):
            if route.request.method == 'GET':
                reads.append(route.request.url)
                if len(reads) == 1:
                    response = route.fetch()
                    assert response.json()['jobs'] == []
                    held.append((route, response))
                    return
            else:
                submissions.append(route.request.post_data_json['submission_id'])
                route.fetch()
                route.abort()
                return
            route.continue_()

        page.route(public + '/ui/api/wikis/starter/sources', requests)
        page.get_by_role('link', name='Team handbook', exact=True).click()
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert held
        page.get_by_label('File', exact=True).set_input_files({
            'name': 'held-refresh.txt', 'mimeType': 'text/plain', 'buffer': b'Synthetic workshop source'})
        page.get_by_role('button', name='Add source', exact=True).click()
        expect(page.locator('#source-message')).to_contain_text('could not be confirmed')
        expect(page.get_by_role('button', name='Add source', exact=True)).to_be_enabled()
        assert len(reads) == 1  # Recovery is queued behind the old response.
        assert page.evaluate("sessionStorage.getItem('wiki-submission:owner:starter') !== null")
        assert page.get_by_label('File', exact=True).input_value().endswith('held-refresh.txt')
        route, response = held[0]
        route.fulfill(response=response)
        expect(page.locator('#source-jobs')).to_contain_text('Saved automatically', timeout=15000)
        assert len(reads) >= 2
        assert len(submissions) == 1
        assert len(app.state.service.state.jobs('starter')) == 1
        assert page.evaluate("sessionStorage.getItem('wiki-submission:owner:starter') !== null")
        assert page.get_by_label('File', exact=True).input_value().endswith('held-refresh.txt')
        page.get_by_role('button', name='Check submission', exact=True).click()
        expect(page.locator('#source-message')).to_contain_text('Source received')
        assert page.evaluate("sessionStorage.getItem('wiki-submission:owner:starter') === null")
        browser.close()
