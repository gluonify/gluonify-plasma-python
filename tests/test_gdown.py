from __future__ import annotations

import io
import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from fakes import Counters, FakeDriver, Result, Summary

from gluonify import GluonifyServices, GluonifyServicesError
from gluonify_gdown import BoltTransport, GdownClient, GdownError, HttpTransport, dedicated_database
from gluonify_gdown import client as gd

P = {"GLUONIFY_ENVIRONMENT": "SBX"}
DB = {"GLUONIFY_GDOWN_DATABASE": "app_1234", "GLUONIFY_GDOWN_USER": "u", "GLUONIFY_GDOWN_PASSWORD": "pw"}
HTTP = {**P, **DB, "GLUONIFY_SERVICE_GDOWN_URL": "http://gdown.internal:8080/"}


def test_dedicated_database() -> None:
    db = dedicated_database(GluonifyServices({**P, **DB}))
    assert db is not None
    assert (db.database, db.user, db.password) == ("app_1234", "u", "pw")
    assert "pw" not in repr(db)


def test_vault_keys_win() -> None:
    db = dedicated_database(GluonifyServices({**P, **DB, "APP_GLUONIFY_GDOWN_USER": "vu", "APP_GLUONIFY_GDOWN_PASSWORD": "vp"}))
    assert db is not None
    assert (db.user, db.password) == ("vu", "vp")


def test_no_dedicated_database() -> None:
    assert dedicated_database(GluonifyServices(P)) is None
    assert GdownClient.optional(GluonifyServices(P)) is None
    with pytest.raises(GluonifyServicesError, match="no dedicated gDown database"):
        GdownClient.from_env(GluonifyServices(P))


def test_incomplete_database() -> None:
    with pytest.raises(GluonifyServicesError, match="GLUONIFY_GDOWN_USER or GLUONIFY_GDOWN_PASSWORD"):
        dedicated_database(GluonifyServices({**P, "GLUONIFY_GDOWN_DATABASE": "d", "GLUONIFY_GDOWN_USER": "u"}))


# ------------------------------------------------------------------ choice of the transport


def test_without_bolt_url_it_is_http_never_a_derived_bolt_port() -> None:
    c = GdownClient.from_env(GluonifyServices(HTTP))
    assert c.kind == "http"
    assert isinstance(c.transport, HttpTransport)
    assert c.transport.url == "http://gdown.internal:8080"
    assert "7687" not in repr(c)
    assert "pw" not in repr(c)
    assert GdownClient.optional(GluonifyServices(HTTP)) is not None


def test_without_any_address() -> None:
    with pytest.raises(GluonifyServicesError, match='did not grant the service "gdown"'):
        GdownClient.from_env(GluonifyServices({**P, **DB}))


@pytest.mark.parametrize("url", ["bolt+s://gdown.internal:7687", "neo4j+s://gdown.internal:7687/"])
def test_bolt_with_tls(url: str) -> None:
    c = GdownClient.from_env(GluonifyServices({**HTTP, "GLUONIFY_GDOWN_BOLT_URL": url}))
    assert c.kind == "bolt"
    assert isinstance(c.transport, BoltTransport)
    assert c.transport.url == url.rstrip("/")
    assert c.transport.ca_file is None
    assert "pw" not in repr(c)


@pytest.mark.parametrize("url", ["bolt://g:7687", "neo4j://g:7687"])
def test_plain_bolt_refused(url: str) -> None:
    with pytest.raises(GluonifyServicesError, match="without TLS"):
        GdownClient.from_env(GluonifyServices({**HTTP, "GLUONIFY_GDOWN_BOLT_URL": url}))


