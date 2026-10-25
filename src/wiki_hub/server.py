"""HTTP API and OAuth-protected MCP endpoint in one application."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

import uvicorn
from fastmcp.server.auth import RemoteAuthProvider
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

from . import __version__
from .auth import WIKI_SCOPE, PrincipalVerifier, create_verifier, principal_subject
from .config import Settings
from .forgejo import ForgejoError
from .gitstore import ContentError, GitError
from .mcp import create_mcp
from .service import Denied, Service

log = logging.getLogger(__name__)


def create_app(settings=None, service=None, verifier=None):
    settings = settings or Settings.from_env()
    service = service or Service(settings)
    verifier = PrincipalVerifier(verifier or create_verifier(settings), service.state)
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

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.lifespan(app):
            yield
        await service.shutdown()

    async def health(_request):
        return JSONResponse({"status": "healthy", "version": __version__})

    async def config(_request):
        return JSONResponse({"issuer": settings.oidc_issuer, "client_id": settings.oidc_client_id,
                             "resource": settings.oidc_audience,
                             "scope": "openid offline_access " + WIKI_SCOPE})

    async def subject(request):
        raw = request.headers.get("authorization", "")
        kind, _, bearer = raw.partition(" ")
        if kind.lower() != "bearer" or not bearer:
            return None
        token = await verifier.verify_token(bearer)
        return principal_subject(token) if token else None

    async def body(request):
        maximum = (settings.upload_limit + 2) // 3 * 4 + 8192
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
            if path == ["me"] and request.method == "GET":
                return JSONResponse({"subject": caller, "owner": caller == settings.owner_subject})
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
            if len(path) == 3 and path[0] == "agents" and path[2] == "revoke" and request.method == "POST":
                return JSONResponse(await service.revoke_agent(caller, path[1]))
            if path == ["wikis"]:
                if request.method == "GET":
                    return JSONResponse(await service.list_wikis(caller))
                if request.method == "POST":
                    data = await body(request)
                    return JSONResponse(await service.create(caller, data["slug"], data["title"]), status_code=201)
            if len(path) == 2 and path[0] == "jobs" and request.method == "GET":
                return JSONResponse(await service.job(caller, path[1]))
            if len(path) == 3 and path[0] == "wikis":
                wiki, operation = path[1:]
                if request.method == "GET":
                    if operation == "pages":
                        return JSONResponse(await service.pages(caller, wiki))
                    if operation == "page":
                        return JSONResponse(await service.read_page(caller, wiki, request.query_params["path"]))
                    if operation == "search":
                        return JSONResponse(await service.search(
                            caller, wiki, request.query_params["q"], int(request.query_params.get("limit", "10")),
                        ))
                    if operation == "history":
                        return JSONResponse(await service.history(caller, wiki))
                    if operation == "access":
                        return JSONResponse(await service.access(caller, wiki))
                    if operation == "copy-access":
                        return JSONResponse(await service.copy_access(caller, wiki),
                                            headers={"Cache-Control": "no-store"})
                    if operation == "settings":
                        return JSONResponse(await service.wiki_settings(caller, wiki))
                    if operation == "document":
                        row = service.allowed(caller, wiki)
                        async with service.lock(wiki):
                            file = await asyncio.to_thread(service.git.original, row, request.query_params["path"])
                        return FileResponse(file, filename=PurePosixPath(request.query_params["path"]).name,
                                            headers={"Cache-Control": "no-store"})
                if request.method == "POST":
                    data = await body(request)
                    if operation == "ingest":
                        return JSONResponse(await service.ingest(
                            caller, wiki, data["filename"], data["content_base64"], data.get("instructions", ""),
                        ), status_code=202)
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
        except ContentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except (ValueError, KeyError):
            return JSONResponse({"error": "Invalid request or content path."}, status_code=400)
        except (GitError, ForgejoError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    app = Starlette(
        routes=[Route("/health", health), Route("/api/config", config),
                Route("/api/{path:path}", api, methods=["GET", "POST"]), Mount("/", app=mcp_app)],
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.mcp = mcp
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host=os.environ.get("WIKI_HOST", "0.0.0.0"),
                port=int(os.environ.get("WIKI_PORT", "8000")), access_log=False)


if __name__ == "__main__":
    main()
