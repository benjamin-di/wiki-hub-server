"""Small HTTP CLI and native-folder Git copies for wiki-hub."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

WINDOWS = os.name == "nt"


class CLIError(Exception):
    """An error safe to display without credentials or a traceback."""


def config_directory() -> Path:
    if os.environ.get("WIKI_CONFIG_DIR"):
        return Path(os.environ["WIKI_CONFIG_DIR"]).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "wiki-hub"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "wiki-hub"


class CredentialStore:
    """Prefer the OS keyring; headless Unix falls back to an owner-only file."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or config_directory()
        self.path = self.directory / "config.json"
        self.keyring = None
        try:
            import keyring

            if keyring.get_keyring().priority > 0:
                self.keyring = keyring
        except (ImportError, RuntimeError):
            pass

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        if os.name != "nt" and stat.S_IMODE(self.path.stat().st_mode) & 0o077:
            raise CLIError(f"Make {self.path} readable only by its owner before continuing.")
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise CLIError(f"Cannot read settings at {self.path}.") from exc

    def _write(self, data: dict[str, Any]) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="config-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def server(self) -> str | None:
        return self._read().get("server")

    def set_server(self, server: str) -> None:
        data = self._read()
        data["server"] = server
        self._write(data)

    def agent(self) -> str | None:
        return self._read().get("agent")

    def set_agent(self, identifier: str) -> None:
        data = self._read()
        data["agent"] = identifier
        self._write(data)

    @staticmethod
    def _chunk_services(name: str, record: dict[str, Any]) -> list[str]:
        if record.get("_wiki_hub_keyring") != 1:
            return []
        generation, count = record.get("generation"), record.get("count")
        if (
            not isinstance(generation, str) or len(generation) != 32
            or any(c not in "0123456789abcdef" for c in generation)
            or type(count) is not int or not 1 <= count <= 128
        ):
            raise ValueError("Invalid credential index")
        prefix = "wiki-hub/chunks/" + hashlib.sha256(name.encode()).hexdigest() + "/" + generation
        return [f"{prefix}/{index}" for index in range(count)]

    def _delete_chunks(self, services: list[str]) -> None:
        for service in services:
            try:
                self.keyring.delete_password(service, "credential")
            except Exception:
                pass

    def _put_keyring(self, name: str, value: dict[str, Any]) -> None:
        previous = self.keyring.get_password("wiki-hub", name)
        old_chunks = self._chunk_services(name, json.loads(previous)) if previous else []
        payload = json.dumps(value, separators=(",", ":"))
        new_chunks = []
        try:
            # WinVault stores UTF-16 and limits each blob to 2560 bytes. JSON is
            # ASCII escaped, so 1000 characters fit even for Unicode values.
            if WINDOWS and len(payload) > 1000:
                chunks = [payload[i:i + 1000] for i in range(0, len(payload), 1000)]
                record = {"_wiki_hub_keyring": 1, "generation": uuid.uuid4().hex, "count": len(chunks)}
                new_chunks = self._chunk_services(name, record)
                for service, chunk in zip(new_chunks, chunks, strict=True):
                    self.keyring.set_password(service, "credential", chunk)
                payload = json.dumps(record)
            # Publish the new index only after every chunk has been saved.
            self.keyring.set_password("wiki-hub", name, payload)
        except Exception:
            self._delete_chunks(new_chunks)
            raise
        self._delete_chunks(old_chunks)

    def get(self, name: str) -> dict[str, Any] | None:
        if self.keyring:
            try:
                value = self.keyring.get_password("wiki-hub", name)
                if value:
                    record = json.loads(value)
                    services = self._chunk_services(name, record)
                    if services:
                        record = json.loads("".join(self.keyring.get_password(s, "credential") for s in services))
                    return record
            except Exception:
                # A locked desktop keyring should not hide an existing headless record.
                pass
        return self._read().get("credentials", {}).get(name)

    def put(self, name: str, value: dict[str, Any]) -> None:
        failure = " No credential-manager backend is available."
        if self.keyring:
            try:
                self._put_keyring(name, value)
            except Exception as exc:
                # Native exception messages may contain secrets. Report only a
                # numeric Windows error, when present, without guessing its cause.
                code = getattr(exc, "winerror", None)
                failure = f" Windows error {code}." if type(code) is int else ""
            else:
                data = self._read()
                data.get("credentials", {}).pop(name, None)
                self._write(data)
                return
        if WINDOWS:
            raise CLIError("Windows Credential Manager could not save credentials." + failure + " Retry wiki login.")
        data = self._read()
        data.setdefault("credentials", {})[name] = value
        self._write(data)


