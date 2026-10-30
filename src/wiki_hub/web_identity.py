"""Browser-only OIDC and minimal, read-only identity directory integration."""

import math
import time
from urllib.parse import quote, urlsplit

import httpx2 as httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet

from .auth import WIKI_SCOPE, identity_subject


class IdentityUnavailable(Exception):
    pass


def profile(subject, claims):
    def label(value, maximum=200):
        return value[:maximum] if isinstance(value, str) else ""
    return {"subject": subject,
            "name": label(claims.get("name") or claims.get("displayName") or claims.get("preferred_username")
                          or claims.get("username") or claims.get("firstName")) or "Unnamed person",
            "email": label(claims.get("email")),
            "username": label(claims.get("preferred_username") or claims.get("username"))}


class BrowserIdentity:
    def __init__(self, settings, verifier, transport=None):
        self.settings = settings
        self.verifier = verifier
        self.transport = transport
        self.cached_metadata = None
        self.metadata_until = 0
        self.cached_keys = None
        self.keys_until = 0

    async def get(self, url, **kwargs):
        async with httpx.AsyncClient(transport=self.transport, timeout=10, trust_env=False) as client:
            response = await client.get(url, **kwargs)
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                raise IdentityUnavailable("The identity response is too large.")
            return response.json()

    async def metadata(self):
        if self.cached_metadata and self.metadata_until > time.monotonic():
            return self.cached_metadata
        issuer = self.settings.oidc_issuer
        value = await self.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
        if value.get("issuer") != issuer:
            raise IdentityUnavailable("The identity provider's issuer does not match.")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            parsed = urlsplit(value.get(key, ""))
            local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
            if not parsed.hostname or parsed.username or parsed.password or not (
                    parsed.scheme == "https" or (local and parsed.scheme == "http")):
                raise IdentityUnavailable("The identity provider has an invalid endpoint.")
        self.cached_metadata, self.metadata_until = value, time.monotonic() + 300
        return value

    async def keys(self):
        if self.cached_keys is None or self.keys_until < time.monotonic():
            metadata = await self.metadata()
            value = await self.get(self.settings.oidc_jwks_url or metadata["jwks_uri"])
            self.cached_keys = KeySet.import_key_set(value)
            self.keys_until = time.monotonic() + 300
        return self.cached_keys

    async def logout_claims(self, token):
        decoded = jwt.decode(token, await self.keys(), algorithms=["RS256"])
        claims, now = decoded.claims, time.time()
        audience = claims.get("aud")
        if isinstance(audience, str):
            audience = [audience]
        issued = claims.get("iat")
        if (decoded.header.get("typ") not in ("logout+jwt", "JWT")
                or claims.get("iss") != self.settings.oidc_issuer
                or not isinstance(audience, list) or self.settings.web_client_id not in audience
                or type(issued) not in (int, float) or not math.isfinite(issued)
                or not now - 300 <= issued <= now + 30
                or "nonce" in claims
                or claims.get("events") != {"http://schemas.openid.net/event/backchannel-logout": {}}
                or not isinstance(claims.get("jti"), str) or not 1 <= len(claims["jti"]) <= 200):
            raise ValueError("Invalid logout token")
        for name in ("sub", "sid"):
            if name in claims and (not isinstance(claims[name], str) or not 1 <= len(claims[name]) <= 200):
                raise ValueError("Invalid logout identity")
        if not claims.get("sub") and not claims.get("sid"):
            raise ValueError("A logout identity is required")
        for name in ("exp", "nbf"):
            if name in claims and (type(claims[name]) not in (int, float) or not math.isfinite(claims[name])):
                raise ValueError("Invalid logout time")
        if claims.get("exp", now + 1) <= now or claims.get("nbf", now) > now + 30:
            raise ValueError("Expired or premature logout token")
        return claims

    def client(self, token=None):
        return AsyncOAuth2Client(
            client_id=self.settings.web_client_id, client_secret=self.settings.web_client_secret or None,
            token_endpoint_auth_method="client_secret_basic" if self.settings.web_client_secret else "none",
            redirect_uri=self.settings.public_url + "/auth/callback",
            scope="openid profile email offline_access " + WIKI_SCOPE,
            code_challenge_method="S256", token=token, transport=self.transport, timeout=10, trust_env=False,
        )

    async def authorize(self, transaction):
        metadata = await self.metadata()
        async with self.client() as client:
            url, _ = client.create_authorization_url(
                metadata["authorization_endpoint"], state=transaction["state"], nonce=transaction["nonce"],
                code_verifier=transaction["verifier"], resource=self.settings.oidc_audience,
            )
        return url

    async def verify_access(self, token, expected_subject=None):
        access = await self.verifier.verify_token(token["access_token"])
        if access is None or access.client_id != self.settings.web_client_id:
            raise ValueError("Invalid browser access token")
        subject = identity_subject(access)
        if subject.startswith(("agent:", "client-")) or (expected_subject and subject != expected_subject):
            raise ValueError("Invalid browser identity")
        # Signed access-token expiry is authoritative, including providers that
        # omit expires_in from the token response.
        token["expires_at"] = access.claims["exp"]
        return subject

    async def exchange(self, code, transaction):
        metadata = await self.metadata()
        async with self.client() as client:
            token = dict(await client.fetch_token(
                metadata["token_endpoint"], code=code, code_verifier=transaction["verifier"],
                resource=self.settings.oidc_audience,
            ))
        decoded = jwt.decode(token["id_token"], await self.keys(), algorithms=["RS256"])
        if decoded.header.get("typ", "JWT") != "JWT":
            raise ValueError("An access token cannot be used as an identity token")
        claims = CodeIDToken(decoded.claims, decoded.header, options={
            "iss": {"essential": True, "value": self.settings.oidc_issuer},
            "aud": {"essential": True, "value": self.settings.web_client_id},
        }, params={"nonce": transaction["nonce"], "client_id": self.settings.web_client_id,
                   "access_token": token["access_token"]})
        claims.validate(leeway=30)
        for name in ("exp", "iat"):
            if type(claims[name]) not in (int, float) or not math.isfinite(claims[name]):
                raise ValueError("Invalid identity token time")
        if claims["iat"] >= claims["exp"]:
            raise ValueError("Invalid identity token lifetime")
        subject = await self.verify_access(token)
        if claims["sub"] != subject:
            raise ValueError("Identity and access token subjects differ")
        sid = claims.get("sid", "")
        if not isinstance(sid, str) or len(sid) > 200:
            raise ValueError("Invalid provider session")
        token.update(_sid=sid, _issued_at=claims["iat"])
        return token, profile(subject, claims)

    async def refresh(self, token, subject):
        metadata = await self.metadata()
        async with self.client(token) as client:
            refreshed = dict(await client.refresh_token(
                metadata["token_endpoint"], refresh_token=token["refresh_token"], resource=self.settings.oidc_audience))
        refreshed.setdefault("refresh_token", token["refresh_token"])
        for name in ("_sid", "_issued_at"):
            if name in token:
                refreshed[name] = token[name]
        await self.verify_access(refreshed, subject)
        return refreshed


