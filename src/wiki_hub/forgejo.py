"""Small Forgejo adapter. Server credentials never leave this process."""

import secrets
import uuid
from urllib.parse import quote

import httpx


class ForgejoError(Exception):
    pass


class Forgejo:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.forgejo_url + "/api/v1",
            auth=(settings.forgejo_username, settings.forgejo_password),
            timeout=30,
            trust_env=False,
        )

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if not response.is_success:
            raise ForgejoError(f"Forgejo request failed (HTTP {response.status_code}).")
        return response.json() if response.content else None

    def ensure_owner(self, subject):
        # This is a backend account, never an alternative human sign-in.
        username = self.username(subject)
        response = self.client.get("/users/" + username)
        if response.status_code == 404:
            self.request("POST", "/admin/users", json={
                "username": username, "email": username + "@accounts.example.invalid",
                "password": secrets.token_urlsafe(48), "must_change_password": False,
                "send_notify": False, "visibility": "private",
            })
        elif not response.is_success:
            raise ForgejoError(f"Cannot check repository owner (HTTP {response.status_code}).")
        return username

    @staticmethod
    def username(subject):
        return "wiki-" + uuid.uuid5(uuid.NAMESPACE_URL, subject).hex[:24]

    def create_repository(self, owner, slug):
        response = self.client.get(f"/repos/{owner}/{slug}")
        if response.status_code == 404:
            return self.request("POST", f"/admin/users/{owner}/repos", json={
                "name": slug, "private": True, "auto_init": False,
                "default_branch": "main", "description": "Managed by wiki-hub",
            })
        if not response.is_success:
            raise ForgejoError(f"Cannot check repository (HTTP {response.status_code}).")
        repo = response.json()
        if not repo.get("private") or repo.get("description") != "Managed by wiki-hub":
            raise ForgejoError("A repository with this name already exists outside wiki-hub.")
        return repo

    def repository_url(self, owner, slug, public=False):
        base = self.settings.forgejo_public_url if public else self.settings.forgejo_url
        return f"{base}/{quote(owner)}/{quote(slug)}.git"

    def set_collaborator(self, owner, slug, subject, role):
        username = self.username(subject) if role == "none" else self.ensure_owner(subject)
        path = f"/repos/{owner}/{slug}/collaborators/{username}"
        if role == "none":
            response = self.client.delete(path)
            if response.status_code not in (204, 404):
                raise ForgejoError(f"Cannot remove Git access (HTTP {response.status_code}). Retry unshare.")
        else:
            self.request("PUT", path, json={"permission": "write" if role in ("write", "admin") else "read"})

    def revoke_copies(self, owner, slug, subject):
        username = self.username(subject)
        path = f"/admin/users/{username}/tokens"
        matched = []
        # Gather before deleting so pagination cannot skip tokens as rows disappear.
        for page in range(1, 1001):
            response = self.client.get(path, params={"page": page, "limit": 100})
            if response.status_code == 404:
                return
            if not response.is_success:
                raise ForgejoError("Cannot check existing Git copies; retry the permission change.")
            tokens = response.json()
            if not isinstance(tokens, list):
                raise ForgejoError("Unexpected Git copy listing; retry the permission change.")
            if not tokens:
                break
            for token in tokens:
                if not token.get("name", "").startswith("wiki-copy-"):
                    continue
                repositories = token.get("repositories")
                if not repositories or any(r.get("owner") == owner and r.get("name") == slug for r in repositories):
                    if type(token.get("id")) is not int or token["id"] <= 0:
                        raise ForgejoError("Unexpected Git copy identifier; retry the permission change.")
                    matched.append(token["id"])
        else:
            raise ForgejoError("Git copy listing is incomplete; no revocation was reported successful.")
        for identifier in matched:
            response = self.client.delete(path + "/" + str(identifier))
            if response.status_code not in (204, 404):
                raise ForgejoError("Cannot revoke an existing Git copy; retry the permission change.")

    def clone_credential(self, owner, slug, write=True, subject=None):
        username = self.ensure_owner(subject) if subject else owner
        token = self.request("POST", f"/admin/users/{username}/tokens", json={
            "name": "wiki-copy-" + uuid.uuid4().hex[:16],
            "scopes": ["write:repository" if write else "read:repository"],
            "repositories": [{"owner": owner, "name": slug}],
        })
        return {"url": self.repository_url(owner, slug, public=True), "username": username, "token": token["sha1"]}
