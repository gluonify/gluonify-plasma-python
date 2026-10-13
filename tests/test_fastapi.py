from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeCharm
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from gluonify import CharmVerifier, GluonifyServices, GluonifyServicesError, Principal
from gluonify_fastapi import GluonifyAuth, create_app, current_principal, optional_principal, require_roles

ISS = "https://id.gluonify.lan/realms/gluonify"


class Body(BaseModel):
    title: str = Field(min_length=1, max_length=5)


def build(charm: FakeCharm, **kw: Any) -> FastAPI:
    services = kw.pop("services", GluonifyServices({}))
    auth = kw.pop("auth", None) or GluonifyAuth(
        lambda: CharmVerifier("u", audience="gluonify-source", issuer=ISS, fetch=charm.jwks), **kw
    )
    app = create_app("test", audience="gluonify-source", services=services, auth=auth)

    @app.get("/read")
    async def read(p: Principal = Depends(require_roles("source:read"))) -> dict[str, Any]:  # noqa: B008
        return {"sub": p.subject}

    @app.get("/me")
    async def me(p: Principal = Depends(current_principal)) -> dict[str, Any]:  # noqa: B008
        return {"sub": p.subject}

    @app.get("/maybe")
    async def maybe(p: Principal | None = Depends(optional_principal)) -> dict[str, Any]:  # noqa: B008
        return {"sub": p.subject if p else None}

    @app.post("/body")
    async def body(b: Body) -> Body:
        return b

    return app


def bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


def test_roles(charm: FakeCharm) -> None:
    c = TestClient(build(charm))
    assert c.get("/read", headers=bearer(charm.token(roles=["source:read"]))).json() == {"sub": "ada"}
    r = c.get("/read", headers=bearer(charm.token(roles=["source:write"])))
    assert r.status_code == 403
    assert r.json() == {"error": "missing role: source:read"}
    assert c.get("/me", headers=bearer(charm.token())).json() == {"sub": "ada"}


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer ", "Bearer not-a-jwt"])
def test_401(charm: FakeCharm, header: str | None) -> None:
    c = TestClient(build(charm))
    r = c.get("/read", headers={} if header is None else {"Authorization": header})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert "error" in r.json()


def test_wrong_audience_is_401(charm: FakeCharm) -> None:
    r = TestClient(build(charm)).get("/read", headers=bearer(charm.token(aud="other", roles=["source:read"])))
    assert r.status_code == 401
    assert "audience" in r.json()["error"]


def test_charm_unavailable_is_503(charm: FakeCharm) -> None:
    def factory() -> CharmVerifier:
        raise GluonifyServicesError("charm not granted")

    r = TestClient(build(charm, auth=GluonifyAuth(factory))).get("/read", headers=bearer("a.b.c"))
    assert r.status_code == 503
    assert "charm not granted" in r.json()["error"]
    r = TestClient(build(charm, auth=GluonifyAuth(None))).get("/read", headers=bearer("a.b.c"))
    assert r.status_code == 503


def test_verifier_built_once(charm: FakeCharm) -> None:
    built: list[int] = []

    def factory() -> CharmVerifier:
        built.append(1)
        return CharmVerifier("u", audience="gluonify-source", issuer=ISS, fetch=charm.jwks)

    c = TestClient(build(charm, auth=GluonifyAuth(factory)))
    for _ in range(3):
        c.get("/me", headers=bearer(charm.token()))
    assert built == [1]


def test_optional_principal(charm: FakeCharm) -> None:
    c = TestClient(build(charm))
    assert c.get("/maybe").json() == {"sub": None}
    assert c.get("/maybe", headers=bearer(charm.token())).json() == {"sub": "ada"}
    assert c.get("/maybe", headers=bearer("x.y.z")).status_code == 401


def test_dev_identity(charm: FakeCharm) -> None:
    dev = Principal("dev", frozenset({"source:read"}), {})
    app = build(charm, dev_identity=dev)
    c = TestClient(app)
    assert c.get("/read").json() == {"sub": "dev"}
    assert c.get("/maybe").json() == {"sub": "dev"}
    assert not app.state.gluonify_auth.sign_in_required


def test_dev_identity_refused_on_the_platform() -> None:
    with pytest.raises(RuntimeError, match="refused on the platform"):
        create_app(
            "x",
            audience="a",
            services=GluonifyServices({"GLUONIFY_ENVIRONMENT": "PRD"}),
            dev_identity=Principal("dev", frozenset(), {}),
        )


def test_default_auth_uses_the_environment(charm: FakeCharm, monkeypatch: pytest.MonkeyPatch) -> None:
    s = GluonifyServices({"GLUONIFY_ENVIRONMENT": "SBX", "GLUONIFY_SERVICE_CHARM_URL": "http://charm"})
    app = create_app("x", audience="gluonify-source", services=s)
    v = app.state.gluonify_auth.verifier()
    assert v.jwks_url == "http://charm/realms/gluonify/protocol/openid-connect/certs"
    assert v.audience == "gluonify-source"
    assert app.state.gluonify_auth.sign_in_required


def test_not_created_by_the_factory() -> None:
    app = FastAPI()

    @app.get("/x")
    async def x(p: Principal = Depends(current_principal)) -> str:  # noqa: B008
        return p.subject

    assert TestClient(app).get("/x").status_code == 500


def test_validation_errors_are_400(charm: FakeCharm) -> None:
    c = TestClient(build(charm))
    r = c.post("/body", json={"title": "too long"})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "invalid body"
    assert body["violations"][0]["field"] == "title"


def test_health(charm: FakeCharm) -> None:
    app = build(charm)
    c = TestClient(app)
    assert c.get("/q/health/live").json() == {"status": "UP", "checks": []}
    state = {"ok": True}
    app.state.health.readiness("store", lambda: (state["ok"], {"store": "memory"}))
    app.state.health.liveness("alive", lambda: True)
    r = c.get("/q/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "UP", "checks": [{"name": "store", "status": "UP", "data": {"store": "memory"}}]}
    state["ok"] = False
    r = c.get("/q/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "DOWN"
    assert c.get("/q/health/live").status_code == 200

    def boom() -> bool:
        raise ValueError("x")

    app.state.health.readiness("boom", boom)
    r = c.get("/q/health")
    assert r.status_code == 503
    assert {"name": "boom", "status": "DOWN", "data": {"error": "ValueError"}} in r.json()["checks"]


def test_openapi_and_swagger_paths(charm: FakeCharm) -> None:
    c = TestClient(build(charm))
    assert c.get("/q/openapi").json()["info"]["title"] == "test"
    assert c.get("/q/swagger-ui").status_code == 200
    assert c.get("/redoc").status_code == 404
    assert c.get("/nothing").json() == {"error": "Not Found"}


def test_top_on_app_state() -> None:
    app = create_app("x", audience="a", services=GluonifyServices({"APP_K": "v"}))
    assert app.state.top.get("k") == "v"
