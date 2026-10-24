#!/usr/bin/env python3
"""Configure Pocket ID clients; optionally prepare the operator's first account.

Passkeys are always enrolled by the person. An explicitly requested one-time
enrollment URL is written to a private file, never printed.
"""

import argparse
import json
import os
import uuid
from pathlib import Path

import httpx

CLIENTS = (
    ("wiki-cli", "Wiki CLI", ["http://127.0.0.1/callback"]),
    ("wiki-claude", "Wiki Hub for Claude", ["https://claude.ai/api/mcp/auth_callback"]),
    ("wiki-chatgpt", "Wiki Hub for ChatGPT", ["https://chatgpt.com/connector_platform_oauth_redirect"]),
)


class SetupError(Exception):
    pass


def request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if not response.is_success:
        raise SetupError(f"Identity setup failed (HTTP {response.status_code}); provider output withheld")
    return response.json() if response.content else None


def items(client, path):
    result = []
    for page in range(1, 1001):
        value = request(client, "GET", path, params={"pagination[page]": page, "pagination[limit]": 100})
        if isinstance(value, list):
            return value
        batch = value["data"]
        if not batch:
            return result
        result.extend(batch)
        pagination = value.get("pagination", {})
        total = pagination.get("totalPages")
        if type(total) is not int or total < 1 or pagination.get("currentPage") != page:
            raise SetupError("Identity pagination is missing or inconsistent")
        if page >= total:
            return result
    raise SetupError("Identity listing is incomplete")


def configure(client, wiki_url, owner_username, *, create_owner=False, owner_email=""):
    owners = [u for u in items(client, "/api/users") if u.get("username") == owner_username]
    if not owners and create_owner:
        if not owner_username or not owner_email:
            raise SetupError("Set the owner's username and email before creating the first account")
        owners = [request(client, "POST", "/api/users", json={
            "id": str(uuid.uuid4()), "username": owner_username, "email": owner_email,
            "firstName": owner_username, "displayName": owner_username,
            "isAdmin": True, "disabled": False, "userGroupIds": [],
        })]
    if len(owners) != 1 or owners[0].get("disabled") or not owners[0].get("isAdmin"):
        raise SetupError("Enroll your own active administrator account before running identity setup")
    closed = client.get("/api/signup/setup")
    if closed.status_code != 404 or closed.json().get("code") != "setup_not_available":
        raise SetupError("The initial public administrator setup has not closed")
    resource = wiki_url.rstrip("/") + "/mcp"
    found = [a for a in items(client, "/api/apis") if a["resource"] == resource]
    api = found[0] if found else request(client, "POST", "/api/apis", json={"name": "Wiki Hub", "resource": resource})
    detail = request(client, "GET", f"/api/apis/{api['id']}")
    permission = next((p for p in detail["permissions"] if p["key"] == "wiki:access"), None)
    if permission is None:
        permissions = [{"key": p["key"], "name": p["name"], "description": p.get("description")}
                       for p in detail["permissions"]]
        request(client, "PUT", f"/api/apis/{api['id']}/permissions", json={
            "permissions": [*permissions, {"key": "wiki:access", "name": "Use Wiki Hub"}]})
        detail = request(client, "GET", f"/api/apis/{api['id']}")
        permission = next(p for p in detail["permissions"] if p["key"] == "wiki:access")
    registered = {c["id"]: c for c in items(client, "/api/oidc/clients")}
    for identifier, name, callbacks in CLIENTS:
        previous = registered.get(identifier)
        if previous:
            if (not previous.get("isPublic") or not previous.get("pkceEnabled")
                    or previous.get("skipConsent") is not False
                    or set(previous.get("callbackURLs", [])) != set(callbacks)):
                raise SetupError("An existing wiki client's configuration differs; review it before changing it")
        else:
            request(client, "POST", "/api/oidc/clients", json={
                "id": identifier, "name": name, "isPublic": True, "pkceEnabled": True,
                "callbackURLs": callbacks, "isGroupRestricted": False, "skipConsent": False,
                "accessTokenDurationMinutes": 15, "refreshTokenDurationMinutes": 43200,
            })
        request(client, "PUT", f"/api/apis/{api['id']}/clients/{identifier}", json={
            "userDelegatedAccess": True, "clientAccess": False,
            "userDelegatedPermissionIds": [permission["id"]], "clientPermissionIds": [],
        })
    return {"owner_subject": owners[0]["id"], "client_id": "wiki-cli", "resource": resource,
            "scope": "openid offline_access wiki:access"}


def enroll(client, subject, issuer, destination):
    result = request(client, "POST", f"/api/users/{subject}/one-time-access-token", json={"ttl": 3600})
    token = result.get("token")
    if not isinstance(token, str) or not token.isascii() or not token.isalnum() or len(token) != 12:
        raise SetupError("The provider did not return a twelve-character enrollment token")
    if destination.is_symlink():
        raise SetupError("The enrollment file must not be a symlink")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(issuer.rstrip("/") + "/lc/" + token + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-owner", action="store_true", help="Create the named first admin if it does not exist")
    parser.add_argument("--enrolment-file", type=Path, help="Write a private one-hour enrollment URL")
    args = parser.parse_args()
    with httpx.Client(base_url=os.environ.get("POCKET_ID_INTERNAL_URL", "http://id:1411"),
                      headers={"X-API-KEY": os.environ["POCKET_ID_STATIC_API_KEY"]},
                      timeout=30, trust_env=False, follow_redirects=False) as client:
        result = configure(client, os.environ["WIKI_PUBLIC_URL"], os.environ["WIKI_OWNER_USERNAME"],
                           create_owner=args.create_owner, owner_email=os.environ.get("WIKI_OWNER_EMAIL", ""))
        if args.enrolment_file:
            enroll(client, result["owner_subject"], os.environ["OIDC_ISSUER"], args.enrolment_file)
            result["enrolment_url_written"] = True
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = str(error) if isinstance(error, SetupError) else "Setup failed; provider output withheld"
        raise SystemExit(message) from None
