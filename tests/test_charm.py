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
    check = verifier(charm)
    signed = charm.token(**kwargs)
    with pytest.raises(InvalidTokenError, match=message):
        check.verify(signed)


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
    check = verifier(charm)
    with pytest.raises(InvalidTokenError, match="token refused"):
        check.verify(tok)


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c"])
def test_not_a_jwt(charm: FakeCharm, bad: str) -> None:
    check = verifier(charm)
    with pytest.raises(InvalidTokenError, match="not a JWT"):
        check.verify(bad)


def test_hs256_and_none_are_refused(charm: FakeCharm) -> None:
    hs = jwt.encode({"sub": "x"}, "k" * 32, algorithm="HS256")
    check = verifier(charm)
    with pytest.raises(InvalidTokenError, match="ES256 only"):
        check.verify(hs)
    none = jwt.encode({"sub": "x"}, None, algorithm="none")
    with pytest.raises(InvalidTokenError, match="ES256 only"):
        check.verify(none)


def test_wrong_signature(charm: FakeCharm) -> None:
    other = FakeCharm()
    check = verifier(charm)
    signed = other.token()
    with pytest.raises(InvalidTokenError, match="InvalidSignatureError"):
        check.verify(signed)


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
    signed = charm.token(kid="k2")
    with pytest.raises(InvalidTokenError, match="unknown signing key"):
        v.verify(signed)  # fetched less than 30 s ago
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
    check = verifier(charm)
    signed = charm.token()
    with pytest.raises(InvalidTokenError, match="not reachable"):
        check.verify(signed)


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
    signed = charm.token()
    with pytest.raises(InvalidTokenError, match="unknown signing key"):
        v2.verify(signed)


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


ORG_ISS = "https://id.gluonify.lan/realms/org-acme"
PLATFORM_CERTS = "http://charm.internal:8080/realms/gluonify/protocol/openid-connect/certs"
ORG_CERTS = "http://charm.internal:8080/realms/org-acme/protocol/openid-connect/certs"


def platform_env(**extra: str) -> GluonifyServices:
    return GluonifyServices(
        {
            "GLUONIFY_ENVIRONMENT": "SBX",
            "GLUONIFY_SERVICE_CHARM_URL": "http://charm.internal:8080",
            "GLUONIFY_OIDC_AUDIENCE": "gluonify-shop",
            **extra,
        }
    )


def test_certs_path_of_the_platform_and_of_an_organization() -> None:
    assert charm_module.certs_path() == charm_module.CERTS_PATH
    assert charm_module.certs_path("gluonify") == "/realms/gluonify/protocol/openid-connect/certs"
    assert charm_module.certs_path("org-acme") == "/realms/org-acme/protocol/openid-connect/certs"
    assert charm_module.certs_path("org-" + "a" * 40) == "/realms/org-" + "a" * 40 + "/protocol/openid-connect/certs"
    assert charm_module.certs_path("org-0") == "/realms/org-0/protocol/openid-connect/certs"


INVALID_REALMS = [
    "org-acme/../gluonify",
    "../gluonify",
    "..",
    "org-acme/",
    "/org-acme",
    "org-acme\\x",
    "org-ac me",
    " org-acme",
    "gluonify ",
    "org-acme\n",
    "Org-Acme",
    "ORG-ACME",
    "GLUONIFY",
    "org-" + "a" * 41,
    "x" * 64,
    "org-acme%2F..",
    "org-acme%2e%2e",
    "%67luonify",
    "org-acme?x=1",
    "org-acme#x",
    "org-",
    "org--acme",
    "org-acme-",
    "org-ac_me",
    "org-ac.me",
    "master",
    "acme",
    "gluonify2",
    "org-acme\x00",
]


@pytest.mark.parametrize("realm", INVALID_REALMS)
def test_certs_path_refuses_what_charm_does_not_serve(realm: str) -> None:
    with pytest.raises(ValueError, match="not a realm Charm serves"):
        charm_module.certs_path(realm)