def oauth_key(server: str) -> str:
    return "oauth:" + server.rstrip("/")


def agent_key(server: str, identifier: str) -> str:
    return "agent:" + server.rstrip("/") + ":" + identifier


def repo_key(directory: Path) -> str:
    normalized = os.path.normcase(str(directory.resolve()))
    return "git:" + hashlib.sha256(normalized.encode()).hexdigest()


class APIClient:
    def __init__(self, server: str, store: CredentialStore, http: httpx.Client | None = None):
        self.server = server.rstrip("/")
        self.store = store
        self.agent_id = store.agent()
        self.http = http or httpx.Client(timeout=60, follow_redirects=False)

    def public_config(self) -> dict[str, Any]:
        response = self.http.get(self.server + "/api/config")
        self._check(response)
        return response.json()

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if not response.is_success:
            raise CLIError(f"Request failed (HTTP {response.status_code}).")

    def _refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        if not token.get("refresh_token"):
            raise CLIError("Your sign-in has expired. Run wiki login again.")
        response = self.http.post(
            token["token_endpoint"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
                "client_id": token["client_id"],
                "resource": token["resource"],
            },
        )
        self._check(response)
        updated = response.json()
        if "access_token" not in updated:
            raise CLIError("Sign-in could not be refreshed. Run wiki login again.")
        token.update(updated)
        token["expires_at"] = time.time() + float(updated.get("expires_in", 300))
        self.store.put(oauth_key(self.server), token)
        return token

    def _response(self, method: str, path: str, *, stream: bool = False, **kwargs: Any) -> httpx.Response:
        key = agent_key(self.server, self.agent_id) if self.agent_id else oauth_key(self.server)
        token = self.store.get(key)
        if not token:
            raise CLIError("Run wiki login first.")
        if not self.agent_id and token.get("expires_at", 0) <= time.time() + 30:
            token = self._refresh(token)
        def send():
            request = self.http.build_request(
                method, self.server + path, headers={"Authorization": "Bearer " + token["access_token"]}, **kwargs
            )
            return self.http.send(request, stream=stream)

        response = send()
        if response.status_code == 401 and token.get("refresh_token"):
            response.close()
            token = self._refresh(token)
            response = send()
        try:
            self._check(response)
        except CLIError:
            response.close()
            raise
        return response

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._response(method, path, **kwargs)
        try:
            return response.json() if response.content else None
        finally:
            response.close()

    def download(self, wiki: str, path: str, destination: Path) -> None:
        """Save an original through the authenticated API without replacing a local file."""
        destination = destination.expanduser().absolute()
        if destination.exists():
            raise CLIError("Choose a new output file; the destination already exists.")
        response = self._response(
            "GET", f"/api/wikis/{wiki}/document", stream=True, params={"path": path}
        )
        created = False
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        print(f"Original document saved: {destination}")

    def login(self, open_browser: bool = True, timeout: int = 600) -> None:
        config = self.public_config()
        discovery = self.http.get(config["issuer"].rstrip("/") + "/.well-known/openid-configuration")
        self._check(discovery)
        endpoints = discovery.json()
        response = self.http.post(
            endpoints["device_authorization_endpoint"],
            data={
                "client_id": config["client_id"],
                "scope": config.get("scope", "openid offline_access"),
                "resource": config["resource"],
            },
        )
        self._check(response)
        device = response.json()
        print("Open " + device["verification_uri"])
        print("Code: " + device["user_code"])
        if open_browser:
            try:
                webbrowser.open(device.get("verification_uri_complete", device["verification_uri"]))
            except webbrowser.Error:
                pass
        deadline = time.monotonic() + min(timeout, float(device.get("expires_in", timeout)))
        interval = max(1, float(device.get("interval", 5)))
        while time.monotonic() < deadline:
            time.sleep(interval)
            response = self.http.post(
                endpoints["token_endpoint"],
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device["device_code"],
                    "client_id": config["client_id"],
                    "resource": config["resource"],
                },
            )
            payload = response.json()
            if response.is_success and payload.get("access_token"):
                payload.update(
                    token_endpoint=endpoints["token_endpoint"],
                    client_id=config["client_id"],
                    resource=config["resource"],
                    expires_at=time.time() + float(payload.get("expires_in", 300)),
                )
                self.store.put(oauth_key(self.server), payload)
                self.store.set_server(self.server)
                print("Signed in.")
                return
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error in {"access_denied", "expired_token"}:
                raise CLIError("Sign-in was declined or expired. Run wiki login to retry.")
            raise CLIError("The identity provider could not complete sign-in.")
        raise CLIError("Sign-in timed out. Run wiki login to retry.")


