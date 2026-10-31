"""Pocket ID access-token validation shared by the REST API and MCP server."""

from __future__ import annotations

import math
import secrets
import time
from typing import Protocol

from fastmcp.server.auth import AccessToken, JWTVerifier, TokenVerifier
from fastmcp.server.dependencies import get_http_request
from fastmcp.utilities.auth import decode_jwt_header

WIKI_SCOPE = "wiki:access"


def valid_client_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 512 and value == value.strip()
            and value.isprintable() and "," not in value)


class GatewayAccessToken(AccessToken):
    """The client is asserted by the authenticated gateway, not a public header."""


class GatewayVerifier(TokenVerifier):
    """Trust one explicitly configured gateway, only on its separate MCP transport.

    The gateway authenticates a human and overwrites X-Wiki-Subject from their
    verified immutable subject. Its backend credential authenticates that assertion;
    neither a caller-supplied header alone nor a person's OAuth token suffices.
    Wiki service operations still resolve current grants and suspension themselves.
    """

    def __init__(self, tokens: str):
        super().__init__(required_scopes=[WIKI_SCOPE])
        self.tokens = tuple(value.strip().encode() for value in tokens.split(","))
        if any(len(value) < 32 for value in self.tokens):
            raise ValueError("Gateway credentials must each contain at least 32 characters")

    async def verify_token(self, token):
        # Evaluate every configured key, including during rotation.
        if not any([secrets.compare_digest(token.encode(), key) for key in self.tokens]):
            return None
        subjects = get_http_request().headers.getlist("x-wiki-subject")
        if len(subjects) != 1:
            return None
        subject = subjects[0]
        if (not subject or len(subject) > 255 or subject != subject.strip()
                or not subject.isprintable() or "," in subject
                or subject.startswith(("agent:", "client-"))):
            return None
        clients = get_http_request().headers.getlist("x-wiki-client")
        if len(clients) > 1 or (clients and not valid_client_id(clients[0])):
            return None
        return GatewayAccessToken(token=token, client_id=clients[0] if clients else "trusted-gateway",
                                  subject=subject, scopes=[WIKI_SCOPE], claims={"sub": subject})


class WorkloadAccessToken(AccessToken):
    """Keep the signed identity intact and carry its separately resolved Wiki actor."""

    wiki_subject: str


class OIDCSettings(Protocol):
    oidc_issuer: str
    oidc_jwks_url: str | None
    oidc_audience: str


def identity_subject(access_token: AccessToken) -> str:
    """Read the signed subject without substituting a client ID or local agent."""
    subject = access_token.claims.get("sub")
    if not isinstance(subject, str) or not subject or subject != subject.strip():
        raise ValueError("The access token has no valid subject")
    if access_token.subject is not None and access_token.subject != subject:
        raise ValueError("The access token subject is inconsistent")
    return subject


def principal_subject(access_token: AccessToken) -> str:
    """Resolve an authenticated actor; current local permissions still apply."""
    subject = identity_subject(access_token)
    return access_token.wiki_subject if isinstance(access_token, WorkloadAccessToken) else subject


def _numeric_date(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


class PocketIDVerifier(JWTVerifier):
    """Apply Pocket ID's RFC 9068 contract on top of FastMCP's verifier."""

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            # ID tokens use a different type. They must never authorize API calls.
            if decode_jwt_header(token).get("typ") != "at+jwt":
                return None
        except (ValueError, TypeError, KeyError, IndexError):
            return None

        access_token = await super().load_access_token(token)
        if access_token is None:
            return None

        claims = access_token.claims
        now = time.time()
        expiration = claims.get("exp")
        issued_at = claims.get("iat")
        not_before = claims.get("nbf")
        if not _numeric_date(expiration) or not _numeric_date(issued_at):
            return None
        if expiration <= now or issued_at > now + 30 or issued_at >= expiration:
            return None
        if not_before is not None and (
            not _numeric_date(not_before) or not_before > now + 30
        ):
            return None
        try:
            identity_subject(access_token)
        except ValueError:
            return None
        return access_token


def create_verifier(settings: OIDCSettings) -> PocketIDVerifier:
    """Create one verifier for both API routes and MCP's RemoteAuthProvider.

    The JWKS location is operator configuration. An internal container URL is
    allowed; token-supplied URLs never control which keys this verifier trusts.
    """
    return PocketIDVerifier(
        jwks_uri=settings.oidc_jwks_url
        or settings.oidc_issuer.rstrip("/") + "/.well-known/jwks.json",
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        algorithm="RS256",
        required_scopes=[WIKI_SCOPE],
    )


class PrincipalVerifier(TokenVerifier):
    """Human OAuth, bound Pocket workloads and independently revocable local keys."""

    def __init__(self, oidc, state):
        super().__init__(required_scopes=[WIKI_SCOPE])
        self.oidc = oidc
        self.state = state

    async def verify_token(self, token):
        if token.startswith("whagent_"):
            identifier = self.state.token_agent(token)
            if identifier is None:
                return None
            return AccessToken(token=token, client_id="wiki-agent", subject=identifier,
                               scopes=[WIKI_SCOPE], claims={"sub": identifier})
        access = await self.oidc.verify_token(token)
        if access is None:
            return None
        subject = identity_subject(access)
        claims = access.claims
        # Pocket ID uses client-CLIENT_ID only for the client-credentials grant.
        # An unregistered workload must never become a human with direct grants.
        if (subject.startswith("client-")
                or self.state.is_workload_client(claims.get("iss"), access.client_id)):
            client = claims.get("client_id")
            issued, expires = claims.get("iat"), claims.get("exp")
            if (not isinstance(client, str) or not client or access.client_id != client
                    or subject != "client-" + client or set(access.scopes) != {WIKI_SCOPE}
                    or not _numeric_date(issued) or not _numeric_date(expires)
                    or expires - issued > 900
                    or any(name in claims for name in ("groups", "person_id", "email", "email_verified"))):
                return None
            agent = self.state.workload_agent(claims.get("iss"), client, subject)
            if agent is None:
                return None
            return WorkloadAccessToken(**access.model_dump(), wiki_subject=agent)
        # Reserve the local agent namespace for credentials issued by this server.
        if subject.startswith("agent:"):
            return None
        return access