@pytest.mark.parametrize("realm", [None, "", "  "])
def test_from_env_reads_the_platform_realm_when_none_is_injected(charm: FakeCharm, realm: str | None) -> None:
    extra = {} if realm is None else {"GLUONIFY_OIDC_REALM": realm}
    v = CharmVerifier.from_env(platform_env(GLUONIFY_OIDC_ISSUER=ISS, **extra), fetch=charm.jwks)
    assert v.jwks_url == PLATFORM_CERTS
    assert v.verify(charm.token(aud="gluonify-shop")).subject == "ada"
    assert charm.last_url == PLATFORM_CERTS


def test_from_env_reads_the_keys_of_the_organization_realm() -> None:
    platform, org = FakeCharm(kid="platform-k1"), FakeCharm(kid="org-acme-k1")
    realms = {PLATFORM_CERTS: platform, ORG_CERTS: org}
    fetched: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        fetched.append(url)
        return realms[url].jwks(url)

    s = platform_env(GLUONIFY_OIDC_REALM="org-acme", GLUONIFY_OIDC_ISSUER=ORG_ISS)
    v = CharmVerifier.from_env(s, fetch=fetch)
    assert v.jwks_url == ORG_CERTS
    assert v.issuer == ORG_ISS
    p = v.verify(org.token(aud="gluonify-shop", iss=ORG_ISS, org="acme"))
    assert (p.subject, p.organization) == ("ada", "acme")
    assert set(fetched) == {ORG_CERTS}


def test_organization_realm_refuses_a_token_of_the_platform_realm() -> None:
    platform, org = FakeCharm(kid="platform-k1"), FakeCharm(kid="org-acme-k1")
    s = platform_env(GLUONIFY_OIDC_REALM="org-acme", GLUONIFY_OIDC_ISSUER=ORG_ISS)
    v = CharmVerifier.from_env(s, fetch=org.jwks)
    with pytest.raises(InvalidTokenError, match="unknown signing key"):
        v.verify(platform.token(aud="gluonify-shop", iss=ISS))
    with pytest.raises(InvalidTokenError, match="issuer"):
        v.verify(org.token(aud="gluonify-shop", iss=ISS))  # the realm's key, but the platform's issuer


def test_organization_realm_refuses_a_token_of_another_organization() -> None:
    acme, globex = FakeCharm(kid="org-acme-k1"), FakeCharm(kid="org-acme-k1")  # same kid, other keys
    s = platform_env(GLUONIFY_OIDC_REALM="org-acme", GLUONIFY_OIDC_ISSUER=ORG_ISS)
    v = CharmVerifier.from_env(s, fetch=acme.jwks)
    with pytest.raises(InvalidTokenError, match="InvalidSignatureError"):
        v.verify(globex.token(aud="gluonify-shop", iss=ORG_ISS))


@pytest.mark.parametrize("realm", INVALID_REALMS)
def test_from_env_refuses_an_invalid_realm_without_building_a_url(realm: str) -> None:
    fetched: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        fetched.append(url)
        return {"keys": []}

    s = platform_env(GLUONIFY_OIDC_REALM=realm, GLUONIFY_OIDC_ISSUER=ORG_ISS)
    with pytest.raises(GluonifyServicesError) as e:
        CharmVerifier.from_env(s, fetch=fetch)
    assert str(e.value) == "GLUONIFY_OIDC_REALM is not a realm Charm serves (gluonify or org-<organization>)"  # never the value
    assert fetched == []


def test_from_env_never_falls_back_to_the_platform_realm_off_the_platform() -> None:
    s = GluonifyServices({"GLUONIFY_SERVICE_CHARM_URL": "http://localhost:8180", "GLUONIFY_OIDC_REALM": "org-Acme"})
    with pytest.raises(GluonifyServicesError, match="GLUONIFY_OIDC_REALM"):
        CharmVerifier.from_env(s, audience="a")


def test_from_env_without_charm() -> None:
    services = GluonifyServices({"GLUONIFY_ENVIRONMENT": "SBX"})
    with pytest.raises(GluonifyServicesError, match='did not grant the service "charm"'):
        CharmVerifier.from_env(services, audience="a")


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

    def urlopen(req: Any, timeout: float, context: Any = None) -> Response:
        seen["url"], seen["timeout"] = req.full_url, timeout
        return Response()

    monkeypatch.setattr(charm_module.urllib.request, "urlopen", urlopen)
    assert charm_module._http_fetch("https://charm/certs") == {"keys": []}
    assert seen == {"url": "https://charm/certs", "timeout": 5}