class HTTPService:
    """The same MCP tools can use the authenticated API from a local CLI."""

    def __init__(self, client: APIClient):
        self.client = client

    async def list_wikis(self, subject: str):
        return self.client.request("GET", "/api/wikis")

    async def pages(self, subject: str, wiki: str):
        return self.client.request("GET", f"/api/wikis/{wiki}/pages")

    async def read_page(self, subject: str, wiki: str, path: str):
        return self.client.request("GET", f"/api/wikis/{wiki}/page", params={"path": path})

    async def search(self, subject: str, wiki: str, query: str, limit: int = 10):
        return self.client.request("GET", f"/api/wikis/{wiki}/search", params={"q": query, "limit": limit})

    async def ingest(self, subject: str, wiki: str, filename: str, content_base64: str, instructions: str = ""):
        return self.client.request(
            "POST",
            f"/api/wikis/{wiki}/ingest",
            json={"filename": filename, "content_base64": content_base64, "instructions": instructions},
        )

    async def job(self, subject: str, jobid: str):
        return self.client.request("GET", f"/api/jobs/{jobid}")

    async def history(self, subject: str, wiki: str):
        return self.client.request("GET", f"/api/wikis/{wiki}/history")


def git_run(
    arguments: list[str], directory: Path, credentials: dict[str, Any] | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Supply a repository token through askpass, never a command argument or remote URL."""
    environment = os.environ.copy()
    environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", LC_ALL="C")
    with tempfile.TemporaryDirectory(prefix="wiki-git-") as temporary:
        if credentials:
            helper = Path(temporary) / "askpass.py"
            helper.write_text(
                "from wiki_hub.git_auth import main\nraise SystemExit(main())\n",
                encoding="utf-8",
            )
            if os.name == "nt":
                launcher = Path(temporary) / "askpass.cmd"
                launcher.write_text(f'@"{sys.executable}" "{helper}" %*\r\n', encoding="utf-8")
            else:
                launcher = Path(temporary) / "askpass"
                launcher.write_text(
                    "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(helper)) + ' "$@"\n',
                    encoding="utf-8",
                )
                launcher.chmod(0o700)
            environment.update(
                GIT_ASKPASS=str(launcher),
                WIKI_GIT_USERNAME=credentials["username"],
                WIKI_GIT_TOKEN=credentials["token"],
                WIKI_GIT_ALLOWED_URLS=json.dumps([credentials["url"]]),
            )
        environment["GIT_LFS_SKIP_SMUDGE"] = "1"
        result = subprocess.run(
            ["git", "-c", "credential.helper=", *arguments],
            cwd=directory,
            env=environment,
            text=True,
            capture_output=True,
        )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        if credentials:
            detail = detail.replace(credentials["token"], "[credential]")
        raise CLIError("Git stopped: " + detail)
    return result


def clone(client: APIClient, wiki: str, directory: Path, *, read_only: bool = False) -> None:
    target = directory.expanduser().resolve()
    credentials = client.request("POST", f"/api/wikis/{wiki}/clone", json={"write": not read_only})
    parsed = urlsplit(credentials["url"])
    if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
        raise CLIError("The server returned an invalid Git URL.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise CLIError("Choose an empty directory for the local copy.")
    target.parent.mkdir(parents=True, exist_ok=True)
    git_run(["clone", credentials["url"], str(target)], target.parent, credentials)
    client.store.put(repo_key(target), credentials)
    git_run(["config", "wikihub.server", client.server], target)
    git_run(["config", "wikihub.wiki", wiki], target)
    print(f"Local copy ready: {target}")


def sync(directory: Path, store: CredentialStore) -> None:
    directory = directory.expanduser().resolve()
    root = Path(git_run(["rev-parse", "--show-toplevel"], directory).stdout.strip())
    credentials = store.get(repo_key(root))
    if not credentials:
        raise CLIError("This copy has no saved wiki credential. Create it with wiki clone.")
    if git_run(["ls-files", "-u"], root).stdout.strip():
        raise CLIError("Resolve the existing merge conflicts before running wiki sync.")
    if git_run(["rev-parse", "--verify", "MERGE_HEAD"], root, check=False).returncode == 0:
        raise CLIError("Finish or abort the existing merge before running wiki sync.")
    if credentials.get("write", True) is False:
        git_run(["fetch", "origin"], root, credentials)
        git_run(["merge", "--ff-only", "@{upstream}"], root)
        print("Read-only local copy updated; local edits remain on this computer.")
        return
    git_run(["add", "-A"], root)
    changes = git_run(["diff", "--cached", "--quiet"], root, check=False)
    if changes.returncode == 1:
        git_run(["commit", "-m", "Update wiki from local copy"], root)
    elif changes.returncode:
        raise CLIError("Git could not inspect the staged changes.")
    git_run(["fetch", "origin"], root, credentials)
    git_run(["merge", "--no-edit", "@{upstream}"], root, credentials)
    git_run(["push"], root, credentials)
    print("Local copy synced.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="wiki", description="Use your wiki-hub wikis.")
    result.add_argument("--server", help="wiki-hub URL (or WIKI_SERVER_URL)")
    sub = result.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="Sign in with your passkey")
    login.add_argument("--no-browser", action="store_true")
    create = sub.add_parser("create")
    create.add_argument("slug")
    create.add_argument("--title")
    sub.add_parser("list")
    sub.add_parser("whoami", help="Show your immutable sign-in subject for a wiki owner to share with")
    wiki_settings = sub.add_parser("settings", help="Inspect or change a wiki's Git-copy policy")
    wiki_settings.add_argument("wiki")
    wiki_settings.add_argument("--local-copies", choices=("allow", "deny"))
    user = sub.add_parser("user", help="Manage per-person settings as the configured owner")
    users = user.add_subparsers(dest="user_command", required=True)
    user_settings = users.add_parser("set")
    user_settings.add_argument("subject")
    user_settings.add_argument("--local-copies", choices=("allow", "deny"))
    agent = sub.add_parser("agent", help="Manage separately scoped agent credentials")
    agents = agent.add_subparsers(dest="agent_command", required=True)
    add_agent = agents.add_parser("add")
    add_agent.add_argument("name")
    add_agent.add_argument("--directory", type=Path, help="New private configuration directory for this agent")
    agents.add_parser("list")
    revoke_agent = agents.add_parser("revoke")
    revoke_agent.add_argument("id")
    for name in ("access", "share", "unshare"):
        command = sub.add_parser(name)
        command.add_argument("wiki")
        if name != "access":
            command.add_argument("subject", help="Exact subject from the recipient's wiki whoami")
        if name == "share":
            command.add_argument("--role", choices=("read", "contribute", "write", "admin"), default="read")
    contribute = sub.add_parser("contribute")
    contribute.add_argument("wiki")
    contribute.add_argument("source", type=Path)
    contribute.add_argument("--instructions", default="")
    status = sub.add_parser("status")
    status.add_argument("job")
    search = sub.add_parser("search")
    search.add_argument("wiki")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    read = sub.add_parser("read")
    read.add_argument("wiki")
    read.add_argument("path")
    docs = sub.add_parser("docs", help="Download preserved original sources")
    documents = docs.add_subparsers(dest="docs_command", required=True)
    get = documents.add_parser("get", help="Save one original without overwriting a local file")
    get.add_argument("wiki")
    get.add_argument("path", help="Source path under raw/, as cited by the wiki")
    get.add_argument("--output", required=True, type=Path)
    local = sub.add_parser("clone")
    local.add_argument("wiki")
    local.add_argument("directory", type=Path)
    local.add_argument("--read-only", action="store_true", help="Request a read-only repository credential")
    synchronize = sub.add_parser("sync")
    synchronize.add_argument("directory", nargs="?", type=Path, default=Path.cwd())
    history = sub.add_parser("log")
    history.add_argument("wiki")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("wiki")
    rollback.add_argument("revision")
    sub.add_parser("mcp", help="Run the wiki tools over local MCP stdio")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        store = CredentialStore()
        if store.agent() and args.command in ("login", "agent", "sync"):
            raise CLIError("Use your human sign-in configuration for login, agent management or Git copies.")
        if args.command == "sync":
            sync(args.directory, store)
            return 0
        server = args.server or os.environ.get("WIKI_SERVER_URL") or store.server()
        if not server:
            raise CLIError("Set the server with wiki --server https://wiki.example.com login.")
        client = APIClient(server, store)
        if args.command == "login":
            client.login(open_browser=not args.no_browser)
            return 0
        if args.command == "clone":
            clone(client, args.wiki, args.directory, read_only=args.read_only)
            return 0
        if args.command == "docs":
            client.download(args.wiki, args.path, args.output)
            return 0
        if args.command == "mcp":
            from .mcp import create_mcp

            create_mcp(HTTPService(client), subject_provider=lambda: "cli").run(transport="stdio")
            return 0
        if args.command == "create":
            value = client.request("POST", "/api/wikis", json={"slug": args.slug, "title": args.title or args.slug})
        elif args.command == "agent":
            if args.agent_command == "add":
                directory = args.directory.expanduser().absolute() if args.directory else None
                if directory and directory.exists():
                    raise CLIError("Choose a new private configuration directory for the agent.")
                value = client.request("POST", "/api/agents", json={"name": args.name})
                credential = value.pop("credential")
                directory = directory or store.directory / "agents" / value["id"].replace(":", "-")
                try:
                    directory.mkdir(mode=0o700, parents=True)  # Never reuse another principal's directory.
                    agent_store = CredentialStore(directory)
                    agent_store.set_server(server)
                    agent_store.set_agent(value["id"])
                    agent_store.put(agent_key(server, value["id"]), {"access_token": credential})
                except Exception:
                    client.request("POST", f"/api/agents/{value['id']}/revoke")
                    raise
                value["config_directory"] = str(directory)
            elif args.agent_command == "list":
                value = client.request("GET", "/api/agents")
            else:
                value = client.request("POST", f"/api/agents/{args.id}/revoke")
        elif args.command == "list":
            value = client.request("GET", "/api/wikis")
        elif args.command == "whoami":
            value = client.request("GET", "/api/me")
        elif args.command == "settings":
            path = f"/api/wikis/{args.wiki}/settings"
            value = (client.request("GET", path) if args.local_copies is None else
                     client.request("POST", path, json={"local_copies": args.local_copies == "allow"}))
        elif args.command == "user":
            value = (client.request("GET", "/api/users/settings", params={"subject": args.subject})
                     if args.local_copies is None else client.request("POST", "/api/users/settings", json={
                         "subject": args.subject, "local_copies": args.local_copies == "allow"}))
        elif args.command == "access":
            value = client.request("GET", f"/api/wikis/{args.wiki}/access")
        elif args.command in ("share", "unshare"):
            data = {"subject": args.subject}
            if args.command == "share":
                data["role"] = args.role
            value = client.request("POST", f"/api/wikis/{args.wiki}/{args.command}", json=data)
        elif args.command == "contribute":
            value = client.request(
                "POST",
                f"/api/wikis/{args.wiki}/ingest",
                json={
                    "filename": args.source.name,
                    "content_base64": base64.b64encode(args.source.read_bytes()).decode(),
                    "instructions": args.instructions,
                },
            )
        elif args.command == "status":
            value = client.request("GET", f"/api/jobs/{args.job}")
        elif args.command == "search":
            value = client.request(
                "GET", f"/api/wikis/{args.wiki}/search", params={"q": args.query, "limit": args.limit}
            )
        elif args.command == "read":
            value = client.request("GET", f"/api/wikis/{args.wiki}/page", params={"path": args.path})
            if isinstance(value, dict) and "content" in value:
                print(value["content"])
                return 0
        elif args.command == "log":
            value = client.request("GET", f"/api/wikis/{args.wiki}/history")
        else:
            value = client.request("POST", f"/api/wikis/{args.wiki}/rollback", json={"revision": args.revision})
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return 0
    except (CLIError, httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        # HTTP exceptions can include a URL. Credentials never enter our URLs.
        print(f"wiki: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
