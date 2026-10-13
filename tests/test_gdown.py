from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from fakes import Counters, FakeDriver, Result, Summary

from gluonify import GdownClient, GluonifyServices, GluonifyServicesError, bolt_url, dedicated_database

P = {"GLUONIFY_ENVIRONMENT": "SBX"}
DB = {"GLUONIFY_GDOWN_DATABASE": "app_1234", "GLUONIFY_GDOWN_USER": "u", "GLUONIFY_GDOWN_PASSWORD": "pw"}


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


@pytest.mark.parametrize(
    ("url", "bolt"),
    [
        ("http://gdown.internal:8080", "bolt://gdown.internal:7687"),
        ("https://gdown.example/x", "bolt+s://gdown.example:7687"),
        ("http://[fd00::1]:8080", "bolt://[fd00::1]:7687"),
    ],
)
def test_bolt_url_derived(url: str, bolt: str) -> None:
    assert bolt_url(GluonifyServices({**P, "GLUONIFY_SERVICE_GDOWN_URL": url})) == bolt


def test_bolt_url_explicit() -> None:
    s = GluonifyServices({**P, "GLUONIFY_GDOWN_BOLT_URL": "neo4j://g:7999/"})
    assert bolt_url(s) == "neo4j://g:7999"
    with pytest.raises(GluonifyServicesError, match="bolt://"):
        bolt_url(GluonifyServices({"GLUONIFY_GDOWN_BOLT_URL": "http://g"}))


def test_bolt_url_without_gdown() -> None:
    with pytest.raises(GluonifyServicesError, match='"gdown"'):
        bolt_url(GluonifyServices(P))


def test_from_env() -> None:
    c = GdownClient.from_env(GluonifyServices({**P, **DB, "GLUONIFY_SERVICE_GDOWN_URL": "http://g:8080"}))
    assert c.url == "bolt://g:7687"
    assert c.database == "app_1234"
    assert "pw" not in repr(c)
    c2 = GdownClient.optional(GluonifyServices({**P, **DB, "GLUONIFY_SERVICE_GDOWN_URL": "http://g:8080"}))
    assert c2 is not None


def test_query_and_write_go_to_the_database() -> None:
    def answer(q: str, p: dict[str, Any]) -> Result:
        if q.startswith("MATCH"):
            return Result([{"id": "1"}, {"id": "2"}])
        return Result([], Summary(Counters(nodes_created=1, properties_set=3)))

    d = FakeDriver(answer)
    c = GdownClient("bolt://g:7687", "app", ("u", "p"), driver=d)
    assert c.query("MATCH (n) RETURN n.id AS id") == [{"id": "1"}, {"id": "2"}]
    assert c.write("CREATE (:N {id: $id})", {"id": "x"})["nodes_created"] == 1
    assert d.calls[1] == ("CREATE (:N {id: $id})", {"id": "x"}, "app")
    assert c.ready()
    d.fail = True
    assert not c.ready()
    c.close()
    assert d.closed


def test_real_driver_is_created_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[str, Any]] = []

    class GraphDatabase:
        @staticmethod
        def driver(url: str, auth: Any) -> FakeDriver:
            created.append((url, auth))
            return FakeDriver()

    monkeypatch.setitem(sys.modules, "neo4j", types.SimpleNamespace(GraphDatabase=GraphDatabase))
    c = GdownClient("bolt://g:7687", "app", ("u", "p"))
    assert created == []
    assert c.driver is c.driver
    assert created == [("bolt://g:7687", ("u", "p"))]
    c.close()
    c.close()


def test_the_official_driver_is_installed() -> None:
    import neo4j

    assert hasattr(neo4j, "GraphDatabase")
