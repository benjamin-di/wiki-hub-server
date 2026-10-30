"""Drive API endpoints and the Windows connection flow, with fake Windows tools."""
import asyncio
import os

import httpx
import pytest
from test_drive import FakeAgent, drive_service, drive_settings  # noqa: F401 - shared fixtures
from test_http_mcp import FixtureVerifier

from wiki_hub import drive_client
from wiki_hub.cli import CredentialStore
from wiki_hub.server import create_app

pytestmark = pytest.mark.skipif(not hasattr(os, "setxattr"), reason="Linux ACL support is required")


class FakeWindows(drive_client.WindowsDrive):
    def __init__(self, mapped=None):
        self.mapped = dict(mapped or {})
        self.credentials = {}
        self.labels = {}
        self.calls = []

    def mappings(self):
        return dict(self.mapped)

    def free_letter(self, preferred="W:"):
        return next(f"{c}:" for c in "WZYX" if f"{c}:" not in self.mapped)

    def save_credential(self, target, username, password):
        self.credentials[target] = (username, password)

    def delete_credential(self, target):
        self.credentials.pop(target, None)

    def map(self, letter, unc):
        self.calls.append(("map", letter, unc))
        self.mapped[letter] = unc

    def unmap(self, letter):
        self.calls.append(("unmap", letter))
        self.mapped.pop(letter, None)

    def label(self, unc, text):
        self.labels[unc] = text


class AppClient:
    """The CLI client's request interface against the in-process application."""

    def __init__(self, app, token):
        self.server = "https://wiki.example.test"
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=self.server)
        self.token = token

    async def arequest(self, method, path, **kwargs):
        response = await self.http.request(method, path, headers={"Authorization": f"Bearer {self.token}"},
                                           **kwargs)
        if not response.is_success:
            raise drive_client.DriveClientError(f"Request failed (HTTP {response.status_code}).")
        return response.json()


async def run_cli(function, client, *args, **kwargs):
    """Run a synchronous CLI flow whose requests go to the async in-process app."""
    loop = asyncio.get_running_loop()

    class Bridge:
        server = client.server

        def request(self, method, path, **request_kwargs):
            return asyncio.run_coroutine_threadsafe(client.arequest(method, path, **request_kwargs), loop).result(30)

    return await asyncio.to_thread(function, Bridge(), *args, **kwargs)


@pytest.fixture
async def agent_task(drive_settings):  # noqa: F811
    fake = FakeAgent(drive_settings.drive_control_dir)
    task = asyncio.create_task(fake.run())
    yield fake
    fake.running = False
    await task


async def test_windows_connect_saves_hidden_credential_maps_and_reconnects(drive_service, drive_settings,  # noqa: F811
                                                                           agent_task, tmp_path):
    await drive_service.create("owner", "starter", "Starter")
    app = create_app(drive_settings, drive_service, FixtureVerifier())
    client = AppClient(app, "owner-token")
    store = CredentialStore(tmp_path / "cli")
    windows = FakeWindows({"L:": "\\\\192.0.2.10\\shared"})
    printed = []
    result = await run_cli(drive_client.connect, client, store, windows=windows, out=printed.append)
    assert result["letter"] == "W:"
    username, password = windows.credentials["files.example.test"]
    assert username == "wd1" and len(password) >= 40
    assert windows.mapped["W:"] == "\\\\files.example.test\\wikis" and windows.mapped["L:"].endswith("shared")
    assert windows.labels == {"\\\\files.example.test\\wikis": "Wiki",
                              "\\\\files.example.test\\inbox": "Wiki Inbox"}
    assert windows.mapped[result["inbox_letter"]] == "\\\\files.example.test\\inbox"
    assert password not in "".join(printed) and password not in (tmp_path / "cli" / "config.json").read_text()
    assert printed[-1].endswith("You can edit pages in the wiki folder of 1 wiki(s).")
    # Connecting again is idempotent while the device remains valid.
    again = await run_cli(drive_client.connect, client, store, windows=windows, out=printed.append)
    assert again["device"] == result["device"] and len(windows.calls) == 2
    # A revoked device is replaced; the old mapping is recreated with the new login.
    await drive_service.revoke_drive_device("owner", result["device"])
    replaced = await run_cli(drive_client.connect, client, store, windows=windows, out=printed.append)
    assert replaced["device"] != result["device"] and windows.credentials["files.example.test"][0] == "wd2"
    assert ("unmap", "W:") in windows.calls
    status = await run_cli(drive_client.status, client, store, windows=windows)
    assert status["letter"] == "W:" and status["this_device"] == replaced["device"]
    assert [d["id"] for d in status["devices"]] == [replaced["device"]]
    everything = await run_cli(drive_client.status, client, store, windows=windows, revoked=True)
    assert len(everything["devices"]) == 2
    gone = await run_cli(drive_client.disconnect, client, store, windows=windows, out=printed.append)
    assert gone["revoked"] is True and "W:" not in windows.mapped and windows.credentials == {}
    assert drive_service.state.drive_device(replaced["device"])["revoked"] == 1


