"""HTTP API and OAuth-protected MCP endpoint in one application."""

import json
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.resources import files
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import uvicorn
from fastmcp.server.auth import RemoteAuthProvider
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from . import __version__
from .auth import WIKI_SCOPE, GatewayVerifier, PrincipalVerifier, create_verifier, principal_subject
from .config import Settings
from .drive import DriveError, DriveUnavailable
from .forgejo import ForgejoError
from .gitstore import ContentError, GitError
from .mcp import create_mcp
from .service import Busy, Denied, Service
from .state import AccessConflict
from .web import BrowserWeb

log = logging.getLogger(__name__)


def line_number(request, name):
    value = request.query_params.get(name)
    if value is None:
        return None
    if not value.isdigit():
        raise ContentError(f"{name} must be a whole line number.")
    return int(value)


def create_app(settings=None, service=None, verifier=None, *, web_transport=None):
    settings = settings or Settings.from_env()
    service = service or Service(settings)
    verifier = PrincipalVerifier(verifier or create_verifier(settings), service.state)
    web = BrowserWeb(settings, service, verifier, web_transport)
    metadata_url = settings.public_url + "/.well-known/oauth-protected-resource/mcp"
    challenge = {"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}", scope="{WIKI_SCOPE}"'}
    # OAuth issuer identifiers are exact strings. Preserve a root issuer's
    # empty path instead of adding a slash that its discovery document lacks.
    issuer = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True)).validate_python(
        settings.oidc_issuer
    )
    auth = RemoteAuthProvider(
        token_verifier=verifier, authorization_servers=[issuer],
        base_url=settings.public_url, scopes_supported=[WIKI_SCOPE],
    )
    mcp = create_mcp(service, auth=auth)
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True, host_origin_protection=False)
    gateway_app = None
    if settings.gateway_bearer_tokens:
        gateway = create_mcp(service, auth=GatewayVerifier(settings.gateway_bearer_tokens), tool_prefix="")
        gateway_app = gateway.http_app(path="/mcp", stateless_http=True, json_response=True,
                                       host_origin_protection=True,
                                       allowed_hosts=[urlsplit(settings.public_url).hostname,
                                                      *settings.gateway_allowed_hosts],
                                       allowed_origins=[settings.public_url])

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_app.lifespan(app))
            if gateway_app is not None:
                await stack.enter_async_context(gateway_app.lifespan(app))
            service.start()
            yield
        await service.shutdown()

    async def health(_request):
        return JSONResponse({"status": "healthy", "version": __version__})

    async def config(_request):
        return JSONResponse({"issuer": settings.oidc_issuer, "client_id": settings.oidc_client_id,
                             "resource": settings.oidc_audience,
                             "scope": "openid offline_access " + WIKI_SCOPE})

    async def drive_windows(request):
        # A public, secret-free script: `irm <server>/drive/windows | iex` connects a Windows computer.
        if not settings.drive_enabled:
            return PlainTextResponse("This server does not offer a network drive.", status_code=404)
        action = request.query_params.get("action", "connect")
        if action not in ("connect", "disconnect"):
            return PlainTextResponse("action must be connect or disconnect.", status_code=400)
        script = files("wiki_hub").joinpath("drive_windows.ps1").read_text()
        script = script.replace("__WIKI_SERVER__", settings.public_url.replace("'", "''"))
        return PlainTextResponse(script.replace("__WIKI_ACTION__", action), headers={"Cache-Control": "no-cache"})

    async def subject(request):
        raw = request.headers.get("authorization", "")
        kind, _, bearer = raw.partition(" ")
        if kind.lower() != "bearer" or not bearer:
            return None
        token = await verifier.verify_token(bearer)
        request.state.access = token
        request.state.client_id = token.client_id if token else None
        return principal_subject(token) if token else None

    async def body(request):
        # Source bytes share one limit. Leave room for 20 filenames, a full
        # 4,000-character instruction and JSON Unicode escapes/base64 padding.
        maximum = (settings.upload_limit + 2) // 3 * 4 + 128 * 1024
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > maximum:
                raise ContentError("Request exceeds the configured upload limit.")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise ContentError("Provide a JSON object.") from None
        if not isinstance(result, dict):
            raise ContentError("Provide a JSON object.")
        return result

    async def api(request: Request):
        try:
            caller = await subject(request)
        except Exception:
            return JSONResponse({"error": "Sign-in verification is temporarily unavailable."}, status_code=503)
        if caller is None:
            return JSONResponse({"error": "Sign in to use wiki-hub."}, status_code=401, headers=challenge)
        path = request.path_params["path"].strip("/").split("/")
        try:
            service.require_active(caller)
            service.observe_access(request.state.access)
            if path == ["me"] and request.method == "GET":
                return JSONResponse({"subject": caller, "owner": caller == settings.owner_subject})
            if path == ["me", "inbox"] and request.method == "GET":
                return JSONResponse(await service.inbox(caller), headers={"Cache-Control": "no-store"})
            if path == ["me", "access"] and request.method == "GET":
                return JSONResponse(await service.effective_access(caller), headers={"Cache-Control": "no-store"})
            if path == ["users", "status"]:
                if request.method == "GET":
                    return JSONResponse(await service.user_status(caller, request.query_params["subject"]),
                                        headers={"Cache-Control": "no-store"})
                data = await body(request)
                if type(data.get("suspended")) is not bool:
                    raise ContentError("suspended must be true or false.")
                return JSONResponse(await service.user_status(caller, data["subject"], data["suspended"]),
                                    headers={"Cache-Control": "no-store"})
            if path == ["users", "settings"]:
                if request.method == "GET":
                    return JSONResponse(await service.user_settings(caller, request.query_params["subject"]))
                data = await body(request)
                if type(data.get("local_copies")) is not bool:
                    raise ContentError("local_copies must be true or false.")
                return JSONResponse(await service.user_settings(caller, data["subject"], data["local_copies"]))
            if path == ["agents"]:
                if request.method == "GET":
                    return JSONResponse(await service.agents(caller))
                if request.method == "POST":
                    data = await body(request)
                    return JSONResponse(await service.add_agent(caller, data["name"]), status_code=201,
                                        headers={"Cache-Control": "no-store"})
            if path == ["agents", "workloads"] and request.method == "POST":
                data = await body(request)
                return JSONResponse(await service.add_workload_agent(
                    caller, data["name"], data["client_id"], data.get("owner"),
                ), status_code=201, headers={"Cache-Control": "no-store"})
            if path == ["me", "apps"] and request.method == "GET":
                return JSONResponse(await service.apps(caller), headers={"Cache-Control": "no-store"})
            if len(path) == 2 and path[0] == "agents" and request.method == "POST":
                data = await body(request)
                return JSONResponse(await service.update_agent(caller, path[1], data["name"], data.get("purpose", "")),
                                    headers={"Cache-Control": "no-store"})
            if path == ["drive"] and request.method == "GET":
                return JSONResponse(await service.drive_status(caller), headers={"Cache-Control": "no-store"})
            if path == ["drive", "devices"]:
                if request.method == "GET":
                    return JSONResponse(await service.drive_devices(caller, request.query_params.get("subject")),
                                        headers={"Cache-Control": "no-store"})
                data = await body(request)
                return JSONResponse(await service.add_drive_device(caller, data["name"]), status_code=201,
                                    headers={"Cache-Control": "no-store"})
            if len(path) == 4 and path[:2] == ["drive", "devices"] and path[3] == "revoke" and request.method == "POST":
                return JSONResponse(await service.revoke_drive_device(caller, path[2]),
                                    headers={"Cache-Control": "no-store"})
            if len(path) == 3 and path[0] == "agents" and path[2] == "revoke" and request.method == "POST":
                return JSONResponse(await service.revoke_agent(caller, path[1]))
            if path == ["wikis"]:
                if request.method == "GET":
                    include = request.query_params.get("include_archived", "false")
                    if include not in {"true", "false"}:
                        raise ContentError("include_archived must be true or false.")
                    return JSONResponse(await service.list_wikis(caller, include_archived=include == "true"))
                if request.method == "POST":
                    data = await body(request)
                    return JSONResponse(await service.create(caller, data["slug"], data["title"]), status_code=201)
            if path == ["search"] and request.method == "GET":
                return JSONResponse(await service.search(
                    caller, None, request.query_params["q"], int(request.query_params.get("limit", "10")),
                ))
            if len(path) == 3 and path[0] == "jobs" and path[2] == "undo" and request.method == "POST":
                return JSONResponse(await service.undo_ingest(caller, path[1]),
                                    headers={"Cache-Control": "no-store"})
            if len(path) == 2 and path[0] == "jobs" and request.method == "GET":
                return JSONResponse(await service.job(caller, path[1]))
            if len(path) == 3 and path[0] == "wikis":
                wiki, operation = path[1:]
                if request.method == "GET":
                    if operation == "pages":
                        return JSONResponse(await service.pages(caller, wiki))
                    if operation == "page":
                        return JSONResponse(await service.read_page(
                            caller, wiki, request.query_params["path"], request.query_params.get("revision"),
                            line_number(request, "start_line"), line_number(request, "end_line"),
                        ), headers={"Cache-Control": "no-store"})
                    if operation == "jobs":
                        return JSONResponse(await service.jobs(
                            caller, wiki, request.query_params.get("status"),
                            int(request.query_params.get("limit", "50")), request.query_params.get("before"),
                        ), headers={"Cache-Control": "no-store"})
                    if operation == "search":
                        return JSONResponse(await service.search(
                            caller, wiki, request.query_params["q"], int(request.query_params.get("limit", "10")),
                        ))
                    if operation == "history":
                        return JSONResponse(await service.history(caller, wiki))
                    if operation == "access":
                        return JSONResponse(await service.access(caller, wiki))
                    if operation == "archive":
                        return JSONResponse(await service.archive(caller, wiki), headers={"Cache-Control": "no-store"})
                    if operation == "copy-access":
                        return JSONResponse(await service.copy_access(caller, wiki),
                                            headers={"Cache-Control": "no-store"})
                    if operation == "settings":
                        return JSONResponse(await service.wiki_settings(caller, wiki))
                    if operation == "document":
                        service.allowed(caller, wiki)
                        async with service.lock(wiki):
                            row = service.allowed(caller, wiki)
                            file = await service._thread(service.git.original, row, request.query_params["path"],
                                                           request.query_params.get("revision"))
                            service.allowed(caller, wiki)
                        headers = {"Cache-Control": "no-store"}
                        if request.query_params.get("revision") is not None:
                            # original() has validated this exact accepted commit.
                            headers["X-Wiki-Revision"] = request.query_params["revision"]
                        return FileResponse(file, filename=PurePosixPath(request.query_params["path"]).name,
                                            headers=headers)
                if request.method == "POST":
                    data = await body(request)
                    if operation == "extract":
                        return JSONResponse(await service.extract_preview(
                            caller, wiki, data["filename"], data["content_base64"],
                        ), headers={"Cache-Control": "no-store"})
                    if operation in ("ingest", "submissions"):
                        if operation == "submissions" and not isinstance(data.get("submission_id"), str):
                            raise ContentError("A retained submission_id is required for this endpoint.")
                        return JSONResponse(await service.ingest(
                            caller, wiki, data["filename"], data["content_base64"], data.get("instructions", ""),
                            data.get("submission_id"), data.get("attachments"),
                            client_id=request.state.client_id,
                        ), status_code=202)
                    if operation == "archive":
                        if type(data.get("archived")) is not bool:
                            raise ContentError("archived must be true or false.")
                        return JSONResponse(await service.archive(caller, wiki, data["archived"]),
                                            headers={"Cache-Control": "no-store"})
                    if operation == "settings":
                        if type(data.get("local_copies")) is not bool:
                            raise ContentError("local_copies must be true or false.")
                        return JSONResponse(await service.wiki_settings(caller, wiki, data["local_copies"]))
                    if operation == "rollback":
                        return JSONResponse(await service.rollback(caller, wiki, data["revision"]))
                    if operation == "share":
                        return JSONResponse(await service.share(caller, wiki, data["subject"], data["role"]))
                    if operation == "unshare":
                        return JSONResponse(await service.share(caller, wiki, data["subject"], "none"))
                    if operation == "clone":
                        if not isinstance(data.get("write", True), bool):
                            raise ContentError("write must be true or false.")
                        return JSONResponse(await service.clone(caller, wiki, data.get("write", True)),
                                            headers={"Cache-Control": "no-store"})
            return JSONResponse({"error": "No such operation."}, status_code=404)
        except Denied as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except Busy as exc:
            return JSONResponse({"error": str(exc)}, status_code=429, headers={"Retry-After": "30"})
        except ContentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except AccessConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, KeyError):
            return JSONResponse({"error": "Invalid request or content path."}, status_code=400)
        except DriveUnavailable as exc:
            return JSONResponse({"error": str(exc)}, status_code=503, headers={"Retry-After": "30"})
        except (GitError, ForgejoError, DriveError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    gateway_routes = [Mount("/gateway", app=gateway_app)] if gateway_app is not None else []
    app = Starlette(
        routes=[*web.routes(), Route("/health", health), Route("/api/config", config),
                Route("/drive/windows", drive_windows),
                Route("/api/{path:path}", api, methods=["GET", "POST"]), *gateway_routes, Mount("/", app=mcp_app)],
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.mcp = mcp
    app.state.web = web
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host=os.environ.get("WIKI_HOST", "0.0.0.0"),
                port=int(os.environ.get("WIKI_PORT", "8000")), access_log=False)


if __name__ == "__main__":
    main()
