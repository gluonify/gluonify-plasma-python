from __future__ import annotations

import threading
from pathlib import Path

import pytest

import gluonify
from gluonify import services as svc
from gluonify.services import Endpoint, GluonifyServices, GluonifyServicesError, Source

PLATFORM = {"GLUONIFY_ENVIRONMENT": "SBX"}


def test_keys_follow_the_java_sdk() -> None:
    assert svc.url_key("crystallon") == "GLUONIFY_SERVICE_CRYSTALLON_URL"
    assert svc.url_key("my-svc") == "GLUONIFY_SERVICE_MY_SVC_URL"
    assert svc.token_key("crystallon") == "GLUONIFY_CRYSTALLON_TOKEN"
    assert svc.token_file_key("my-svc") == "GLUONIFY_MY_SVC_TOKEN_FILE"


@pytest.mark.parametrize("bad", ["", "-a", "a-", "A B", "a_b", "x" * 64, "é"])
def test_invalid_service_names_are_refused(bad: str) -> None:
    services = GluonifyServices({})
    with pytest.raises(ValueError, match="not a service name"):
        services.resolve(bad)


def test_names_are_normalised() -> None:
    s = GluonifyServices({**PLATFORM, "GLUONIFY_SERVICE_CHARM_URL": "http://charm:8080/"})
    e = s.resolve("  Charm ")
    assert e.service == "charm"
    assert e.url == "http://charm:8080"


def test_platform_detection_and_variables() -> None:
    s = GluonifyServices(
        {
            **PLATFORM,
            "GLUONIFY_REPLICA": "2",
            "GLUONIFY_SELF_URL": "http://10.0.0.5:8080",
            "GLUONIFY_NODE_ADDRESS": "10.0.0.5",
        }
    )
    assert s.on_platform
    assert s.environment == "SBX"
    assert s.replica == "2"
    assert s.self_url == "http://10.0.0.5:8080"
    assert s.node_address == "10.0.0.5"
    off = GluonifyServices({"GLUONIFY_ENVIRONMENT": "  "})
    assert not off.on_platform
    assert off.environment is None
    assert off.replica is None


def test_granted_lists_injected_services() -> None:
    s = GluonifyServices(
        {
            **PLATFORM,
            "GLUONIFY_SERVICE_CHARM_URL": "http://charm",
            "GLUONIFY_SERVICE_MY_APP_URL": " http://my-app ",
            "GLUONIFY_SERVICE_EMPTY_URL": "",
            "GLUONIFY_SERVICE_X": "no suffix",
        }
    )
    assert s.granted() == {"charm": "http://charm", "my-app": "http://my-app"}


def test_platform_uses_the_token_file_and_rereads_it(tmp_path: Path) -> None:
    f = tmp_path / "token"
    s = GluonifyServices(
        {**PLATFORM, "GLUONIFY_SERVICE_CRYSTALLON_URL": "http://c:1", "GLUONIFY_CRYSTALLON_TOKEN_FILE": f" {f} "}
    )
    e = s.resolve("crystallon")
    assert e.source is Source.PLATFORM
    assert e.token() is None  # not written by the agent yet
    f.write_text("t1\n")
    assert e.token() == "t1"
    f.write_text("t2")
    assert e.token() == "t2"
    assert e.headers() == {"Authorization": "Bearer t2"}
    f.write_text("   ")
    assert e.token() is None
    assert e.headers() == {}


def test_platform_token_file_unreadable(tmp_path: Path) -> None:
    s = GluonifyServices(
        {**PLATFORM, "GLUONIFY_SERVICE_X_URL": "http://x", "GLUONIFY_X_TOKEN_FILE": str(tmp_path)}  # a directory
    )
    service = s.resolve("x")
    with pytest.raises(GluonifyServicesError, match="cannot read the token file of x") as e:
        service.token()
    assert str(tmp_path) in str(e.value)


def test_platform_fixed_token_when_no_file() -> None:
    s = GluonifyServices({**PLATFORM, "GLUONIFY_SERVICE_X_URL": "http://x", "GLUONIFY_X_TOKEN": " abc "})
    assert s.resolve("x").token() == "abc"


def test_platform_file_wins_over_fixed_token(tmp_path: Path) -> None:
    f = tmp_path / "t"
    f.write_text("from-file")
    s = GluonifyServices(
        {**PLATFORM, "GLUONIFY_SERVICE_X_URL": "http://x", "GLUONIFY_X_TOKEN": "fixed", "GLUONIFY_X_TOKEN_FILE": str(f)}
    )
    assert s.resolve("x").token() == "from-file"


def test_platform_without_tokens() -> None:
    e = GluonifyServices({**PLATFORM, "GLUONIFY_SERVICE_X_URL": "http://x"}).resolve("x")
    assert e.token() is None


