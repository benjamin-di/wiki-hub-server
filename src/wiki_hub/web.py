"""Same-origin browser client. Cookie authentication never reaches bearer APIs."""

import asyncio
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from importlib.resources import files
from urllib.parse import parse_qs, quote, urlsplit
from weakref import WeakValueDictionary

from authlib.integrations.base_client.errors import OAuthError
from jinja2 import Environment, FileSystemLoader, select_autoescape
from joserfc.errors import JoseError
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__
from .drive import DriveError
from .forgejo import ForgejoError
from .gitstore import ContentError, GitError
from .service import ARCHIVE_STATES, ROLES, Busy, Denied
from .state import AccessConflict
from .web_identity import BrowserIdentity, IdentityUnavailable, PocketDirectory
from .web_state import BrowserState

log = logging.getLogger(__name__)
LABELS = {"read": "Viewer", "contribute": "Contributor", "write": "Editor", "admin": "Manager", "none": "No access"}
DESCRIPTIONS = {
    "read": "Read pages, sources and history.",
    "contribute": "Also submit sources. Accepted ingests save automatically.",
    "write": "Also edit through Git or drive and restore content history.",
    "admin": "Also share this wiki with other people and manage its settings.",
}
HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class LoginRequired(Exception):
    pass


def local_return(value):
    # Only routes owned by this UI, without query strings, encoded separators or controls.
    if value in ("/", "/connections", "/agents", "/administration"):
        return value
    if (isinstance(value, str) and len(value) < 250 and value.startswith("/wikis/")
            and all(c.isalnum() or c in "/-_" for c in value)):
        return value
    return "/"


