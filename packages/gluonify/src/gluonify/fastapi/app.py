"""The app factory: a FastAPI application wired to the platform with no configuration."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException

from gluonify import CharmVerifier, GluonifyServices, Principal, Top, defaults

from .auth import GluonifyAuth
from .health import Health
from .mcp import McpRegistry, default_registry, mcp_router

__all__ = ["DEFAULT_PORT", "DOCS_URL", "OPENAPI_URL", "PORT_ENV", "READ_ONLY_MESSAGE", "create_app", "serve"]

PORT_ENV = "PORT"
DEFAULT_PORT = 8080
OPENAPI_URL = "/openapi.json"
DOCS_URL = "/docs"
READ_ONLY_MESSAGE = "Swagger UI is read-only in production"

# Same rule as the Quarkus services in production (quarkus.swagger-ui.request-interceptor and on-complete): every request but
# the contract is refused, and the Authorize and Try it out controls are hidden.
_READ_ONLY_SCRIPT = (
    "<script>(function(){var o=window.SwaggerUIBundle;var w=function(c){"
    "c.requestInterceptor=function(req){if(req.loadSpec){return req;}throw new Error('" + READ_ONLY_MESSAGE + "');};"
    "c.tryItOutEnabled=false;c.persistAuthorization=false;c.onComplete=function(){var s=document.createElement('style');"
    "s.textContent='.auth-wrapper,.authorization__btn,.try-out,.btn.execute{display:none !important}';"
    "document.head.appendChild(s);};return o(c);};Object.assign(w,o);window.SwaggerUIBundle=w;})();</script>"
)


def _read_only_docs(app: FastAPI, docs_url: str, openapi_url: str) -> None:
    """Swagger UI at ``docs_url``, read-only: the page of FastAPI with the interceptor added after the bundle."""

    @app.get(docs_url, include_in_schema=False)
    async def docs() -> HTMLResponse:
        page = get_swagger_ui_html(openapi_url=openapi_url, title=f"{app.title} - Swagger UI", oauth2_redirect_url=None)
        html = bytes(page.body).decode()
        marker = 'swagger-ui-bundle.js"></script>'
        if marker not in html:  # pragma: no cover - a FastAPI that changed its page: refuse rather than serve it writable
            raise RuntimeError("cannot make Swagger UI read-only: unexpected FastAPI page")
        return HTMLResponse(html.replace(marker, marker + _READ_ONLY_SCRIPT, 1))


def create_app(
    title: str,
    *,
    audience: str,
    version: str = "0.1.0",
    description: str | None = None,
    services: GluonifyServices | None = None,
    auth: GluonifyAuth | None = None,
    dev_identity: Principal | None = None,
    swagger_read_only: bool | None = None,
    mcp: McpRegistry | bool | None = None,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """A FastAPI application for Gluonify.

    * ``app.state.gluonify``: the :class:`gluonify.GluonifyServices` of the application; ``app.state.top``: its
      :class:`gluonify.Top`; ``app.state.health``: its :class:`Health` (add readiness checks to it);
      ``app.state.gluonify_auth``: its :class:`GluonifyAuth` (Charm, built on first use);
    * health at ``/health/live`` and ``/health/ready``, OpenAPI at ``/openapi.json``, Swagger UI at ``/docs`` (FastAPI's
      conventional paths; ``/q/`` belongs to Quarkus), no ReDoc; Swagger UI is read-only on the platform (no Authorize, no
      Try it out, every request but the contract refused), like the Quarkus services in production;
    * errors as ``{"error": "..."}``; an invalid body is a 400 with the list of ``violations``.

    :param audience: the audience expected in Charm tokens when the platform does not inject ``GLUONIFY_OIDC_AUDIENCE``
    :param dev_identity: an identity for requests without a token, local development only (refused on the platform)
    :param swagger_read_only: read-only Swagger UI; ``None`` (default): on the platform only
    :param mcp: serve the MCP tools of the application at ``POST /mcp`` (:mod:`gluonify.fastapi.mcp`): ``True`` for the tools
        declared with :func:`gluonify.fastapi.mcp_tool`, or a :class:`McpRegistry`; ``None``/``False``: no ``/mcp``
    """
    s = services or defaults()
    if dev_identity is not None and s.on_platform:
        raise RuntimeError("a development identity is refused on the platform (GLUONIFY_ENVIRONMENT is set)")
    if auth is None:
        auth = GluonifyAuth(lambda: CharmVerifier.from_env(s, audience=audience), dev_identity=dev_identity)
    fastapi_kwargs.setdefault("openapi_url", OPENAPI_URL)
    fastapi_kwargs.setdefault("redoc_url", None)
    docs_url = fastapi_kwargs.pop("docs_url", DOCS_URL)
    read_only = s.on_platform if swagger_read_only is None else swagger_read_only
    openapi_url = fastapi_kwargs["openapi_url"]
    fastapi_kwargs["docs_url"] = None if read_only else docs_url
    if read_only:
        fastapi_kwargs["swagger_ui_oauth2_redirect_url"] = None
    app = FastAPI(title=title, version=version, description=description or "", **fastapi_kwargs)
    if read_only and docs_url and openapi_url:
        _read_only_docs(app, docs_url, openapi_url)
    health = Health()
    app.state.gluonify = s
    app.state.top = Top(s.environ)
    app.state.health = health
    app.state.gluonify_auth = auth
    app.include_router(health.router())
    if mcp:
        app.include_router(mcp_router(default_registry if mcp is True else mcp))

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
