"""Verification of incoming Charm tokens (JWT, ES256) against Charm's JWKS.

Zero configuration on the platform (``"uses": ["charm"]``, inferred from the marker of this module):

* JWKS: ``GLUONIFY_SERVICE_CHARM_URL`` + ``/realms/gluonify/protocol/openid-connect/certs``;
* issuer: ``GLUONIFY_OIDC_ISSUER`` (the stable ``https://id.<zone>/realms/gluonify``, not the internal address);
* audience: ``GLUONIFY_OIDC_AUDIENCE`` (``gluonify-<app>``, injected by Higgs with the sign-in client of the application);
* roles: the ``roles`` claim.

Each value can be given explicitly (tests, special cases). Only ES256 is accepted; ``exp`` and ``sub`` are required.
The JWKS is kept 5 minutes and fetched again at once for an unknown ``kid`` (key rotation), at most every 30 seconds.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import jwt

from .services import GluonifyServices, GluonifyServicesError, defaults

__all__ = [
    "CERTS_PATH",
    "CharmVerifier",
    "InvalidTokenError",
    "Principal",
]

CERTS_PATH = "/realms/gluonify/protocol/openid-connect/certs"
ALGORITHMS = ("ES256",)

JwksFetcher = Callable[[str], Mapping[str, Any]]


class InvalidTokenError(Exception):
    """The token is refused (expired, wrong signature, audience or issuer...). The message never contains the token."""


@dataclass(frozen=True, slots=True)
class Principal:
    """The verified identity: subject, roles of the ``roles`` claim and all the claims."""

    subject: str
    roles: frozenset[str]
    claims: Mapping[str, Any] = field(repr=False)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_all(self, *roles: str) -> bool:
        return all(r in self.roles for r in roles)

    @property
    def organization(self) -> str | None:
        org = self.claims.get("org")
        return org if isinstance(org, str) else None


def _http_fetch(url: str) -> Mapping[str, Any]:
    if not url.startswith(("https://", "http://")):
        raise GluonifyServicesError(f"the JWKS address must be http(s): {url}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 (scheme checked above)
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        data: Mapping[str, Any] = json.loads(r.read().decode("utf-8"))
    return data


class CharmVerifier:
    """Verifies bearer tokens issued by Charm. Thread-safe.

    :param jwks_url: the JWKS address
    :param audience: the expected audience (``aud``); ``None`` disables the check (not advised)
    :param issuer: the expected issuer (``iss``); ``None`` disables the check
    :param fetch: reads the JWKS document (default: HTTP GET); injectable for tests
    :param clock: seconds since the epoch (default: ``time.time``)
    """

    def __init__(
        self,
        jwks_url: str,
        *,
        audience: str | None,
        issuer: str | None = None,
        fetch: JwksFetcher | None = None,
        clock: Callable[[], float] = time.time,
        ttl: float = 300.0,
        refetch_interval: float = 30.0,
        leeway: float = 30.0,
    ) -> None:
        self.jwks_url = jwks_url
        self.audience = audience
        self.issuer = issuer
        self._fetch = fetch or _http_fetch
        self._clock = clock
        self._ttl = ttl
        self._refetch = refetch_interval
        self._leeway = leeway
        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None

    @classmethod
    def from_env(
        cls,
        services: GluonifyServices | None = None,
        *,
        audience: str | None = None,
        issuer: str | None = None,
        fetch: JwksFetcher | None = None,
        clock: Callable[[], float] = time.time,
    ) -> CharmVerifier:
        """The verifier of the platform: Charm resolved through ``services``, issuer and audience from the environment.

        ``audience`` and ``issuer`` are defaults used when the platform does not inject them.

        :raises GluonifyServicesError: when Charm cannot be resolved
        """
        s = services or defaults()
        charm = s.resolve("charm")
        return cls(
            charm.resolve(CERTS_PATH),
            audience=s.env("GLUONIFY_OIDC_AUDIENCE") or audience,
            issuer=s.env("GLUONIFY_OIDC_ISSUER") or issuer,
            fetch=fetch,
            clock=clock,
        )

    # ------------------------------------------------------------------ keys

    def _load(self) -> None:
        doc = self._fetch(self.jwks_url)
        keys: dict[str, jwt.PyJWK] = {}
        for k in doc.get("keys", []) if isinstance(doc, Mapping) else []:
            if not isinstance(k, Mapping) or k.get("kty") != "EC" or k.get("use", "sig") != "sig":
                continue
            try:
                key = jwt.PyJWK(dict(k), algorithm="ES256")
            except jwt.PyJWTError:
                continue
            keys[str(k.get("kid", ""))] = key
        self._keys = keys
        self._fetched_at = self._clock()

    def _key(self, kid: str) -> jwt.PyJWK:
        now = self._clock()
        with self._lock:
            if self._fetched_at is None or now - self._fetched_at >= self._ttl:
                self._fetch_or_keep()
            key = self._keys.get(kid)
            if key is None and self._fetched_at is not None and now - self._fetched_at >= self._refetch:
                self._fetch_or_keep()
                key = self._keys.get(kid)
        if key is None:
            raise InvalidTokenError("unknown signing key")
        return key

    def _fetch_or_keep(self) -> None:
        try:
            self._load()
        except Exception as e:
            if not self._keys:
                raise InvalidTokenError(f"Charm's keys are not reachable ({type(e).__name__})") from e
            # keep the last keys read: a short Charm outage must not refuse every request

    # ------------------------------------------------------------------ verification

    def verify(self, token: str) -> Principal:
        """Verifies the token and returns its identity.

        :raises InvalidTokenError: if the token is refused
        """
        if not token or token.count(".") != 2:
            raise InvalidTokenError("not a JWT")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise InvalidTokenError("not a JWT") from e
        if header.get("alg") not in ALGORITHMS:
            raise InvalidTokenError("algorithm not allowed (ES256 only)")
        key = self._key(str(header.get("kid", "")))
        options: dict[str, Any] = {"require": ["exp", "sub"], "verify_aud": self.audience is not None}
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=list(ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self._leeway,
                options=cast(Any, options),
            )
        except jwt.ExpiredSignatureError as e:
            raise InvalidTokenError("token expired") from e
        except jwt.InvalidAudienceError as e:
            raise InvalidTokenError("token not issued for this application (audience)") from e
        except jwt.InvalidIssuerError as e:
            raise InvalidTokenError("token of another issuer") from e
        except jwt.PyJWTError as e:
            raise InvalidTokenError(f"token refused ({type(e).__name__})") from e
        roles = claims.get("roles", [])
        if isinstance(roles, str):
            roles = roles.split()
        if not isinstance(roles, list):
            roles = []
        return Principal(str(claims["sub"]), frozenset(r for r in roles if isinstance(r, str)), claims)
