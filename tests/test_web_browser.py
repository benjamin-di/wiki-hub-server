"""Opt-in real browser acceptance against fake OIDC and disposable Git state.

Run `make check-browser` after installing Playwright Chromium and its OS libraries.
"""

import asyncio
import os
import socket
import threading
import time
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
from test_web import FakeIdentity

from wiki_hub.auth import create_verifier
from wiki_hub.forgejo import ForgejoError
from wiki_hub.server import create_app

pytestmark = pytest.mark.skipif(not os.environ.get("WIKI_TEST_BROWSER"),
                               reason="Run make check-browser to enable Chromium")


@pytest.fixture
def running_web(settings, service):
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
        page.get_by_role("link", name="Agents", exact=True).click()
        page.get_by_label("Agent name", exact=True).fill("Synthetic helper")
        page.get_by_role("button", name="Register agent", exact=True).click()
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
        page.get_by_role("button", name="Revoke Synthetic helper", exact=True).click()
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
