from __future__ import annotations

from typing import Any

import jwt
import pytest
from conftest import FakeCharm

from gluonify import CharmVerifier, GluonifyServices, GluonifyServicesError, InvalidTokenError
from gluonify import charm as charm_module

ISS = "https://id.gluonify.lan/realms/gluonify"


def verifier(charm: FakeCharm, clock: Any = None, **kw: Any) -> CharmVerifier:
    kw.setdefault("audience", "gluonify-source")
    kw.setdefault("issuer", ISS)
    if clock is not None:
        kw["clock"] = clock
    return CharmVerifier("http://charm/certs", fetch=charm.jwks, **kw)


def test_valid_token(charm: FakeCharm) -> None:
    p = verifier(charm).verify(charm.token(roles=["source:read", "source:write", 3], org="acme"))
    assert p.subject == "ada"
    assert p.roles == {"source:read", "source:write"}
    assert p.has_role("source:read")
    assert p.has_all("source:read", "source:write")
    assert not p.has_all("source:read", "admin")
    assert p.organization == "acme"
    assert "claims" not in repr(p)


def test_roles_as_a_string_or_missing(charm: FakeCharm) -> None:
    v = verifier(charm)
    assert v.verify(charm.token(roles="a b")).roles == {"a", "b"}
    assert v.verify(charm.token()).roles == frozenset()
    assert v.verify(charm.token(roles={"x": 1})).roles == frozenset()  # type: ignore[arg-type]
    assert v.verify(charm.token()).organization is None


