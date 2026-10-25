"""Shared sign-ins serialize refresh across independent CLI processes."""

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from wiki_hub import cli

CHILD = r"""
import json, sys, time
from pathlib import Path
import httpx
from wiki_hub import cli

# Exercise the real OS lock with a synthetic filesystem credential store on
# every platform; native credential-manager coverage is a separate test.
cli.WINDOWS = False
try:
    store = cli.CredentialStore(Path(sys.argv[1]), lock_timeout=float(sys.argv[5]))
    store.keyring = None
    with httpx.Client(timeout=5, trust_env=False) as http:
        client = cli.APIClient(sys.argv[2], store, http)
        Path(sys.argv[3]).touch()
        deadline = time.monotonic() + 10
        while not Path(sys.argv[4]).exists():
            if time.monotonic() >= deadline:
                sys.exit(91)
            time.sleep(0.01)
        result = client.request('GET', '/api/me')
        assert result == {'subject': 'synthetic-person'}
except cli.CLIError:
    print(json.dumps({'ok': False}))
    sys.exit(23)
print(json.dumps({'ok': True}))
"""


@pytest.fixture
def provider():
    state = SimpleNamespace(refreshes=0, refresh_used=False, mutex=threading.Lock(),
                            fail_next=False, block_refresh=False, block_old_access=False,
                            refresh_started=threading.Event(), access_started=threading.Event(),
                            release=threading.Event(), processes=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.path == "/token"
            data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            assert data["grant_type"] == ["refresh_token"]
            with state.mutex:
                state.refreshes += 1
            state.refresh_started.set()
            if state.block_refresh and not state.release.wait(10):
                return self.respond(503, {})
            with state.mutex:
                if state.fail_next:
                    state.fail_next = False
                    return self.respond(503, {})
                if data["refresh_token"] != ["synthetic-original-refresh"] or state.refresh_used:
                    return self.respond(400, {"error": "invalid_grant"})
                state.refresh_used = True
            self.respond(200, {"access_token": "synthetic-fresh-access",
                               "refresh_token": "synthetic-rotated-refresh", "expires_in": 300})

        def do_GET(self):
            assert self.path == "/api/me"
            authorization = self.headers.get("Authorization")
            if authorization == "Bearer synthetic-original-access" and state.block_old_access:
                state.access_started.set()
                if not state.release.wait(10):
                    return self.respond(503, {})
            if authorization in {"Bearer synthetic-fresh-access", "Bearer synthetic-login-access"}:
                return self.respond(200, {"subject": "synthetic-person"})
            self.respond(401, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        state.release.set()
        for process in state.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def prepare_store(directory, provider, monkeypatch, *, expired=True):
    monkeypatch.setattr(cli, "WINDOWS", False)
    store = cli.CredentialStore(directory)
    store.keyring = None
    store.put(cli.oauth_key(provider.url), {
        "access_token": "synthetic-original-access", "refresh_token": "synthetic-original-refresh",
        "expires_at": 0 if expired else time.time() + 300,
        "token_endpoint": provider.url + "/token", "client_id": "example-cli",
        "resource": "https://wiki.example.test/mcp",
    })
    return store


def spawn(provider, store, marker, *, timeout=5):
    ready = marker.with_name(marker.name + "-ready-" + str(len(provider.processes)))
    process = subprocess.Popen([sys.executable, "-c", CHILD, str(store.directory), provider.url,
                                str(ready), str(marker), str(timeout)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    provider.processes.append(process)
    return process, ready


def await_ready(paths):
    deadline = time.monotonic() + 10
    while not all(path.exists() for path in paths):
        assert time.monotonic() < deadline, "Synthetic CLI process did not become ready"
        time.sleep(0.01)


def finish(process, expected=0):
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == expected, stderr
    assert json.loads(stdout) == {"ok": expected == 0}


def test_concurrent_processes_refresh_once_and_reuse_the_rotated_cache(tmp_path, provider, monkeypatch):
    store = prepare_store(tmp_path / "shared-config", provider, monkeypatch)
    provider.block_refresh = True
    marker = tmp_path / "start"
    children = [spawn(provider, store, marker) for _ in range(3)]
    await_ready([ready for _, ready in children])
    marker.touch()
    assert provider.refresh_started.wait(10)
    # All processes have initialized; keep the first refresh in flight so an
    # unprotected read/refresh sequence would reuse its single-use old token.
    time.sleep(0.2)
    provider.release.set()
    for process, _ in children:
        finish(process)
    assert provider.refreshes == 1
    token = store.get(cli.oauth_key(provider.url))
    assert token["refresh_token"] == "synthetic-rotated-refresh"
    assert token["expires_at"] > time.time() + 100


def test_lock_wait_has_a_timeout_and_releases_for_a_later_process(tmp_path, provider, monkeypatch):
    store = prepare_store(tmp_path / "shared-config", provider, monkeypatch)
    marker = tmp_path / "start"
    marker.touch()
    with store.locked():
        process, _ = spawn(provider, store, marker, timeout=0.1)
        finish(process, 23)
    assert provider.refreshes == 0
    process, _ = spawn(provider, store, marker)
    finish(process)
    assert provider.refreshes == 1


def test_failed_provider_response_releases_the_credential_lock(tmp_path, provider, monkeypatch):
    store = prepare_store(tmp_path / "shared-config", provider, monkeypatch)
    provider.fail_next = True
    marker = tmp_path / "start"
    marker.touch()
    process, _ = spawn(provider, store, marker)
    finish(process, 23)
    assert store.get(cli.oauth_key(provider.url))["refresh_token"] == "synthetic-original-refresh"
    # The failure did not consume a refresh token in this synthetic provider.
    process, _ = spawn(provider, store, marker)
    finish(process)
    assert provider.refreshes == 2


@pytest.mark.parametrize("replace_login", [False, True])
def test_delayed_rejection_uses_new_login_or_keeps_credentials_cleared(
    tmp_path, provider, monkeypatch, replace_login,
):
    store = prepare_store(tmp_path / "shared-config", provider, monkeypatch, expired=False)
    provider.block_old_access = True
    marker = tmp_path / "start"
    marker.touch()
    process, _ = spawn(provider, store, marker)
    assert provider.access_started.wait(10)
    key = cli.oauth_key(provider.url)
    if replace_login:
        store.put(key, {"access_token": "synthetic-login-access", "refresh_token": "synthetic-login-refresh",
                        "expires_at": time.time() + 300})
    else:
        with store.locked():
            data = store._read()
            data["credentials"].pop(key)
            store._write(data)
    provider.release.set()
    finish(process, 0 if replace_login else 23)
    assert provider.refreshes == 0
    assert (store.get(key) is not None) is replace_login
