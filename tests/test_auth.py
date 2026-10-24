"""Exercise real signatures and JWKS selection, with no live IdP dependency."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp.server.auth import AccessToken
from joserfc import jwk, jwt

from wiki_hub.auth import create_verifier, principal_subject

ISSUER = "https://id.example.test"
AUDIENCE = "https://wiki.example.test/mcp"
SUBJECT = "428e2444-d54b-4f59-b95e-b988b052da45"


@pytest.fixture(scope="module")
def signing_key():
    return jwk.RSAKey.generate_key(2048, parameters={"kid": "test-key"})


@pytest.fixture
def verifier(signing_key):
    result = create_verifier(
        SimpleNamespace(
            oidc_issuer=ISSUER,
            oidc_audience=AUDIENCE,
            oidc_jwks_url="http://identity:1411/.well-known/jwks.json",
        )
    )
    result._fetch_jwks = AsyncMock(
        return_value={"keys": [signing_key.as_dict(private=False)]}
    )
    return result


def signed_token(signing_key, changes=None, *, remove=(), algorithm="RS256", typ="at+jwt"):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "client_id": "wiki-cli",
        "scope": "openid wiki:access",
        "iat": now,
        "exp": now + 900,
    }
    claims.update(changes or {})
    for name in remove:
        claims.pop(name, None)
    return jwt.encode(
        {"alg": algorithm, "typ": typ, "kid": "test-key"},
        claims,
        signing_key,
        algorithms=[algorithm],
    )


def verify(verifier, token):
    return asyncio.run(verifier.verify_token(token))


def test_accepts_resource_access_token_and_retains_subject(verifier, signing_key):
    token = verify(verifier, signed_token(signing_key))
    assert token is not None
    assert principal_subject(token) == SUBJECT
    assert token.client_id == "wiki-cli"
    assert token.scopes == ["openid", "wiki:access"]


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.example.test"},
        {"aud": "wiki-cli"},
        {"exp": 1},
        {"sub": ""},
        {"sub": " padded "},
        {"scope": "openid"},
        {"iat": int(time.time()) + 3600},
        {"nbf": int(time.time()) + 3600},
        {"exp": True},
        {"iat": True},
    ],
)
def test_rejects_wrong_identity_resource_lifetime_or_scope(verifier, signing_key, changes):
    assert verify(verifier, signed_token(signing_key, changes)) is None


@pytest.mark.parametrize("missing", ["sub", "exp", "iat", "iss", "aud"])
def test_rejects_missing_required_claims(verifier, signing_key, missing):
    assert verify(verifier, signed_token(signing_key, remove=[missing])) is None


def test_rejects_an_id_token(verifier, signing_key):
    assert verify(verifier, signed_token(signing_key, typ="JWT")) is None


def test_rejects_wrong_algorithm(verifier, signing_key):
    assert verify(verifier, signed_token(signing_key, algorithm="RS512")) is None


def test_rejects_signature_from_a_different_key(verifier):
    other_key = jwk.RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    assert verify(verifier, signed_token(other_key)) is None


def test_rejects_malformed_token(verifier):
    assert verify(verifier, "not-a-jwt") is None


def test_subject_never_falls_back_to_client_id():
    token = AccessToken(token="unused", client_id="wiki-cli", scopes=[], claims={})
    with pytest.raises(ValueError, match="subject"):
        principal_subject(token)


def test_rejects_inconsistent_subject():
    token = AccessToken(
        token="unused",
        client_id="wiki-cli",
        subject="different-person",
        scopes=[],
        claims={"sub": SUBJECT},
    )
    with pytest.raises(ValueError, match="inconsistent"):
        principal_subject(token)


def test_default_jwks_location():
    verifier = create_verifier(
        SimpleNamespace(oidc_issuer=ISSUER, oidc_audience=AUDIENCE, oidc_jwks_url=None)
    )
    assert verifier.jwks_uri == ISSUER + "/.well-known/jwks.json"