class BrowserWeb:
    def __init__(self, settings, service, verifier, transport=None):
        self.settings, self.service = settings, service
        self.enabled = bool(settings.web_client_id and settings.web_session_key)
        if self.enabled:
            parsed = urlsplit(settings.public_url)
            if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in
                                                  ("localhost", "127.0.0.1", "::1")):
                raise ValueError("Browser login requires HTTPS (or a loopback development origin).")
        self.secure = settings.public_url.startswith("https://")
        self.cookie = "__Host-wiki_session" if self.secure else "wiki_session"
        self.login_cookie = "__Host-wiki_login" if self.secure else "wiki_login"
        self.store = BrowserState(service.state, settings.web_session_key) if self.enabled else None
        self.identity = BrowserIdentity(settings, verifier, transport)
        self.directory = PocketDirectory(settings, self.identity)
        self.refresh_locks = WeakValueDictionary()
        self.searches = defaultdict(deque)
        self.templates = Environment(loader=FileSystemLoader(str(files("wiki_hub").joinpath("web/templates"))),
                                     autoescape=select_autoescape(["html"]))
        self.templates.globals.update(labels=LABELS, descriptions=DESCRIPTIONS, version=__version__)

    def routes(self):
        return [Route("/", self.page), Route("/wikis/{slug}", self.page),
                Route("/wikis/{slug}/access", self.page), Route("/wikis/{slug}/settings", self.page),
                Route("/connections", self.page), Route("/agents", self.page), Route("/administration", self.page),
                Route("/auth/backchannel-logout", self.provider_logout, methods=["POST"]),
                Route("/auth/login", self.login),
                Route("/auth/callback", self.callback), Route("/auth/logout", self.logout, methods=["POST"]),
                Route("/ui/assets/{name}", self.asset),
                Route("/ui/api/{path:path}", self.api, methods=["GET", "POST"])]

    def render(self, template, *, status=200, **values):
        return HTMLResponse(self.templates.get_template(template + ".html").render(**values),
                            status_code=status, headers=HEADERS)

    def set_cookie(self, response, name, value, age):
        response.set_cookie(name, value, max_age=age, httponly=True, secure=self.secure, samesite="lax", path="/")

    def clear_cookie(self, response, name):
        response.delete_cookie(name, path="/", secure=self.secure, httponly=True, samesite="lax")

    async def principal(self, request):
        if not self.enabled:
            raise LoginRequired
        identifier = request.cookies.get(self.cookie)
        if not identifier or len(identifier) > 128:
            raise LoginRequired
        # Serialize refresh-token rotation for one session without holding every
        # other signed-in browser behind a slow identity-provider request.
        lock = self.refresh_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            session = self.store.session(identifier, self.settings.oidc_issuer)
            if not session:
                raise LoginRequired
            self.service.require_active(session["subject"])
            try:
                expiry = session["token"].get("expires_at", 0)
                if expiry < time.time() + 30:
                    session["token"] = await self.identity.refresh(session["token"], session["subject"])
                    self.store.update_session(identifier, session["token"], session["csrf"])
                await self.identity.verify_access(session["token"], session["subject"])
            except (ValueError, KeyError, OAuthError) as exc:
                if isinstance(exc, OAuthError) and exc.error in ("server_error", "temporarily_unavailable"):
                    raise IdentityUnavailable("The identity provider is temporarily unavailable.") from None
                self.store.delete_session(identifier)
                raise LoginRequired from None
        if not self.store.session(identifier, self.settings.oidc_issuer):
            raise LoginRequired  # A provider notification may have arrived during refresh.
        self.service.require_active(session["subject"])
        self.service.state.observe_actor(session["subject"], self.settings.web_client_id)
        return session

    def check_csrf(self, request, session, token=None):
        if request.headers.get("origin") != self.settings.public_url:
            raise Denied("This request did not come from Wiki Hub. Reload the page and try again.")
        supplied = token or request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(supplied, session["csrf"]):
            raise Denied("This page has expired. Reload it before making changes.")

    async def body(self, request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8192:
                raise ContentError("This request is too large.")
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise ContentError("Provide a JSON object.") from None

    def summarize(self, subject, slug):
        wiki = self.service.allowed(subject, slug)
        role = "admin" if wiki["owner"] == subject else self.service.state.role(subject, slug)
        return {"slug": slug, "title": wiki["title"], "status": wiki["status"], "role": role,
                "owner": wiki["owner"] == subject, "manage": role == "admin",
                "read_only": wiki["status"] in ARCHIVE_STATES,
                "local_copies": self.service.state.copies_allowed(subject, slug)}

    def snapshot(self, subject, slug):
        wiki = self.service.allowed(subject, slug, "admin")
        result = self.service._access_result(wiki)
        for grant in result["grants"]:
            grant["effective_role"] = None
            for candidate in reversed(ROLES):
                try:
                    self.service.allowed(grant["subject"], slug, candidate)
                    grant["effective_role"] = candidate
                    break
                except Denied:
                    continue
            if grant["subject"].startswith("agent:"):
                agent = self.service.state.agent(grant["subject"])
                grant["profile"] = {"subject": grant["subject"], "name": agent["name"] if agent else "Unknown agent",
                                    "email": "", "username": "", "agent": True,
                                    "owner": agent["owner"] if agent else ""}
            else:
                grant["profile"] = self.store.profile(self.settings.oidc_issuer, grant["subject"])
        result["owner_profile"] = self.store.profile(self.settings.oidc_issuer, wiki["owner"])
        result["activity"] = self.service.state.access_activity(slug)
        # A pending removal must remain discoverable even after fifty later changes.
        with self.service.state.connect() as db:
            result["pending_operations"] = [dict(row) for row in db.execute(
                "SELECT * FROM access_operations WHERE wiki=? AND status='pending' ORDER BY created", (slug,))]
        for event in result["activity"]:
            event["actor_profile"] = self.store.profile(self.settings.oidc_issuer, event["actor"])
            event["target_profile"] = self.store.profile(self.settings.oidc_issuer, event["subject"])
        return result

    async def page(self, request):
        if not self.enabled:
            return self.render("signin", enabled=False, return_to="/")
        try:
            session = await self.principal(request)
            subject = session["subject"]
            common = {"person": self.store.profile(self.settings.oidc_issuer, subject), "csrf": session["csrf"],
                      "instance_owner": subject == self.settings.owner_subject, "current": "wikis",
                      "drive_enabled": self.service.drive.enabled}
            slug = request.path_params.get("slug")
            if request.url.path == "/agents":
                return self.render("agents", agents=await self.service.agents(subject),
                                   apps=await self.service.apps(subject),
                                   authorized_apps_url=self.settings.authorized_apps_url or self.settings.oidc_issuer,
                                   **common)
            if request.url.path == "/connections":
                recipient = request.query_params.get("subject", subject)
                if recipient != subject and not common["instance_owner"]:
                    raise Denied("Only the owner manages another person's devices.")
                devices = (await self.service.drive_devices(subject, recipient)
                           if self.service.drive.enabled else [])
                return self.render("connections", enabled=self.service.drive.enabled, devices=devices,
                                   recipient=recipient, drive=self.service.settings,
                                   recipient_profile=self.store.profile(self.settings.oidc_issuer, recipient), **common)
            if request.url.path == "/administration":
                self.require_owner(subject)
                people = self.store.people(self.settings.oidc_issuer)
                for person in people:
                    person.update(status=self.service.state.user_status(person["subject"]),
                                  copies=self.service.state.copy_setting("user", person["subject"]))
                return self.render("administration", people=people, directory=self.directory.enabled, **common)
            if not slug:
                wikis = [self.summarize(subject, row["slug"]) for row in self.service.state.list_wikis(subject)]
                return self.render("home", wikis=wikis, **common)
            wiki = self.summarize(subject, slug)
            if request.url.path.endswith("/access"):
                return self.render("access", wiki=wiki, access=self.snapshot(subject, slug),
                                   available_agents=[a for a in await self.service.agents(subject) if not a["revoked"]],
                                   directory=self.directory.enabled, **common)
            if request.url.path.endswith("/settings"):
                return self.render("settings", wiki=wiki,
                                   copies=await self.service.wiki_settings(subject, slug),
                                   archive=await self.service.archive(subject, slug), **common)
            return self.render("wiki", wiki=wiki, **common)
        except LoginRequired:
            return self.render("signin", enabled=True, return_to=quote(local_return(request.url.path), safe=""))
        except (Denied, ContentError):
            return self.render("error", message="You do not have access to this page.", status=403)
        except Exception:
            log.warning("Browser page sign-in or loading unavailable")
            return self.render("error", message="Wiki Hub could not verify your sign-in. Try again shortly.",
                               status=503)

    async def login(self, request):
        if not self.enabled:
            return self.render("signin", enabled=False, return_to="/")
        try:
            transaction, binding = self.store.login(local_return(request.query_params.get("return_to", "/")))
            response = RedirectResponse(await self.identity.authorize(transaction), status_code=303, headers=HEADERS)
        except Exception:
            return self.render("error", message="Sign-in is temporarily unavailable. Try again shortly.", status=503)
        self.set_cookie(response, self.login_cookie, binding, 600)
        return response

    async def callback(self, request):
        if not self.enabled:
            return self.render("error", message="Browser sign-in is not configured.", status=503)
        transaction = self.store.consume_login(request.query_params.get("state"),
                                               request.cookies.get(self.login_cookie))
        try:
            if not transaction or len(request.query_params.getlist("state")) != 1:
                raise ValueError
            if request.query_params.get("iss", self.settings.oidc_issuer) != self.settings.oidc_issuer:
                raise ValueError
            if request.query_params.get("error") or len(request.query_params.getlist("code")) != 1:
                raise ValueError
            token, person = await self.identity.exchange(request.query_params["code"], transaction)
            self.service.require_active(person["subject"])
            identifier, _ = self.store.create_session(self.settings.oidc_issuer, person["subject"], token,
                                                      request.cookies.get(self.cookie))
            self.store.put_profile(self.settings.oidc_issuer, person)
            response = RedirectResponse(local_return(transaction["return_to"]), status_code=303, headers=HEADERS)
            self.set_cookie(response, self.cookie, identifier, 43200)
        except Exception:
            response = self.render("error", message="Sign-in could not be completed. Start sign-in again.", status=400)
        self.clear_cookie(response, self.login_cookie)
        return response

    async def provider_logout(self, request):
        if not self.enabled:
            return Response(status_code=503, headers=HEADERS)
        try:
            if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
                raise ValueError
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 8192:
                    raise ValueError
            fields = parse_qs(raw.decode("ascii"), strict_parsing=True, max_num_fields=4)
            if len(fields.get("logout_token", [])) != 1:
                raise ValueError
            claims = await self.identity.logout_claims(fields["logout_token"][0])
            self.store.provider_logout(self.settings.oidc_issuer, claims)
            return Response(status_code=200, headers=HEADERS)
        except (ValueError, UnicodeError, JoseError):
            return JSONResponse({"error": "Logout notification could not be verified."}, status_code=400,
                                headers=HEADERS)
        except Exception:
            return JSONResponse({"error": "Logout notification is temporarily unavailable."}, status_code=503,
                                headers=HEADERS)

    async def logout(self, request):
        try:
            # A locally suspended or expired identity can still discard its browser
            # session. Logout neither refreshes tokens nor authorizes wiki access.
            session = (self.store.session(request.cookies.get(self.cookie), self.settings.oidc_issuer)
                       if self.store else None)
            if session:
                self.check_csrf(request, session)
        except Denied:
            return JSONResponse({"error": "Reload the page before signing out."}, status_code=403, headers=HEADERS)
        if self.store:
            self.store.delete_session(request.cookies.get(self.cookie))
        response = JSONResponse({"signed_out": True}, headers=HEADERS)
        self.clear_cookie(response, self.cookie)
        return response

    async def asset(self, request):
        name = request.path_params["name"]
        if name not in ("app.css", "app.js", "logo.svg"):
            return Response(status_code=404)
        media = {"app.css": "text/css", "app.js": "text/javascript", "logo.svg": "image/svg+xml"}[name]
        return Response(files("wiki_hub").joinpath("web/static", name).read_bytes(), media_type=media,
                        headers={**HEADERS, "Cache-Control": "public, max-age=3600"})

    def rate_search(self, subject):
        current = time.monotonic()
        # Bound memory as well as provider traffic; all callers are authenticated.
        for key in list(self.searches):
            if not self.searches[key] or self.searches[key][-1] < current - 60:
                del self.searches[key]
        queue = self.searches[subject]
        while queue and queue[0] < current - 60:
            queue.popleft()
        if len(queue) >= 30:
            raise Busy("Too many people searches. Wait a minute before searching again.")
        queue.append(current)

    async def resolve_person(self, subject):
        if (not isinstance(subject, str) or not 1 <= len(subject) <= 200 or subject.startswith(("agent:", "client-"))
                or any(c.isspace() or ord(c) < 32 for c in subject)):
            raise ContentError("Select a person from the results.")
        try:
            self.service.require_active(subject)
        except Denied:
            raise ContentError("This account is suspended or its access update is pending.") from None
        if self.directory.enabled:
            try:
                value = await self.directory.resolve(subject)
            except ValueError:
                raise ContentError("Select an active person who is admitted to Wiki Hub.") from None
            self.store.put_profile(self.settings.oidc_issuer, value)
        # Without a directory, existing verified profiles and the explicitly labelled
        # advanced subject entry preserve compatibility with other issuers.

    def require_owner(self, subject):
        self.service.require_active(subject)
        if subject != self.settings.owner_subject:
            raise Denied("Only the configured owner manages instance accounts.")

    async def refresh_profiles(self, recipients, authorize):
        """Resolve known labels in small batches; lookup never changes permissions."""
        failed = 0

        async def refresh(recipient):
            nonlocal failed
            if recipient.startswith(("agent:", "client-")):
                return
            try:
                value = await self.directory.display(recipient)
                authorize()
                self.store.put_profile(self.settings.oidc_issuer, value)
            except Denied:
                raise
            except Exception:
                failed += 1

        recipients = list(recipients)
        for start in range(0, len(recipients), 4):
            await asyncio.gather(*(refresh(recipient) for recipient in recipients[start:start + 4]))
        authorize()
        return failed

    async def controls(self, request, subject, path):
        """Browser controls reuse service authorization and projection contracts."""
        data = await self.body(request) if request.method == "POST" else {}
        post = request.method == "POST"
        if path == ["agents"]:
            if not post:
                return await self.service.agents(subject)
            if data.get("client_id"):
                return await self.service.add_workload_agent(subject, data["name"], data["client_id"],
                                                             data.get("owner") or None)
            return await self.service.add_agent(subject, data["name"])
        if len(path) == 3 and path[0] == "agents" and path[2] == "revoke" and post:
            return await self.service.revoke_agent(subject, path[1])
        if len(path) == 2 and path[0] == "agents" and post:
            return await self.service.update_agent(subject, path[1], data["name"], data.get("purpose", ""))
        if path == ["devices"]:
            if post:
                return await self.service.add_drive_device(subject, data["name"])
            return await self.service.drive_devices(subject, request.query_params.get("subject"))
        if len(path) == 3 and path[0] == "devices" and path[2] == "revoke" and post:
            device = self.service.state.drive_device(path[1])
            if device and (device["subject"] == subject or subject == self.settings.owner_subject):
                request.state.pending_check = lambda: bool(self.service.state.drive_device(path[1])["revoked"])
            return await self.service.revoke_drive_device(subject, path[1])
        if len(path) == 2 and path[0] == "accounts" and post:
            self.require_owner(subject)
            if path[1] == "profiles":
                if not self.directory.enabled:
                    raise ContentError("The identity directory is not available for refreshing names.")
                self.rate_search(subject)
                failed = await self.refresh_profiles(
                    (person["subject"] for person in self.store.people(self.settings.oidc_issuer)),
                    lambda: self.require_owner(subject))
                return {"people": self.store.people(self.settings.oidc_issuer), "unavailable": failed}
            recipient = data["subject"]
            if path[1] == "status":
                if type(data.get("suspended")) is not bool:
                    raise ContentError("Choose whether the account is suspended.")
                request.state.pending_check = lambda: self.service.state.user_status(recipient)["pending"]
                return await self.service.user_status(subject, recipient, data["suspended"])
            if path[1] == "copies":
                if type(data.get("local_copies")) is not bool:
                    raise ContentError("Choose whether managed copies are allowed.")
                request.state.pending_check = lambda: self.service.state.copy_setting("user", recipient)["pending"]
                return await self.service.user_settings(subject, recipient, data["local_copies"])
        if len(path) == 3 and path[0] == "wikis":
            slug, operation = path[1:]
            self.service.allowed(subject, slug, "admin")
            if operation == "profiles" and post:
                if not self.directory.enabled:
                    raise ContentError("Configure the read-only identity directory to refresh names.")
                self.rate_search(subject)
                wiki = self.service.state.wiki(slug)
                recipients = {wiki["owner"], *(g["subject"] for g in self.service.state.grants(slug))}
                failed = await self.refresh_profiles(recipients, lambda: self.service.allowed(subject, slug, "admin"))
                return {"refreshed": True, "unavailable": failed, "pending": bool(failed)}
            if operation == "settings":
                if post and type(data.get("local_copies")) is not bool:
                    raise ContentError("Choose whether managed copies are allowed.")
                request.state.pending_check = lambda: self.service.state.copy_setting("wiki", slug)["pending"]
                return await self.service.wiki_settings(subject, slug, data["local_copies"] if post else None)
            if operation == "archive":
                if post and type(data.get("archived")) is not bool:
                    raise ContentError("Choose whether the wiki is archived.")
                request.state.pending_check = lambda: self.service.state.wiki(slug)["status"] in {
                    "archiving", "reopening"}
                return await self.service.archive(subject, slug, data["archived"] if post else None)
        return None

    async def api(self, request):
        operation_id = None
        subject = slug = None
        try:
            session = await self.principal(request)
            subject = session["subject"]
            if request.method == "POST":
                self.check_csrf(request, session)
            path = request.path_params["path"].strip("/").split("/")
            if len(path) == 3 and path[0] == "wikis":
                slug = path[1]
            if path == ["session"] and request.method == "GET":
                target = local_return(request.query_params.get("page", "/"))
                if target == "/administration":
                    self.require_owner(subject)
                parts = target.strip("/").split("/")
                if parts[0] == "wikis" and len(parts) >= 2:
                    slug = parts[1]
                    self.service.allowed(subject, slug, "admin" if len(parts) == 3 else "read")
                return JSONResponse({"active": True}, headers=HEADERS)
            if path == ["wikis"] and request.method == "POST":
                data = await self.body(request)
                result = await self.service.create(subject, data["slug"], data["title"])
                return JSONResponse(result, status_code=201, headers=HEADERS)
            if path and (path[0] in ("agents", "devices", "accounts") or
                         (len(path) == 3 and path[0] == "wikis" and path[2] in ("settings", "archive", "profiles"))):
                result = await self.controls(request, subject, path)
                if result is not None:
                    return JSONResponse(result, status_code=202 if isinstance(result, dict) and result.get("pending")
                                        else 200, headers=HEADERS)
            if len(path) == 3 and path[0] == "wikis":
                slug, operation = path[1:]
                self.service.allowed(subject, slug, "admin")
                if operation == "access":
                    if request.method == "GET":
                        return JSONResponse(self.snapshot(subject, slug), headers=HEADERS)
                    data = await self.body(request)
                    if type(data.get("version")) is not int or not isinstance(data.get("operation_id"), str):
                        raise ContentError("Reload access before making this change.")
                    role, recipient = data["role"], data["subject"]
                    if not isinstance(recipient, str) or role not in (*ROLES, "none"):
                        raise ContentError("Choose a person and a valid role.")
                    if role != "none" and not recipient.startswith("agent:"):
                        await self.resolve_person(recipient)
                    operation_id = data["operation_id"]
                    result = await self.service.share(subject, slug, recipient, role, operation_id=operation_id,
                                                      expected_version=data["version"], retry=data.get("retry", False))
                    # Self-demotion must not return the newly protected member list.
                    return JSONResponse({"operation": result["operation"]}, headers=HEADERS)
                if operation == "people" and request.method == "GET":
                    self.rate_search(subject)
                    query = request.query_params.get("q", "").strip()
                    if not 2 <= len(query) <= 100:
                        return JSONResponse({"people": [], "directory": self.directory.enabled}, headers=HEADERS)
                    rows = (await self.directory.search(query) if self.directory.enabled else
                            self.store.search_profiles(self.settings.oidc_issuer, query))
                    rows = [row for row in rows if not any(self.service.state.user_status(row["subject"]).values())
                            and not row["subject"].startswith(("agent:", "client-"))]
                    self.service.allowed(subject, slug, "admin")  # directory work may have waited
                    return JSONResponse({"people": rows, "directory": self.directory.enabled}, headers=HEADERS)
                if operation == "operation" and request.method == "GET":
                    event = self.service.state.access_operation(request.query_params.get("id", ""))
                    if not event or event["wiki"] != slug:
                        return JSONResponse({"error": "No such access change."}, status_code=404, headers=HEADERS)
                    return JSONResponse({"operation": event}, headers=HEADERS)
            return JSONResponse({"error": "No such operation."}, status_code=404, headers=HEADERS)
        except LoginRequired:
            return JSONResponse({"error": "Sign in again to continue."}, status_code=401, headers=HEADERS)
        except Denied as exc:
            access_lost = True
            if subject:
                try:
                    self.service.require_active(subject)
                    if slug:
                        self.service.allowed(subject, slug, "admin")
                    access_lost = False
                except Denied:
                    pass
            return JSONResponse({"error": str(exc), "access_lost": access_lost}, status_code=403, headers=HEADERS)
        except AccessConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409, headers=HEADERS)
        except Busy as exc:
            return JSONResponse({"error": str(exc)}, status_code=429, headers={**HEADERS, "Retry-After": "60"})
        except ContentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=HEADERS)
        except (GitError, ForgejoError, DriveError):
            check = getattr(request.state, "pending_check", None)
            if check and check():
                return JSONResponse({"pending": True}, status_code=202, headers=HEADERS)
            event = self.service.state.access_operation(operation_id) if operation_id else None
            if event and event["status"] == "pending":
                return JSONResponse({"operation": event, "pending": True}, status_code=202, headers=HEADERS)
            return JSONResponse({"error": "The backend update did not complete. Reload to check its status."},
                                status_code=503, headers=HEADERS)
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "Invalid request. Reload the page and review the change."},
                                status_code=400, headers=HEADERS)
        except Exception:
            log.warning("Browser identity or directory operation unavailable")
            return JSONResponse({"error": "Sign-in or people lookup is temporarily unavailable. Try again shortly."},
                                status_code=503, headers=HEADERS)