def test_platform_service_not_granted() -> None:
    s = GluonifyServices(PLATFORM)
    with pytest.raises(GluonifyServicesError, match='did not grant the service "gdown"') as e:
        s.resolve("gdown")
    assert "GLUONIFY_SERVICE_GDOWN_URL" in str(e.value)
    assert s.resolve_optional("gdown") is None


def test_on_platform_the_environment_is_not_an_override() -> None:
    # On the platform an env URL is a PLATFORM resolution (its token file is the agent's), never an OVERRIDE.
    s = GluonifyServices({**PLATFORM, "GLUONIFY_SERVICE_X_URL": "http://x"})
    assert s.resolve("x").source is Source.PLATFORM
    assert s.resolve_optional("x") is not None


def test_overrides_win_even_on_the_platform(tmp_path: Path) -> None:
    f = tmp_path / "t"
    f.write_text("file-token")
    s = GluonifyServices(
        {**PLATFORM, "GLUONIFY_SERVICE_X_URL": "http://platform"},
        overrides={"GLUONIFY_SERVICE_X_URL": "http://override/", "GLUONIFY_X_TOKEN_FILE": str(f)},
    )
    e = s.resolve("x")
    assert e.source is Source.OVERRIDE
    assert e.url == "http://override"
    assert e.token() == "file-token"
    assert s.resolve_optional("x") == e


def test_override_token_precedence(tmp_path: Path) -> None:
    f = tmp_path / "t"
    f.write_text("env-file")
    base = {"GLUONIFY_SERVICE_X_URL": "http://x"}
    assert GluonifyServices({**base, "GLUONIFY_X_TOKEN": "env", "GLUONIFY_X_TOKEN_FILE": str(f)}).resolve("x").token() == "env"
    assert GluonifyServices({**base, "GLUONIFY_X_TOKEN_FILE": str(f)}).resolve("x").token() == "env-file"
    assert (
        GluonifyServices({**base, "GLUONIFY_X_TOKEN": "env"}, overrides={"GLUONIFY_X_TOKEN": "prop"}).resolve("x").token()
        == "prop"
    )
    assert GluonifyServices(base).resolve("x").token() is None


def test_off_platform_environment_is_an_override() -> None:
    e = GluonifyServices({"GLUONIFY_SERVICE_X_URL": "https://x.example/api//"}).resolve("x")
    assert e.source is Source.OVERRIDE
    assert e.url == "https://x.example/api"
    assert e.resolve("/v1/a") == "https://x.example/api/v1/a"
    assert e.resolve("v1/a") == "https://x.example/api/v1/a"


def test_off_platform_without_address() -> None:
    s = GluonifyServices({})
    with pytest.raises(GluonifyServicesError, match="no address for the service 'x'") as e:
        s.resolve("x")
    assert "GLUONIFY_SERVICE_X_URL" in str(e.value)
    assert s.resolve_optional("x") is None


@pytest.mark.parametrize("bad", ["ftp://x", "/relative", "x", "http://", "http://[bad"])
def test_invalid_urls(bad: str) -> None:
    services = GluonifyServices({"GLUONIFY_SERVICE_X_URL": bad})
    with pytest.raises(GluonifyServicesError, match="GLUONIFY_SERVICE_X_URL"):
        services.resolve("x")


@pytest.mark.parametrize("scheme", ["http", "https", "ws", "wss"])
def test_accepted_schemes(scheme: str) -> None:
    assert GluonifyServices({"GLUONIFY_SERVICE_X_URL": f"{scheme}://h"}).resolve("x").url == f"{scheme}://h"


def test_repr_never_shows_the_token() -> None:
    e = GluonifyServices({"GLUONIFY_SERVICE_X_URL": "http://x", "GLUONIFY_X_TOKEN": "s3cr3t"}).resolve("x")
    assert "s3cr3t" not in repr(e)
    assert e == Endpoint("x", "http://x", lambda: "other", Source.OVERRIDE)


def test_reads_the_environment_live(monkeypatch: pytest.MonkeyPatch) -> None:
    s = GluonifyServices()
    monkeypatch.delenv("GLUONIFY_ENVIRONMENT", raising=False)
    monkeypatch.setenv("GLUONIFY_SERVICE_LIVE_URL", "http://one")
    assert s.resolve("live").url == "http://one"
    monkeypatch.setenv("GLUONIFY_SERVICE_LIVE_URL", "http://two")
    assert s.resolve("live").url == "http://two"
    assert s.environ is not None


def test_defaults_is_a_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "_defaults", None)
    seen: list[GluonifyServices] = []
    threads = [threading.Thread(target=lambda: seen.append(svc.defaults())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(x) for x in seen}) == 1
    monkeypatch.delenv("GLUONIFY_ENVIRONMENT", raising=False)
    monkeypatch.setenv("GLUONIFY_SERVICE_SHORT_URL", "http://short")
    assert gluonify.endpoint("short").url == "http://short"
    assert gluonify.optional("short") is not None
    assert gluonify.optional("absent") is None