def test_plain_bolt_only_for_the_dev_identity_off_the_platform() -> None:
    off = {**DB, "GLUONIFY_GDOWN_BOLT_URL": "bolt://localhost:7687"}
    c = GdownClient.from_env(GluonifyServices(off), insecure_dev=True)
    assert c.kind == "bolt"
    assert GdownClient.optional(GluonifyServices(off), insecure_dev=True) is not None
    with pytest.raises(GluonifyServicesError, match="refused on the platform"):
        GdownClient.from_env(GluonifyServices({**HTTP, "GLUONIFY_GDOWN_BOLT_URL": "bolt://g"}), insecure_dev=True)


@pytest.mark.parametrize("url", ["http://g", "bolt+s://", "ftp://g"])
def test_invalid_bolt_url(url: str) -> None:
    with pytest.raises(GluonifyServicesError, match="bolt\\+s://"):
        GdownClient.from_env(GluonifyServices({**HTTP, "GLUONIFY_GDOWN_BOLT_URL": url}))


def test_ca_file(tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("PEM")
    env = {**HTTP, "GLUONIFY_GDOWN_BOLT_URL": "bolt+s://g:7687", "GLUONIFY_GDOWN_CA_FILE": f" {ca} "}
    c = GdownClient.from_env(GluonifyServices(env))
    assert isinstance(c.transport, BoltTransport)
    assert c.transport.ca_file == ca
    with pytest.raises(GluonifyServicesError, match="does not exist"):
        GdownClient.from_env(GluonifyServices({**env, "GLUONIFY_GDOWN_CA_FILE": str(tmp_path / "absent")}))
    with pytest.raises(GluonifyServicesError, match="has no TLS"):
        BoltTransport("bolt://g", "d", ("u", "p"), ca_file=ca, insecure_dev=True)


def test_invalid_http_url() -> None:
    with pytest.raises(GluonifyServicesError, match="http"):
        HttpTransport("ws://g", "d", ("u", "p"))


# ------------------------------------------------------------------ Bolt


def test_bolt_query_write_ready_close() -> None:
    def answer(q: str, p: dict[str, Any]) -> Result:
        if q.startswith("MATCH"):
            return Result([{"id": "1"}, {"id": "2"}])
        return Result([], Summary(Counters(nodes_created=1, properties_set=3)))

    d = FakeDriver(answer)
    made: list[tuple[str, Any, Any]] = []

    def factory(url: str, auth: tuple[str, str], ca: Path | None) -> FakeDriver:
        made.append((url, auth, ca))
        return d

    c = GdownClient(BoltTransport("bolt+s://g:7687", "app", ("u", "p"), factory=factory))
    assert made == []
    assert c.query("MATCH (n) RETURN n.id AS id") == [{"id": "1"}, {"id": "2"}]
    assert c.write("CREATE (:N {id: $id})", {"id": "x"})["nodes_created"] == 1
    assert d.calls[1] == ("CREATE (:N {id: $id})", {"id": "x"}, "app")
    assert made == [("bolt+s://g:7687", ("u", "p"), None)]
    assert c.ready()
    d.fail = True
    assert not c.ready()
    c.close()
    assert d.closed
    c.close()


class FakeNeo4j:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def module(self) -> types.SimpleNamespace:
        calls = self.calls

        class GraphDatabase:
            @staticmethod
            def driver(url: str, **kw: Any) -> FakeDriver:
                calls.append((url, kw))
                return FakeDriver()

        return types.SimpleNamespace(GraphDatabase=GraphDatabase, TrustCustomCAs=lambda *f: ("custom", f))


def test_real_driver_with_system_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNeo4j()
    monkeypatch.setitem(sys.modules, "neo4j", fake.module())
    gd._bolt_driver("bolt+s://g:7687", ("u", "p"), None)
    assert fake.calls == [("bolt+s://g:7687", {"auth": ("u", "p")})]


def test_real_driver_with_the_platform_ca(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeNeo4j()
    monkeypatch.setitem(sys.modules, "neo4j", fake.module())
    gd._bolt_driver("neo4j+s://g:7687", ("u", "p"), tmp_path / "ca.pem")
    url, kw = fake.calls[0]
    assert url == "neo4j://g:7687"  # TLS kept by encrypted=True: the driver refuses a custom CA with "+s"
    assert kw["encrypted"] is True
    assert kw["trusted_certificates"] == ("custom", (str(tmp_path / "ca.pem"),))


def test_the_official_driver_is_installed() -> None:
    import neo4j

    assert hasattr(neo4j, "TrustCustomCAs")


# ------------------------------------------------------------------ HTTP


def http_client(answer: Any) -> tuple[GdownClient, list[urllib.request.Request]]:
    seen: list[urllib.request.Request] = []

    def post(req: urllib.request.Request, timeout: float) -> bytes:
        seen.append(req)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, bytes) else json.dumps(answer).encode()

    return GdownClient(HttpTransport("http://g:8080/", "app", ("u", "pw"), post=post)), seen


def test_http_query_like_graph_note_store() -> None:
    c, seen = http_client({"columns": ["id", "title"], "rows": [["1", "a"], ["2", "b"], "junk"]})
    assert c.query("MATCH (n) RETURN n.id AS id, n.title AS title", {"x": 1}) == [
        {"id": "1", "title": "a"},
        {"id": "2", "title": "b"},
    ]
    req = seen[0]
    assert req.full_url == "http://g:8080/db/app/query"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Basic dTpwdw=="
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {"statement": "MATCH (n) RETURN n.id AS id, n.title AS title", "parameters": {"x": 1}}  # type: ignore[arg-type]


def test_http_counters() -> None:
    c, _ = http_client({"columns": [], "rows": [], "stats": {"nodesDeleted": 1, "propertiesSet": None}})
    assert c.write("MATCH (n) DELETE n") == {
        "nodes_created": 0,
        "nodes_deleted": 1,
        "relationships_created": 0,
        "relationships_deleted": 0,
        "properties_set": 0,
    }
    c2, _ = http_client({"stats": "odd"})
    assert c2.write("x")["nodes_deleted"] == 0


@pytest.mark.parametrize(("body", "message"), [(b"not json", "not JSON"), (b"[1]", "not a JSON object")])
def test_http_bad_answers(body: bytes, message: str) -> None:
    c, _ = http_client(body)
    with pytest.raises(GdownError, match=message):
        c.query("RETURN 1")
    assert not c.ready()


def test_http_urlopen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    t = HttpTransport("https://g", "app", ("u", "p"))
    seen: dict[str, Any] = {}

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def urlopen(req: Any, timeout: float, context: Any) -> Response:
        seen["context"] = context
        return Response(b'{"columns": ["ok"], "rows": [[1]]}')

    monkeypatch.setattr(gd.urllib.request, "urlopen", urlopen)
    assert GdownClient(t).query("RETURN 1 AS ok") == [{"ok": 1}]
    assert seen["context"] is None

    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    created: list[str] = []
    monkeypatch.setattr(gd.ssl, "create_default_context", lambda cafile: created.append(cafile) or "ctx")
    GdownClient(HttpTransport("https://g", "app", ("u", "p"), ca_file=ca)).query("RETURN 1 AS ok")
    assert created == [str(ca)]
    assert seen["context"] == "ctx"


def test_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    t = HttpTransport("http://g", "app", ("u", "secret-pw"))

    def refused(req: Any, timeout: float, context: Any) -> Any:
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error":"bad credentials"}'))  # type: ignore[arg-type]

    monkeypatch.setattr(gd.urllib.request, "urlopen", refused)
    with pytest.raises(GdownError, match="HTTP 401") as e:
        GdownClient(t).query("RETURN 1")
    assert "secret-pw" not in str(e.value)

    def down(req: Any, timeout: float, context: Any) -> Any:
        raise ConnectionRefusedError()

    monkeypatch.setattr(gd.urllib.request, "urlopen", down)
    with pytest.raises(GdownError, match="unreachable"):
        GdownClient(t).query("RETURN 1")
    GdownClient(t).close()