async def test_failed_mapping_revokes_the_new_device(drive_service, drive_settings, agent_task, tmp_path):  # noqa: F811
    app = create_app(drive_settings, drive_service, FixtureVerifier())
    client = AppClient(app, "owner-token")

    class Unreachable(FakeWindows):
        def map(self, letter, unc):
            raise drive_client.DriveClientError("Windows could not map W:")

    windows = Unreachable()
    with pytest.raises(drive_client.DriveClientError):
        await run_cli(drive_client.connect, client, CredentialStore(tmp_path / "cli"), windows=windows,
                      out=lambda _text: None)
    assert [d["revoked"] for d in drive_service.state.drive_devices("owner")] == [1]
    assert windows.credentials == {}


async def test_drive_endpoints_require_people_and_hide_secrets(drive_service, drive_settings, agent_task,  # noqa: F811
                                                               service, settings):
    app = create_app(drive_settings, drive_service, FixtureVerifier())
    client = AppClient(app, "stranger-token")
    created = await client.arequest("POST", "/api/drive/devices", json={"name": "Phone"})
    assert created["login"] == "wd1" and "password" in created
    listed = await client.arequest("GET", "/api/drive/devices")
    assert "password" not in str(listed)
    with pytest.raises(drive_client.DriveClientError, match="403"):
        await client.arequest("GET", "/api/drive/devices", params={"subject": "owner"})
    owner = AppClient(app, "owner-token")
    assert (await owner.arequest("GET", "/api/drive/devices", params={"subject": "stranger"}))[0]["name"] == "Phone"
    disabled = AppClient(create_app(settings, service, FixtureVerifier()), "owner-token")
    with pytest.raises(drive_client.DriveClientError, match="400"):
        await disabled.arequest("GET", "/api/drive")


def test_windows_mapping_parser_reads_net_use_output():
    class Output(drive_client.WindowsDrive):
        def run(self, *args):
            text = ("New connections will be remembered.\n\nStatus       Local     Remote                    Network\n"
                    "-------------------------------------------------------------------------------\n"
                    "OK           W:        \\\\wiki.example.test\\wikis   Microsoft Windows Network\n"
                    "Unavailable  L:        \\\\192.0.2.10\\shared       Microsoft Windows Network\n"
                    "The command completed successfully.\n")
            return type("Result", (), {"stdout": text, "returncode": 0})()

    assert Output().mappings() == {"W:": "\\\\wiki.example.test\\wikis", "L:": "\\\\192.0.2.10\\shared"}


def test_disconnect_retains_device_until_revocation_is_confirmed(tmp_path):
    class Client:
        server = "https://wiki.example.test"
        pending = True

        def request(self, method, path):
            assert (method, path) == ("POST", "/api/drive/devices/drive-test/revoke")
            return {"id": "drive-test", "revoked": True, "pending": self.pending}

    client, store = Client(), CredentialStore(tmp_path / "cli")
    store.keyring = None
    record = {"device": "drive-test", "unc": "\\\\files.example.test\\wikis"}
    drive_client._save_record(store, client.server, record)
    windows, printed = FakeWindows({"W:": record["unc"]}), []
    result = drive_client.disconnect(client, store, windows=windows, out=printed.append)
    assert result["pending"] and "pending" in printed[-1]
    assert windows.mapped == {}
    assert drive_client._record(store, client.server) == record
    client.pending = False
    assert not drive_client.disconnect(client, store, windows=windows, out=printed.append)["pending"]
    assert drive_client._record(store, client.server) is None


@pytest.mark.parametrize("observation", [None, {"age": 120, "generation": 1, "devices": {}},
                                         {"age": 0, "generation": 0, "devices": {}}])
async def test_missing_or_stale_observation_cannot_confirm_revocation(drive_service, observation):  # noqa: F811
    device = drive_service.state.add_drive_device("drive-test", "owner", "Computer")
    drive_service.state.revoke_drive_device(device["id"])
    device = drive_service.state.drive_device(device["id"])
    assert drive_service._public_device(device, observation)["pending"]
    current = {"age": 0, "generation": drive_service.state.drive_generation(), "devices": {}}
    assert not drive_service._public_device(device, current)["pending"]
