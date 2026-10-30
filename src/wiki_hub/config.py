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
    # Optional trusted MCP gateway. Never a human OAuth or local agent credential.
    gateway_bearer_tokens: str = field(default="", repr=False)
    gateway_allowed_hosts: tuple[str, ...] = ()
    owner_subject: str = ""
    forgejo_url: str = ""
    forgejo_public_url: str = ""
    forgejo_username: str = ""
    forgejo_password: str = field(default="", repr=False)
    ingest_url: str = ""
    ingest_tier: str = "daily"
    upload_limit: int = 32 * 1024 * 1024
    max_pending_jobs: int = 32
    # Optional read-only network drive. All three locations enable it together.
    drive_dir: Path | None = None
    drive_control_dir: Path | None = None
    drive_server: str = ""
    drive_share: str = "wikis"
    drive_domain: str = ""
    # Optional writable intake share; offered only when the file server serves it.
    drive_inbox_share: str = ""
    # The agent's report; deployments should keep it outside the app-writable directory.
    drive_observed_file: Path | None = None
    drive_refresh_seconds: int = 60
    drive_confirm_seconds: int = 20
    # Saved pages publish once they have been unchanged this long.
    drive_edit_quiet_seconds: int = 20
    drive_edit_scan_seconds: int = 5

    @property
    def drive_enabled(self):
        return bool(self.drive_dir and self.drive_control_dir and self.drive_server)

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
            gateway_bearer_tokens=os.environ.get("WIKI_GATEWAY_BEARER_TOKENS", ""),
            gateway_allowed_hosts=tuple(host.strip() for host in
                                        os.environ.get("WIKI_GATEWAY_ALLOWED_HOSTS", "").split(",") if host.strip()),
            owner_subject=os.environ.get("WIKI_OWNER_SUBJECT", ""),
            forgejo_url=os.environ["FORGEJO_URL"].rstrip("/"),
            forgejo_public_url=os.environ["FORGEJO_PUBLIC_URL"].rstrip("/"),
            forgejo_username=os.environ["FORGEJO_USERNAME"],
            forgejo_password=os.environ["FORGEJO_PASSWORD"],
            ingest_url=os.environ["WIKI_INGEST_URL"].rstrip("/"),
            ingest_tier=os.environ.get("WIKI_INGEST_TIER", "daily"),
            upload_limit=int(os.environ.get("WIKI_UPLOAD_LIMIT", 32 * 1024 * 1024)),
            max_pending_jobs=int(os.environ.get("WIKI_MAX_PENDING_JOBS", 32)),
            drive_dir=Path(os.environ["WIKI_DRIVE_DIR"]) if os.environ.get("WIKI_DRIVE_DIR") else None,
            drive_control_dir=(Path(os.environ["WIKI_DRIVE_CONTROL_DIR"])
                               if os.environ.get("WIKI_DRIVE_CONTROL_DIR") else None),
            drive_server=os.environ.get("WIKI_DRIVE_SERVER", ""),
            drive_share=os.environ.get("WIKI_DRIVE_SHARE", "wikis"),
            drive_domain=os.environ.get("WIKI_DRIVE_DOMAIN", ""),
            drive_inbox_share=os.environ.get("WIKI_DRIVE_INBOX_SHARE", ""),
            drive_observed_file=(Path(os.environ["WIKI_DRIVE_OBSERVED_FILE"])
                                 if os.environ.get("WIKI_DRIVE_OBSERVED_FILE") else None),
            drive_refresh_seconds=int(os.environ.get("WIKI_DRIVE_REFRESH_SECONDS", 60)),
            drive_confirm_seconds=int(os.environ.get("WIKI_DRIVE_CONFIRM_SECONDS", 20)),
            drive_edit_quiet_seconds=int(os.environ.get("WIKI_DRIVE_EDIT_QUIET_SECONDS", 20)),
            drive_edit_scan_seconds=int(os.environ.get("WIKI_DRIVE_EDIT_SCAN_SECONDS", 5)),
        )
