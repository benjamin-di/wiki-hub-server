"""Real Git LFS object transfers must stay on the configured backend origin."""
import hashlib
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from wiki_hub.gitstore import GitStore


@pytest.fixture
def lfs_servers(tmp_path):
    if not shutil.which("git-lfs"):
        pytest.skip("Git LFS is required for object transfer integration tests.")
    state = SimpleNamespace(objects={}, public_requests=0, object_requests=[], public="", suffix=".git")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, body, code=200, content_type="application/octet-stream"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            response = []
            for obj in request["objects"]:
                action = "upload" if request["operation"] == "upload" else "download"
                response.append({**obj, "authenticated": True, "actions": {action: {
                    "href": f"{state.public}/owner/pilot{state.suffix}/info/lfs/objects/{obj['oid']}",
                    "header": {"Authorization": "Bearer synthetic-object-token"},
                }}})
            self.reply(json.dumps({"transfer": "basic", "objects": response}).encode(),
                       content_type="application/vnd.git-lfs+json")

        def object(self, upload=False):
            if self.server.public:
                state.public_requests += 1
                if upload:
                    self.rfile.read(int(self.headers["Content-Length"]))
                self.reply(b"Object data must use the internal origin", 400)
                return
            assert self.headers.get("Authorization") == "Bearer synthetic-object-token"
            oid = self.path.rsplit("/", 1)[-1]
            state.object_requests.append(("upload" if upload else "download", self.path))
            if upload:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                assert hashlib.sha256(raw).hexdigest() == oid
                state.objects[oid] = raw
                self.reply(b"")
            else:
                self.reply(state.objects[oid])

        def do_PUT(self):
            self.object(upload=True)

        def do_GET(self):
            self.object()

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    for index, server in enumerate(servers):
        server.public = bool(index)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    internal, public = [f"http://127.0.0.1:{server.server_port}" for server in servers]
    state.public = public
    settings = SimpleNamespace(state_dir=tmp_path / "state", workspace_dir=tmp_path / "work",
                               forgejo_url=internal, forgejo_public_url=public,
                               forgejo_username="test-user", forgejo_password="synthetic-backend-credential")
    forgejo = SimpleNamespace(repository_url=lambda owner, slug, public=False:
                             f"{settings.forgejo_public_url if public else internal}/{owner}/{slug}.git")
    store = GitStore(settings, forgejo)
    repo = tmp_path / "client"
    store.run(tmp_path, "init", "--initial-branch=main", str(repo))
    store.run(repo, "config", "remote.origin.url", forgejo.repository_url("owner", "pilot"))
    store.run(repo, "config", "lfs.transfer.maxretries", "0")
    try:
        yield store, repo, {"repo_owner": "owner", "slug": "pilot"}, state
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize("suffix", [".git", ""])
@pytest.mark.parametrize("operation", ["upload", "download"])
def test_native_lfs_rewrites_advertised_object_links_without_sending_data_to_public_origin(
    lfs_servers, suffix, operation
):
    store, repo, wiki, state = lfs_servers
    state.suffix = suffix
    raw = b"Synthetic immutable source\x00\xff\n" * 100
    oid = hashlib.sha256(raw).hexdigest()
    if operation == "upload":
        store.lfs(repo, wiki, "clean", input_data=raw)
        store.lfs(repo, wiki, "push", "--object-id", "origin", oid, timeout=20)
        assert state.objects[oid] == raw
    else:
        state.objects[oid] = raw
        pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(raw)}\n".encode()
        assert store.lfs(repo, wiki, "smudge", input_data=pointer, smudge=True, timeout=20) == raw
    assert state.public_requests == 0
    assert state.object_requests == [(operation, f"/owner/pilot{suffix}/info/lfs/objects/{oid}")]