def test_audience_list(charm: FakeCharm) -> None:
    assert verifier(charm).verify(charm.token(aud=["other", "gluonify-source"])).subject == "ada"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"aud": "gluonify-other"}, "audience"),
        ({"iss": "https://evil/realms/gluonify"}, "issuer"),
        ({"ttl": -120}, "expired"),
    ],
)
def test_refused_claims(charm: FakeCharm, kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(InvalidTokenError, match=message):
        verifier(charm).verify(charm.token(**kwargs))


def test_audience_and_issuer_checks_can_be_off(charm: FakeCharm) -> None:
    v = verifier(charm, audience=None, issuer=None)
    assert v.verify(charm.token(aud="anything", iss="any")).subject == "ada"


def test_sub_is_required(charm: FakeCharm) -> None:
    tok = jwt.encode(
        {"aud": "gluonify-source", "iss": ISS, "exp": 9999999999},
        charm.keys["k1"],
        algorithm="ES256",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidTokenError, match="token refused"):
        verifier(charm).verify(tok)


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c"])
def test_not_a_jwt(charm: FakeCharm, bad: str) -> None:
    with pytest.raises(InvalidTokenError, match="not a JWT"):
        verifier(charm).verify(bad)


def test_hs256_and_none_are_refused(charm: FakeCharm) -> None:
    hs = jwt.encode({"sub": "x"}, "k" * 32, algorithm="HS256")
    with pytest.raises(InvalidTokenError, match="ES256 only"):
        verifier(charm).verify(hs)
    none = jwt.encode({"sub": "x"}, None, algorithm="none")
    with pytest.raises(InvalidTokenError, match="ES256 only"):
        verifier(charm).verify(none)


def test_wrong_signature(charm: FakeCharm) -> None:
    other = FakeCharm()
    with pytest.raises(InvalidTokenError, match="InvalidSignatureError"):
        verifier(charm).verify(other.token())


def test_jwks_is_cached_then_refreshed(charm: FakeCharm, clock: Any) -> None:
    v = verifier(charm, clock)
    v.verify(charm.token())
    v.verify(charm.token())
    assert charm.fetches == 1
    clock.now += 301
    v.verify(charm.token())
    assert charm.fetches == 2


def test_key_rotation_refetches_at_most_every_30_seconds(charm: FakeCharm, clock: Any) -> None:
    v = verifier(charm, clock)
    v.verify(charm.token())
    charm.add_key("k2")
    with pytest.raises(InvalidTokenError, match="unknown signing key"):
        v.verify(charm.token(kid="k2"))  # fetched less than 30 s ago
    assert charm.fetches == 1
    clock.now += 31
    assert v.verify(charm.token(kid="k2")).subject == "ada"
    assert charm.fetches == 2


def test_charm_outage_keeps_the_last_keys(charm: FakeCharm, clock: Any) -> None:
    v = verifier(charm, clock)
    v.verify(charm.token())
    charm.down = True
    clock.now += 400
    assert v.verify(charm.token()).subject == "ada"


def test_charm_unreachable_from_the_start(charm: FakeCharm) -> None:
    charm.down = True
    with pytest.raises(InvalidTokenError, match="not reachable"):
        verifier(charm).verify(charm.token())


def test_unusable_jwks_entries_are_skipped(charm: FakeCharm) -> None:
    good = charm.jwks("x")["keys"][0]
    doc = {
        "keys": [
            {"kty": "RSA", "kid": "r"},
            {"kty": "EC", "use": "enc", "kid": "e"},
            "junk",
            {"kty": "EC", "kid": "bad", "crv": "P-256", "x": "AA", "y": "AA"},
            good,
        ]
    }
    v = CharmVerifier("u", audience="gluonify-source", issuer=ISS, fetch=lambda _: doc)
    assert v.verify(charm.token()).subject == "ada"
    v2 = CharmVerifier("u", audience=None, fetch=lambda _: ["not", "a", "mapping"])  # type: ignore[arg-type,return-value]
    with pytest.raises(InvalidTokenError, match="unknown signing key"):
        v2.verify(charm.token())


def test_from_env_on_the_platform(charm: FakeCharm) -> None:
    s = GluonifyServices(
        {
            "GLUONIFY_ENVIRONMENT": "SBX",
            "GLUONIFY_SERVICE_CHARM_URL": "http://charm.internal:8080/",
            "GLUONIFY_OIDC_ISSUER": ISS,
            "GLUONIFY_OIDC_AUDIENCE": "gluonify-shop",
        }
    )
    v = CharmVerifier.from_env(s, audience="gluonify-source", fetch=charm.jwks)
    assert v.jwks_url == "http://charm.internal:8080/realms/gluonify/protocol/openid-connect/certs"
    assert v.audience == "gluonify-shop"
    assert v.issuer == ISS
    assert v.verify(charm.token(aud="gluonify-shop")).subject == "ada"
    assert charm.last_url == v.jwks_url


def test_from_env_defaults_when_not_injected(charm: FakeCharm) -> None:
    s = GluonifyServices({"GLUONIFY_SERVICE_CHARM_URL": "http://localhost:8180"})
    v = CharmVerifier.from_env(s, audience="gluonify-source", issuer="x", fetch=charm.jwks)
    assert v.audience == "gluonify-source"
    assert v.issuer == "x"


def test_from_env_without_charm() -> None:
    with pytest.raises(GluonifyServicesError, match='did not grant the service "charm"'):
        CharmVerifier.from_env(GluonifyServices({"GLUONIFY_ENVIRONMENT": "SBX"}), audience="a")


def test_http_fetch_refuses_other_schemes() -> None:
    with pytest.raises(GluonifyServicesError, match="http"):
        charm_module._http_fetch("file:///etc/passwd")


def test_http_fetch_reads_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"keys": []}'

    seen: dict[str, Any] = {}

    def urlopen(req: Any, timeout: float) -> Response:
        seen["url"], seen["timeout"] = req.full_url, timeout
        return Response()

    monkeypatch.setattr(charm_module.urllib.request, "urlopen", urlopen)
    assert charm_module._http_fetch("https://charm/certs") == {"keys": []}
    assert seen == {"url": "https://charm/certs", "timeout": 5}
