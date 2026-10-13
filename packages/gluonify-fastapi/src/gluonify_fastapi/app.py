"""The app factory: a FastAPI application wired to the platform with no configuration."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from gluonify import CharmVerifier, GluonifyServices, Principal, Top, defaults

from .auth import GluonifyAuth
from .health import Health

__all__ = ["DEFAULT_PORT", "PORT_ENV", "create_app", "serve"]

PORT_ENV = "PORT"
DEFAULT_PORT = 8080


def create_app(
    title: str,
    *,
    audience: str,
    version: str = "0.1.0",
    description: str | None = None,
    services: GluonifyServices | None = None,
    auth: GluonifyAuth | None = None,
    dev_identity: Principal | None = None,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """A FastAPI application for Gluonify.

    * ``app.state.gluonify``: the :class:`gluonify.GluonifyServices` of the application; ``app.state.top``: its
      :class:`gluonify.Top`; ``app.state.health``: its :class:`Health` (add readiness checks to it);
      ``app.state.gluonify_auth``: its :class:`GluonifyAuth` (Charm, built on first use);
    * health at ``/q/health/live`` and ``/q/health/ready``, OpenAPI at ``/q/openapi``, Swagger UI at ``/q/swagger-ui``;
    * errors as ``{"error": "..."}``; an invalid body is a 400 with the list of ``violations``.

    :param audience: the audience expected in Charm tokens when the platform does not inject ``GLUONIFY_OIDC_AUDIENCE``
    :param dev_identity: an identity for requests without a token, local development only (refused on the platform)
    """
    s = services or defaults()
    if dev_identity is not None and s.on_platform:
        raise RuntimeError("a development identity is refused on the platform (GLUONIFY_ENVIRONMENT is set)")
    if auth is None:
        auth = GluonifyAuth(lambda: CharmVerifier.from_env(s, audience=audience), dev_identity=dev_identity)
    fastapi_kwargs.setdefault("docs_url", "/q/swagger-ui")
    fastapi_kwargs.setdefault("openapi_url", "/q/openapi")
    fastapi_kwargs.setdefault("redoc_url", None)
    app = FastAPI(title=title, version=version, description=description or "", **fastapi_kwargs)
    health = Health()
    app.state.gluonify = s
    app.state.top = Top(s.environ)
    app.state.health = health
    app.state.gluonify_auth = auth
    app.include_router(health.router())

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, e: HTTPException) -> JSONResponse:
        return JSONResponse({"error": str(e.detail)}, status_code=e.status_code, headers=e.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, e: RequestValidationError) -> JSONResponse:
        violations = [
            {"field": ".".join(str(p) for p in err.get("loc", ())[1:]), "message": err.get("msg", "")} for err in e.errors()
        ]
        return JSONResponse({"error": "invalid body", "violations": violations}, status_code=400)

    return app


def serve(app: FastAPI, *, port: int | None = None) -> None:  # pragma: no cover - starts a server
    """Runs the application with uvicorn, one process (the platform runs one per instance), on ``0.0.0.0:$PORT``."""
    import uvicorn

    p = port if port is not None else int(os.environ.get(PORT_ENV, str(DEFAULT_PORT)))
    uvicorn.run(app, host="0.0.0.0", port=p, proxy_headers=True, forwarded_allow_ips="*")  # noqa: S104 (rule R-ECOUTE)
