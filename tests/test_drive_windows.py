"""The one-line Windows drive connection, run in real PowerShell against the in-process application."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
from dataclasses import replace
from importlib.resources import files

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient
from test_drive import FakeAgent, drive_service, drive_settings  # noqa: F401 - shared fixtures
from test_http_mcp import FixtureVerifier

from wiki_hub.server import create_app

pytestmark = pytest.mark.skipif(not hasattr(os, "setxattr"), reason="Linux ACL support is required")

# Windows operations are replaced after the served script is loaded; everything else runs as served.
HARNESS = r"""
param([string]$Server, [string]$Action, [string]$StatePath)
$text = Invoke-RestMethod -Uri "$Server/drive/windows?action=$Action" -UseBasicParsing
$text = $text -replace "(?m)^Invoke-WikiDrive '[a-z]+'\s*$", ''
. ([scriptblock]::Create($text))
$saved = Get-Content -Raw $StatePath | ConvertFrom-Json
$global:State = @{ mapped = @{}; credentials = @{}; labels = @{}; calls = @() }
foreach ($name in 'mapped', 'credentials', 'labels') {
    foreach ($p in $saved.$name.PSObject.Properties) { $global:State[$name][$p.Name] = $p.Value }
}
function Assert-WikiDriveWindows { }
function Open-WikiDriveBrowser([string]$Address) { $global:State.opened = $Address }
function Open-WikiDriveFolder([string]$Letter) { }
function Test-WikiDriveLetterInUse([string]$Letter) { return $false }
function Save-WikiDriveCredential([string]$Target, [string]$User, [string]$Password) {
    $global:State.credentials[$Target] = @($User, $Password)
}
function Remove-WikiDriveCredential([string]$Target) { $global:State.credentials.Remove($Target) }
function Set-WikiDriveLabel([string]$Unc, [string]$Text) { $global:State.labels[$Unc] = $Text }
function Invoke-WikiDriveNet([string[]]$Arguments) {
    $global:State.calls += ,($Arguments -join ' ')
    if ($Arguments.Count -eq 1) {
        $lines = foreach ($l in $global:State.mapped.Keys) {
            "OK           $l        $($global:State.mapped[$l])   Microsoft Windows Network"
        }
        return @{ Output = ($lines -join "`n"); ExitCode = 0 }
    }
    if ($Arguments[1] -eq '/?') { return @{ Output = 'NET USE [/REQUIREINTEGRITY] [/REQUIREPRIVACY]'; ExitCode = 0 } }
    if ($Arguments -contains '/delete') {
        $global:State.mapped.Remove($Arguments[1])
        return @{ Output = ''; ExitCode = 0 }
    }
    if ($env:WIKI_FAIL_MAP) { return @{ Output = 'System error 53 has occurred.'; ExitCode = 2 } }
    $global:State.mapped[$Arguments[1]] = $Arguments[2]
    return @{ Output = ''; ExitCode = 0 }
}
$env:COMPUTERNAME = 'LAPTOP'
Invoke-WikiDrive $Action
$global:State | ConvertTo-Json -Depth 5 | Set-Content -Path $StatePath
"""


def fake_identity(base, polls):
    async def discovery(_request):
        return JSONResponse({"device_authorization_endpoint": base + "/oidc/device",
                             "token_endpoint": base + "/oidc/token"})

    async def device(request: Request):
        form = await request.form()
        assert form["client_id"] == "wiki-cli" and form["resource"] == "https://wiki.example.test/mcp"
        return JSONResponse({"device_code": "synthetic-device", "user_code": "ABCD-EFGH",
                             "verification_uri": base + "/oidc/device-login",
                             "verification_uri_complete": base + "/oidc/device-login?code=ABCD-EFGH",
                             "interval": 1, "expires_in": 60})

    async def token(request: Request):
        form = await request.form()
        assert form["device_code"] == "synthetic-device"
        polls.append(form["grant_type"])
        if len(polls) % 2:
            return JSONResponse({"error": "authorization_pending"}, status_code=400)
        return JSONResponse({"access_token": "owner-token", "token_type": "Bearer", "expires_in": 300})

    return Starlette(routes=[Route("/.well-known/openid-configuration", discovery),
                             Route("/device", device, methods=["POST"]), Route("/token", token, methods=["POST"])])


def test_the_script_is_served_without_sign_in_only_when_the_drive_exists(drive_service, drive_settings,  # noqa: F811
                                                                         service, settings):
    with TestClient(create_app(drive_settings, drive_service, FixtureVerifier())) as client:
        served = client.get("/drive/windows")
        assert served.status_code == 200 and served.headers["content-type"].startswith("text/plain")
        assert "$WikiDriveServer = 'https://wiki.example.test'" in served.text
        assert served.text.rstrip().endswith("Invoke-WikiDrive 'connect'") and "__WIKI" not in served.text
        assert client.get("/drive/windows?action=disconnect").text.rstrip().endswith("Invoke-WikiDrive 'disconnect'")
        assert client.get("/drive/windows?action=format").status_code == 400
    with TestClient(create_app(settings, service, FixtureVerifier())) as client:
        assert client.get("/drive/windows").status_code == 404


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is required")
def test_disconnect_retries_pending_revocations_and_reports_server_confirmation(tmp_path):
    script = files("wiki_hub").joinpath("drive_windows.ps1").read_text()
    script = script.rsplit("Invoke-WikiDrive '__WIKI_ACTION__'", 1)[0]
    harness = tmp_path / "pending.ps1"
    harness.write_text(script + r'''
$ErrorActionPreference = 'Stop'
$global:Calls = @()
$global:Pending = $true
function Dismount-WikiDriveShares($Shares) { }
function Remove-WikiDriveCredential($Server) { }
function Invoke-WikiDriveApi($Method, $Path, $Token, $Body) {
    $global:Calls += $Path
    return @{ revoked = $true; pending = $global:Pending }
}
$status = @{ server = 'files.example.test'; unc = '\\files.example.test\wikis'; devices = @(
    @{ id = 'drive-test'; name = 'LAPTOP'; revoked = $false; pending = $false },
    @{ id = 'drive-other'; name = 'DESKTOP'; revoked = $false; pending = $false }
) }
Disconnect-WikiDrive 'synthetic' $status 'LAPTOP'
$status.devices[0].revoked = $true
$status.devices[0].pending = $true
$global:Pending = $false
Disconnect-WikiDrive 'synthetic' $status 'LAPTOP'
$unexpected = @($global:Calls | Where-Object { $_ -ne '/api/drive/devices/drive-test/revoke' })
if ($global:Calls.Count -ne 2 -or $unexpected.Count) {
    throw 'Revocation was not retried for this computer alone.'
}
''')
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "server revocation is pending" in result.stdout
    assert result.stdout.count("1 login(s) revoked") == 1


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is required")
async def test_one_line_windows_connection_repairs_and_disconnects(drive_service, drive_settings, tmp_path):  # noqa: F811
    await drive_service.create("owner", "starter", "Starter")
    agent = FakeAgent(drive_settings.drive_control_dir)
    agent_task = asyncio.create_task(agent.run())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    polls = []
    hub = create_app(replace(drive_settings, public_url=base, oidc_issuer=base + "/oidc"), drive_service,
                     FixtureVerifier())
    app = Starlette(routes=[Mount("/oidc", fake_identity(base, polls)), Mount("/", hub)])
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    harness = tmp_path / "harness.ps1"
    harness.write_text(HARNESS)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"mapped": {"L:": "\\\\192.0.2.10\\shared"}, "credentials": {}, "labels": {}}))

    async def run(action, **env):
        process = await asyncio.create_subprocess_exec(
            "pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness), base, action, str(state_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, **env})
        output = (await asyncio.wait_for(process.communicate(), 120))[0].decode()
        assert process.returncode == 0, output
        return output, json.loads(state_path.read_text())

    def devices():
        return {d["id"]: d for d in drive_service.state.drive_devices("owner")}

    try:
        while not server.started:
            await asyncio.sleep(0.05)
        output, state = await run("connect")
        assert "Connected. Your wikis are in Explorer as W: (Wiki)." in output, output
        assert "edit pages in the wiki folder of 1 wiki(s)" in output and polls
        assert state["opened"].endswith("?code=ABCD-EFGH")
        assert state["mapped"] == {"L:": "\\\\192.0.2.10\\shared", "W:": "\\\\files.example.test\\wikis",
                                   "I:": "\\\\files.example.test\\inbox"}
        assert state["labels"] == {"\\\\files.example.test\\wikis": "Wiki",
                                   "\\\\files.example.test\\inbox": "Wiki Inbox"}
        login, password = state["credentials"]["files.example.test"]
        assert login == "wd1" and len(password) >= 40 and password not in output
        mapped = "use W: \\\\files.example.test\\wikis /persistent:yes /requireprivacy /requireintegrity"
        assert mapped in state["calls"]
        first = [d for d in devices().values() if not d["revoked"]]
        assert [d["name"] for d in first] == ["LAPTOP"]

        # Running it again repairs the connection with a new login and revokes this computer's old one.
        output, state = await run("connect")
        assert state["credentials"]["files.example.test"][0] == "wd2" and state["mapped"]["W:"].endswith("wikis")
        assert devices()[first[0]["id"]]["revoked"] == 1
        assert [d["name"] for d in devices().values() if not d["revoked"]] == ["LAPTOP"]

        # A mapping that fails leaves no usable login behind and says so.
        output, state = await run("connect", WIKI_FAIL_MAP="1")
        assert "The Wiki drive was not connected: Windows could not map W:" in output, output
        assert state["credentials"] == {} and "W:" not in state["mapped"]
        assert all(d["revoked"] for d in devices().values())

        await run("connect")
        output, state = await run("disconnect")
        assert "Disconnected the Wiki drive from this computer (1 login(s) revoked)." in output, output
        assert state["credentials"] == {} and state["mapped"] == {"L:": "\\\\192.0.2.10\\shared"}
        assert all(d["revoked"] for d in devices().values())
    finally:
        server.should_exit = True
        await serving
        agent.running = False
        await agent_task
