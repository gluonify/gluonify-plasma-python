"""The cluster's root as read by gluonify.tls."""

from __future__ import annotations

import ssl
import tempfile
from pathlib import Path

import pytest

from gluonify import charm, fermion, tls


@pytest.fixture(autouse=True)
def _reset() -> None:
    tls._warned[0] = False


def test_without_variable_the_default_context() -> None:
    assert tls.ca_file({}) is None
    assert tls.client_context({}).verify_mode == ssl.CERT_REQUIRED


def test_alias_and_precedence(tmp_path: Path) -> None:
    root = tmp_path / "root.pem"
    root.write_text("x")
    assert tls.ca_file({tls.CA_FILE_ALIAS_ENV: str(root)}) == str(root)
    assert tls.ca_file({tls.CA_FILE_ENV: str(root), tls.CA_FILE_ALIAS_ENV: "/nowhere"}) == str(root)


def test_unreadable_or_invalid_is_never_fatal(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert tls.ca_file({tls.CA_FILE_ENV: str(tmp_path / "missing.pem")}) is None
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    context = tls.client_context({tls.CA_FILE_ENV: str(junk)})
    assert context.check_hostname
    assert len(caplog.records) == 1, "logged once"


def test_a_root_is_added_to_the_system_ones() -> None:
    root = Path(__file__).parent / "fixtures" / "tls-root.pem"
    plain = len(ssl.create_default_context().get_ca_certs())
    context = tls.client_context({tls.CA_FILE_ENV: str(root)})
    assert len(context.get_ca_certs()) >= plain
    assert any(dict(x[0] for x in c["subject"]).get("commonName") == "Gluonify test root" for c in context.get_ca_certs())


def test_callers_use_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(req: object, timeout: float, context: object = None) -> _Response:
        seen.append(context)
        return _Response()

    monkeypatch.setattr(charm.urllib.request, "urlopen", fake_urlopen)
    charm._http_fetch("https://charm/certs")
    charm._http_fetch("http://charm/certs")
    assert isinstance(seen[0], ssl.SSLContext)
    assert seen[1] is None
    pytest.importorskip("websockets")
    assert fermion._websockets_connect("wss://fermion/ws") is not None
    assert fermion._websockets_connect("ws://fermion/ws") is not None


def _pair(tmp_path: Path) -> tuple[str, str]:
    """A self-signed EC pair made by openssl for the test (no key is kept in the repository)."""
    import shutil
    import subprocess

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=app",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert.read_text(), key.read_text()


def test_without_the_pair_plain_http() -> None:
    assert tls.server_context({}) is None
    assert tls.server_context({tls.CERTIFICATE_ENV: " ", tls.PRIVATE_KEY_ENV: ""}) is None


def test_half_a_pair_refuses_to_start() -> None:
    with pytest.raises(ValueError, match="go together"):
        tls.server_context({tls.CERTIFICATE_ENV: "-----BEGIN CERTIFICATE-----"})


def test_the_pair_gives_a_server_context(tmp_path: Path) -> None:
    import base64

    cert, key = _pair(tmp_path)
    context = tls.server_context({tls.CERTIFICATE_ENV: cert, tls.PRIVATE_KEY_ENV: base64.b64encode(key.encode()).decode()})
    assert context is not None
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert not any(p.name.startswith("gluonify-tls-") for p in Path(tempfile.gettempdir()).iterdir()), "no file left behind"


def test_values_that_are_not_pem_are_refused() -> None:
    with pytest.raises(ValueError, match="neither PEM nor base64"):
        tls.server_context({tls.CERTIFICATE_ENV: "not base64 !", tls.PRIVATE_KEY_ENV: "x"})
    with pytest.raises(ValueError, match="a PEM PRIVATE KEY block expected"):
        tls.server_context(
            {
                tls.CERTIFICATE_ENV: "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----",
                tls.PRIVATE_KEY_ENV: "-----BEGIN X-----",
            }
        )
