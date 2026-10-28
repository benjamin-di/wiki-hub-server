"""Runtime configuration; deployment values stay outside application source."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    state_dir: Path = field(default_factory=lambda: Path("state"))
    workspace_dir: Path = field(default_factory=lambda: Path("workspaces"))
    public_url: str = "http://localhost:8000"
    oidc_issuer: str = ""
    oidc_jwks_url: str = ""
    oidc_audience: str = ""
    oidc_client_id: str = ""
    owner_subject: str = ""
    forgejo_url: str = ""
    forgejo_public_url: str = ""
    forgejo_username: str = ""
    forgejo_password: str = field(default="", repr=False)
    ingest_url: str = ""
    ingest_tier: str = "daily"
    upload_limit: int = 32 * 1024 * 1024
    max_pending_jobs: int = 32

    @classmethod
    def from_env(cls):
        public = os.environ["WIKI_PUBLIC_URL"].rstrip("/")
        return cls(
            state_dir=Path(os.environ.get("WIKI_STATE_DIR", "/data")),
            workspace_dir=Path(os.environ.get("WIKI_WORKSPACE_DIR", "/workspaces")),
            public_url=public,
            oidc_issuer=os.environ["OIDC_ISSUER"].rstrip("/"),
            oidc_jwks_url=os.environ.get("OIDC_JWKS_URL", ""),
            oidc_audience=public + "/mcp",
            oidc_client_id=os.environ["WIKI_OIDC_CLIENT_ID"],
            owner_subject=os.environ.get("WIKI_OWNER_SUBJECT", ""),
            forgejo_url=os.environ["FORGEJO_URL"].rstrip("/"),
            forgejo_public_url=os.environ["FORGEJO_PUBLIC_URL"].rstrip("/"),
            forgejo_username=os.environ["FORGEJO_USERNAME"],
            forgejo_password=os.environ["FORGEJO_PASSWORD"],
            ingest_url=os.environ["WIKI_INGEST_URL"].rstrip("/"),
            ingest_tier=os.environ.get("WIKI_INGEST_TIER", "daily"),
            upload_limit=int(os.environ.get("WIKI_UPLOAD_LIMIT", 32 * 1024 * 1024)),
            max_pending_jobs=int(os.environ.get("WIKI_MAX_PENDING_JOBS", 32)),
        )