class PocketDirectory:
    """Never return upstream user objects or use a bootstrap administrator key."""

    def __init__(self, settings, identity):
        self.settings, self.identity = settings, identity

    @property
    def enabled(self):
        return bool(self.settings.directory_url and self.settings.directory_api_key)

    async def get(self, path, **kwargs):
        return await self.identity.get(self.settings.directory_url + "/api/" + path,
                                       headers={"X-API-KEY": self.settings.directory_api_key}, **kwargs)

    async def admission(self):
        client = await self.get("oidc/clients/" + quote(self.settings.web_client_id, safe=""))
        if type(client.get("isGroupRestricted")) is not bool:
            raise IdentityUnavailable("The directory's application admission could not be read.")
        if not client["isGroupRestricted"]:
            return None
        return {group["id"] for group in client["allowedUserGroups"]}

    @staticmethod
    def admitted(user, groups):
        return (user.get("disabled") is False and (groups is None or bool(
            groups & {group["id"] for group in user.get("userGroups", [])})))

    async def search(self, query):
        groups = await self.admission()
        result = await self.get("users", params={"search": query, "pagination[limit]": 20, "pagination[page]": 1})
        rows = result if isinstance(result, list) else result["data"]
        return [profile(user["id"], user) for user in rows[:20] if self.admitted(user, groups)]

    async def display(self, subject):
        user = await self.get("users/" + quote(subject, safe=""))
        if user.get("id") != subject:
            raise ValueError("Unexpected directory identity")
        return profile(subject, user)

    async def resolve(self, subject):
        groups = await self.admission()
        try:
            user = await self.get("users/" + quote(subject, safe=""))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError("Choose an active person admitted to Wiki Hub.") from None
            raise
        if user.get("id") != subject or not self.admitted(user, groups):
            raise ValueError("Choose an active person admitted to Wiki Hub.")
        return profile(subject, user)
