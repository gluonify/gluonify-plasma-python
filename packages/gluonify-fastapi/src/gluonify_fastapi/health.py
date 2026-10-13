"""Health endpoints, at the same addresses as a Quarkus application: ``/q/health/live`` and ``/q/health/ready``.

The platform sends traffic only to an instance whose readiness answers 200. Liveness must not depend on anything
external (a gDown outage must not make the application restart in a loop). The body follows MicroProfile Health:
``{"status": "UP", "checks": [{"name": "...", "status": "UP", "data": {...}}]}``; 503 when a check is DOWN.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

__all__ = ["Check", "Health", "Result"]

Result = bool | tuple[bool, Mapping[str, Any]]
Check = Callable[[], Result]


@dataclass(frozen=True, slots=True)
class _Named:
    name: str
    check: Check


class Health:
    """The checks of the application; :meth:`router` serves them."""

    def __init__(self) -> None:
        self._live: list[_Named] = []
        self._ready: list[_Named] = []

    def liveness(self, name: str, check: Check) -> None:
        self._live.append(_Named(name, check))

    def readiness(self, name: str, check: Check) -> None:
        self._ready.append(_Named(name, check))

    @staticmethod
    def _run(checks: list[_Named]) -> tuple[int, dict[str, Any]]:
        out: list[dict[str, Any]] = []
        up = True
        for c in checks:
            data: Mapping[str, Any] = {}
            try:
                r = c.check()
                ok, data = (r, {}) if isinstance(r, bool) else (bool(r[0]), r[1])
            except Exception as e:
                ok, data = False, {"error": type(e).__name__}
            up = up and ok
            entry: dict[str, Any] = {"name": c.name, "status": "UP" if ok else "DOWN"}
            if data:
                entry["data"] = dict(data)
            out.append(entry)
        return (200 if up else 503), {"status": "UP" if up else "DOWN", "checks": out}

    def live(self) -> tuple[int, dict[str, Any]]:
        return self._run(self._live)

    def ready(self) -> tuple[int, dict[str, Any]]:
        return self._run(self._ready)

    def router(self) -> APIRouter:
        r = APIRouter(prefix="/q/health", tags=["Health"])

        async def respond(fn: Callable[[], tuple[int, dict[str, Any]]]) -> JSONResponse:
            code, body = await run_in_threadpool(fn)
            return JSONResponse(body, status_code=code)

        @r.get("", summary="Health: liveness and readiness")
        async def health() -> JSONResponse:
            return await respond(lambda: self._run(self._live + self._ready))

        @r.get("/live", summary="Liveness: depends on nothing external")
        async def live() -> JSONResponse:
            return await respond(self.live)

        @r.get("/ready", summary="Readiness: the platform sends traffic only when it is UP")
        async def ready() -> JSONResponse:
            return await respond(self.ready)

        return r
