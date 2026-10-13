"""Charm authentication of FastAPI requests, and role checks.

``require_roles("source:read")`` is a FastAPI dependency: it reads ``Authorization: Bearer <token>``, verifies the token
with Charm (:class:`gluonify.CharmVerifier`, built on first use from the environment) and checks the roles of the
``roles`` claim. Answers: 401 without a valid token (``WWW-Authenticate: Bearer``), 403 without a required role, 503
when Charm is not available to the application. Errors are ``{"error": "..."}``.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from gluonify import CharmVerifier, GluonifyServicesError, InvalidTokenError, Principal

__all__ = ["GluonifyAuth", "current_principal", "optional_principal", "require_roles"]

VerifierFactory = Callable[[], CharmVerifier]


class GluonifyAuth:
    """How the application authenticates requests.

    :param factory: builds the verifier on first use (default of :func:`gluonify_fastapi.create_app`:
        ``CharmVerifier.from_env``); a failure answers 503 and is retried at the next request
    :param dev_identity: an identity given to requests WITHOUT a token, for local development only;
        :func:`gluonify_fastapi.create_app` refuses it on the platform
    """

    def __init__(self, factory: VerifierFactory | None, *, dev_identity: Principal | None = None) -> None:
        self._factory = factory
        self._verifier: CharmVerifier | None = None
        self._lock = threading.Lock()
        self.dev_identity = dev_identity

    @property
    def sign_in_required(self) -> bool:
        """False when requests without a token are let in (development identity)."""
        return self.dev_identity is None

    def verifier(self) -> CharmVerifier:
        v = self._verifier
        if v is None:
            with self._lock:
                if self._verifier is None:
                    if self._factory is None:
                        raise GluonifyServicesError("no Charm verifier configured")
                    self._verifier = self._factory()
                v = self._verifier
        return v

    def authenticate(self, authorization: str | None) -> Principal:
        """Synchronous: the identity of a request from its ``Authorization`` header (may fetch Charm's keys)."""
        if not authorization or not authorization.strip():
            if self.dev_identity is not None:
                return self.dev_identity
            raise _unauthorized("missing bearer token")
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _unauthorized("missing bearer token")
        try:
            verifier = self.verifier()
        except GluonifyServicesError as e:
            raise HTTPException(503, f"authentication unavailable: {e}") from e
        try:
            return verifier.verify(token.strip())
        except InvalidTokenError as e:
            raise _unauthorized(str(e)) from e


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(401, message, headers={"WWW-Authenticate": "Bearer"})


def _auth(request: Request) -> GluonifyAuth:
    auth = getattr(request.app.state, "gluonify_auth", None)
    if not isinstance(auth, GluonifyAuth):
        raise HTTPException(500, "the application was not created by gluonify_fastapi.create_app")
    return auth


async def current_principal(request: Request) -> Principal:
    """Dependency: the authenticated identity (401 otherwise)."""
    principal: Principal = await run_in_threadpool(_auth(request).authenticate, request.headers.get("authorization"))
    return principal


async def optional_principal(request: Request) -> Principal | None:
    """Dependency: the identity when a valid token is given, ``None`` without a token (401 for an invalid one)."""
    header = request.headers.get("authorization")
    auth = _auth(request)
    if not header and auth.dev_identity is None:
        return None
    principal: Principal = await run_in_threadpool(auth.authenticate, header)
    return principal


def require_roles(*roles: str) -> Callable[[Request], Awaitable[Principal]]:
    """Dependency: the identity, which must hold every role given (403 otherwise)."""
    wanted = tuple(roles)

    async def dependency(request: Request) -> Principal:
        principal = await current_principal(request)
        missing = [r for r in wanted if not principal.has_role(r)]
        if missing:
            raise HTTPException(403, f"missing role: {', '.join(missing)}")
        return principal

    return dependency
