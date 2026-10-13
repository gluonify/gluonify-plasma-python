"""Shared fixtures: an EC P-256 key pair standing in for Charm's signing key, and a token factory."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec


class FakeCharm:
    """Signs tokens like Charm (ES256, kid) and serves its JWKS; counts the JWKS reads."""

    def __init__(self, kid: str = "k1") -> None:
        self.keys: dict[str, ec.EllipticCurvePrivateKey] = {}
        self.fetches = 0
        self.down = False
        self.add_key(kid)
        self.kid = kid

    def add_key(self, kid: str) -> None:
        self.keys[kid] = ec.generate_private_key(ec.SECP256R1())

    def jwks(self, url: str) -> dict[str, Any]:
        self.fetches += 1
        self.last_url = url
        if self.down:
            raise OSError("Charm is down")
        out = []
        for kid, k in self.keys.items():
            jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(k.public_key()))
            jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
            out.append(jwk)
        return {"keys": out}

    def token(
        self,
        sub: str = "ada",
        roles: list[str] | str | None = None,
        aud: str | list[str] = "gluonify-source",
        iss: str = "https://id.gluonify.lan/realms/gluonify",
        ttl: int = 300,
        kid: str | None = None,
        **extra: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {"sub": sub, "aud": aud, "iss": iss, "iat": now, "exp": now + ttl}
        if roles is not None:
            claims["roles"] = roles
        claims.update(extra)
        k = kid or self.kid
        return jwt.encode(claims, self.keys[k], algorithm="ES256", headers={"kid": k})


@pytest.fixture
def charm() -> FakeCharm:
    return FakeCharm()


@pytest.fixture
def clock() -> Callable[[], float]:
    class Clock:
        now = 1000.0

        def __call__(self) -> float:
            return self.now

    return Clock()
