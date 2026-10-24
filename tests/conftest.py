import subprocess
from dataclasses import replace

import pytest

from wiki_hub.config import Settings
from wiki_hub.service import Service


class LocalForgejo:
    def __init__(self, directory):
        self.directory = directory

    def ensure_owner(self, _subject):
        return "owner"

    def create_repository(self, _owner, slug):
        path = self.directory / (slug + ".git")
        if not path.exists():
            subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(path)],
                           capture_output=True, check=True)
        return {"id": 1}

    def repository_url(self, _owner, slug, public=False):
        return str(self.directory / (slug + ".git"))

    def set_collaborator(self, owner, slug, subject, role):
        pass

    def revoke_copies(self, owner, slug, subject):
        pass

    def clone_credential(self, owner, slug, write=True, subject=None):
        return {"url": self.repository_url(owner, slug), "username": owner, "token": "synthetic-repo-only"}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        state_dir=tmp_path / "state", workspace_dir=tmp_path / "workspaces",
        public_url="https://wiki.example.test", oidc_issuer="https://id.example.test",
        oidc_audience="https://wiki.example.test/mcp", oidc_client_id="wiki-cli", owner_subject="owner",
        forgejo_url="https://git.example.test", forgejo_public_url="https://git.example.test",
        forgejo_username="operator", forgejo_password="synthetic-backend-password",
    )


@pytest.fixture
def service(settings, tmp_path):
    async def runner(work, _job, raw, reference):
        source = (work / reference).read_text()
        (work / "wiki/workshop.md").write_text(
            f"# Workshop\n\nThe workshop opens at 09:30.\n\nSource: [{raw}](../{raw})\n\n"
            f"Extracted reference: [{reference}](../{reference})\n\n{source}"
        )
        (work / "wiki/index.md").write_text("# Starter\n\n[Workshop](workshop.md)\n")

    return Service(replace(settings, upload_limit=1024 * 1024), LocalForgejo(tmp_path), runner=runner)
