"""Trust of the cluster's internal authority, and the server TLS of an application that opts in.

``GLUONIFY_CA_FILE`` (alias ``GLUONIFY_GDOWN_CA_FILE``) names the root; without it, or with a file that cannot be used, the
default context applies (logged once, never fatal). Never a context that trusts everything.

``GLUONIFY_TLS_CERTIFICATE_PEM`` / ``GLUONIFY_TLS_PRIVATE_KEY_PEM`` (internal TLS, stage 2: the control plane hands them
over when the specification sets ``tls: true``) give :func:`server_context`, which serves the application's port over
HTTPS only (``gluonify.fastapi.serve``).
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import ssl
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

CA_FILE_ENV = "GLUONIFY_CA_FILE"
"""Path of the PEM root of the cluster's internal authority, set by the platform on every instance."""
CA_FILE_ALIAS_ENV = "GLUONIFY_GDOWN_CA_FILE"
"""Former name of :data:`CA_FILE_ENV`, kept as an alias."""

CERTIFICATE_ENV = "GLUONIFY_TLS_CERTIFICATE_PEM"
"""Certificate chain of the application's port (PEM, or its base64), issued by the cluster's internal authority."""
PRIVATE_KEY_ENV = "GLUONIFY_TLS_PRIVATE_KEY_PEM"
"""Private key of that certificate (PKCS#8 PEM, or its base64); never written to a disk."""

_LOG = logging.getLogger(__name__)
_warned: list[bool] = [False]


def ca_file(environ: Mapping[str, str] | None = None) -> str | None:
    """The readable root file named by ``environ`` (the process environment by default), or ``None``."""
    env = os.environ if environ is None else environ
    value = (env.get(CA_FILE_ENV) or "").strip() or (env.get(CA_FILE_ALIAS_ENV) or "").strip()
    if not value:
        return None
    if not os.access(value, os.R_OK):
        _warn(value, "unreadable")
        return None
    return value


def client_context(environ: Mapping[str, str] | None = None) -> ssl.SSLContext:
    """A client TLS context: the system's roots plus the cluster's root when the platform delivers one."""
    path = ca_file(environ)
    context = ssl.create_default_context()
    if path is not None:
        try:
            context.load_verify_locations(cafile=path)
        except OSError as e:  # ssl.SSLError is an OSError
            _warn(path, type(e).__name__)
            return ssl.create_default_context()
    return context


def _warn(path: str, why: str) -> None:
    if not _warned[0]:
        _warned[0] = True
        _LOG.warning("the cluster's root %s cannot be used (%s), the default authorities apply", path, why)


def server_context(environ: Mapping[str, str] | None = None) -> ssl.SSLContext | None:
    """The server TLS context of the platform's pair, or ``None`` when the application serves plain HTTP.

    Half a pair, or a value that is neither PEM nor the base64 of PEM, raises :class:`ValueError`: the application
    must not start on plain HTTP when the platform announces it over HTTPS.
    """
    env = os.environ if environ is None else environ
    certificate = (env.get(CERTIFICATE_ENV) or "").strip()
    key = (env.get(PRIVATE_KEY_ENV) or "").strip()
    if not certificate and not key:
        return None
    if not certificate or not key:
        raise ValueError(f"{CERTIFICATE_ENV} and {PRIVATE_KEY_ENV} go together")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with (
        _readable(_pem(certificate, "CERTIFICATE", CERTIFICATE_ENV)) as c,
        _readable(_pem(key, "PRIVATE KEY", PRIVATE_KEY_ENV)) as k,
    ):
        context.load_cert_chain(certfile=c, keyfile=k)
    return context


def _pem(value: str, kind: str, name: str) -> str:
    """The PEM text of a value given as PEM or as the base64 of the PEM."""
    text = value
    if "-----BEGIN " not in value:
        try:
            text = base64.b64decode("".join(value.split()), validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError) as e:
            raise ValueError(f"{name}: neither PEM nor base64") from e
    if f"{kind}-----" not in text:
        raise ValueError(f"{name}: a PEM {kind} block expected")
    return text


@contextmanager
def _readable(text: str) -> Iterator[str]:
    """A path ``ssl`` can read ``text`` from, for the time of the block.

    Linux: an anonymous memory file (``memfd``), so the private key never reaches a disk. Elsewhere (macOS and Windows,
    development platforms): an owner-only file in a private temporary folder, removed when the block ends.
    """
    memfd = getattr(os, "memfd_create", None)
    if memfd is not None:  # pragma: no cover - Linux only (the tests run on every platform)
        fd = memfd("gluonify-tls", 0)
        try:
            os.write(fd, text.encode("ascii"))
            yield f"/proc/self/fd/{fd}"
        finally:
            os.close(fd)
        return
    with tempfile.TemporaryDirectory(prefix="gluonify-tls-") as folder:
        path = Path(folder) / "pem"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(text)
        yield str(path)
