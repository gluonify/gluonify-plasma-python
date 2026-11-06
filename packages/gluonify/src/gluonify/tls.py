"""Trust of the cluster's internal authority: the system's roots plus the PEM root the platform delivers.

``GLUONIFY_CA_FILE`` (alias ``GLUONIFY_GDOWN_CA_FILE``) names the root; without it, or with a file that cannot be used, the
default context applies (logged once, never fatal). Never a context that trusts everything.
"""

from __future__ import annotations

import logging
import os
import ssl
from collections.abc import Mapping

CA_FILE_ENV = "GLUONIFY_CA_FILE"
"""Path of the PEM root of the cluster's internal authority, set by the platform on every instance."""
CA_FILE_ALIAS_ENV = "GLUONIFY_GDOWN_CA_FILE"
"""Former name of :data:`CA_FILE_ENV`, kept as an alias."""

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
