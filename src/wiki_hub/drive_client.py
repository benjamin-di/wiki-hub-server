"""Connect this computer to the Wiki drive.

The server issues one revocable credential per device. On Windows it goes
straight into Credential Manager and is never displayed; the drive is mapped
with `net use` and labelled in Explorer. Other systems receive instructions.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from typing import Any

WINDOWS = os.name == "nt"
CRED_TYPE_DOMAIN_PASSWORD = 2
CRED_PERSIST_LOCAL_MACHINE = 2


class DriveClientError(Exception):
    pass


class WindowsDrive:
    """Native Windows operations; output from Windows tools is never displayed."""

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["net", *args], capture_output=True, text=True, errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def mappings(self) -> dict[str, str]:
        result = self.run("use")
        found = {}
        for line in result.stdout.splitlines():
            letter = re.search(r"(?<![\\\w])([A-Za-z]:)(?=\s)", line)
            remote = re.search(r"(\\\\[^\s]+)", line)
            if letter and remote:
                found[letter.group(1).upper()] = remote.group(1)
        return found

    def free_letter(self, preferred: str = "W:") -> str:
        used = set(self.mappings())
        for letter in [preferred.upper()] + [f"{c}:" for c in "WZYXVUTSRQPONMLKJIHGFED"]:
            if letter not in used and not os.path.exists(letter + "\\"):
                return letter
        raise DriveClientError("No free drive letter is available.")

    def supports_required_privacy(self) -> bool:
        return "REQUIREPRIVACY" in self.run("use", "/?").stdout.upper()

    def save_credential(self, target: str, username: str, password: str) -> None:
        import ctypes
        from ctypes import wintypes

        class Credential(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                        ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.c_void_p),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]

        blob = password.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(blob, len(blob))
        try:
            credential = Credential(Type=CRED_TYPE_DOMAIN_PASSWORD, TargetName=target, CredentialBlobSize=len(blob),
                                    CredentialBlob=ctypes.cast(buffer, ctypes.c_void_p),
                                    Persist=CRED_PERSIST_LOCAL_MACHINE, UserName=username)
            write = ctypes.WinDLL("advapi32", use_last_error=True).CredWriteW
            if not write(ctypes.byref(credential), 0):
                raise DriveClientError(f"Windows Credential Manager refused the drive login "
                                       f"(Windows error {ctypes.get_last_error()}).")
        finally:
            ctypes.memset(buffer, 0, len(blob))

    def delete_credential(self, target: str) -> None:
        import ctypes

        ctypes.WinDLL("advapi32", use_last_error=True).CredDeleteW(target, CRED_TYPE_DOMAIN_PASSWORD, 0)

    def map(self, letter: str, unc: str) -> None:
        options = ["/persistent:yes"]
        if self.supports_required_privacy():
            options += ["/requireprivacy", "/requireintegrity"]
        result = self.run("use", letter, unc, *options)
        if result.returncode:
            raise DriveClientError(f"Windows could not map {letter} (net use exit {result.returncode}). "
                                   "Check that this computer is on the home network or private route.")

    def unmap(self, letter: str) -> None:
        self.run("use", letter, "/delete", "/y")

    def label(self, unc: str, text: str) -> None:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\MountPoints2" + "\\" + unc.replace("\\", "#")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as handle:
            winreg.SetValueEx(handle, "_LabelFromReg", 0, winreg.REG_SZ, text)


def _status(client) -> dict[str, Any]:
    try:
        return client.request("GET", "/api/drive")
    except Exception as exc:
        if "HTTP 400" in str(exc):
            raise DriveClientError("This server does not offer a network drive.") from None
        raise


def _record(store, server: str) -> dict[str, Any] | None:
    with store.locked():
        return store._read().get("drives", {}).get(server)


def _save_record(store, server: str, record: dict[str, Any] | None) -> None:
    with store.locked():
        data = store._read()
        drives = data.setdefault("drives", {})
        if record is None:
            drives.pop(server, None)
        else:
            drives[server] = record
        store._write(data)


def _map_inbox(windows, status, result):
    unc = status.get("inbox_unc")
    if not unc:
        return
    mapped = {remote.lower(): drive for drive, remote in windows.mappings().items()}
    letter = mapped.get(unc.lower())
    if not letter:
        letter = windows.free_letter("I:")
        windows.map(letter, unc)
    windows.label(unc, "Wiki Inbox")
    result.update(inbox_unc=unc, inbox_letter=letter)


def connect(client, store, *, letter: str | None = None, name: str | None = None, label: str = "Wiki",
            windows: WindowsDrive | None = None, out=print) -> dict[str, Any]:
    status = _status(client)
    record = _record(store, client.server)
    active = {d["id"] for d in status["devices"] if not d["revoked"]}
    if WINDOWS or windows is not None:
        windows = windows or WindowsDrive()
        mapped = {remote.lower(): drive for drive, remote in windows.mappings().items()}
        current = mapped.get(status["unc"].lower())
        if record and record.get("device") in active and current:
            windows.label(status["unc"], label)
            result = {**record, "letter": current}
            _map_inbox(windows, status, result)
            _save_record(store, client.server, result)
            out(f"The Wiki drive is already connected as {current}.")
            return result
        target = (letter.upper().rstrip(":") + ":") if letter else current or windows.free_letter()
    credential = client.request("POST", "/api/drive/devices", json={"name": name or platform.node() or "Computer"})
    device = credential["device"]["id"]
    saved = False
    try:
        if windows is None:
            out(f"Connect {credential['unc']} (smb://{credential['server']}/{credential['share']}) as "
                f"{credential['login']} with this one-time password:")
            out(credential["password"])
            out("Store it in your system's password manager; it is not shown again.")
            result = {"device": device, "unc": credential["unc"]}
        else:
            windows.save_credential(credential["server"], credential["login"], credential["password"])
            saved = True
            # Both shares may hold the previous device's SMB session.
            for mapped_letter, remote in windows.mappings().items():
                if remote.lower() in {credential["unc"].lower(), status.get("inbox_unc", "").lower()}:
                    windows.unmap(mapped_letter)
            windows.map(target, credential["unc"])
            windows.label(credential["unc"], label)
            result = {"device": device, "unc": credential["unc"], "letter": target}
            _map_inbox(windows, status, result)
            editable = sum(wiki.get("access") == "write" for wiki in status["wikis"])
            out(f"Connected the Wiki drive as {target} ({credential['unc']}). "
                + (f"You can edit pages in the wiki folder of {editable} wiki(s)." if editable
                   else "It is read-only for you."))
    except BaseException:
        if windows is not None and saved:
            for mapped_letter, remote in windows.mappings().items():
                if remote.lower() in {credential["unc"].lower(), status.get("inbox_unc", "").lower()}:
                    windows.unmap(mapped_letter)
        # Each cleanup step runs even if an earlier one fails; the original error is reported.
        steps = [lambda: client.request("POST", f"/api/drive/devices/{device}/revoke")]
        if saved:
            # The saved login replaced the previous device's; neither is usable now.
            steps.append(lambda: windows.delete_credential(credential["server"]))
            if record and record.get("device") in active:
                steps.append(lambda: client.request("POST", f"/api/drive/devices/{record['device']}/revoke"))
            steps.append(lambda: _save_record(store, client.server, None))
        for step in steps:
            try:
                step()
            except Exception:
                pass
        if saved:
            out("The Wiki drive was not connected. Run wiki drive connect again on the home network.")
        raise
    finally:
        credential.pop("password", None)
    _save_record(store, client.server, result)
    if record and record.get("device") not in (None, device) and record["device"] in active:
        client.request("POST", f"/api/drive/devices/{record['device']}/revoke")
    return result


def disconnect(client, store, *, windows: WindowsDrive | None = None, out=print) -> dict[str, Any]:
    record = _record(store, client.server)
    if not record:
        raise DriveClientError("This computer has no Wiki drive connection for this server.")
    if WINDOWS or windows is not None:
        windows = windows or WindowsDrive()
        status_unc = record["unc"].lower()
        for drive, remote in windows.mappings().items():
            if remote.lower() in {status_unc, record.get("inbox_unc", "").lower()}:
                windows.unmap(drive)
        windows.delete_credential(record["unc"].lstrip("\\").split("\\", 1)[0])
    result = client.request("POST", f"/api/drive/devices/{record['device']}/revoke")
    if not result.get("pending"):
        _save_record(store, client.server, None)
    out("Disconnected the Wiki drive" + (" (server confirmation pending; repeat to finish)."
                                         if result.get("pending") else "."))
    return result


def status(client, store, *, windows: WindowsDrive | None = None, revoked: bool = False) -> dict[str, Any]:
    value = _status(client)
    if not revoked:
        value["devices"] = [device for device in value["devices"] if not device["revoked"]]
    record = _record(store, client.server)
    value["this_device"] = record["device"] if record else None
    if record and (WINDOWS or windows is not None):
        windows = windows or WindowsDrive()
        value["letter"] = next((drive for drive, remote in windows.mappings().items()
                                if remote.lower() == value["unc"].lower()), None)
    if value.get("file_server") == "unavailable":
        print("The file server is not reporting. The drive works on the home network or private route.",
              file=sys.stderr)
    return value
