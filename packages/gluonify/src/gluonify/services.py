"""How to reach a Gluonify service with no configuration: the address and the token source follow from where the app runs.

The resolution order mirrors the Java SDK (``io.gluonify.plasma.services.GluonifyServices``), for every service:

1. **Explicit override** (:attr:`Source.OVERRIDE`): ``GLUONIFY_SERVICE_<APP>_URL`` passed in ``overrides`` (the Python
   counterpart of a Java system property), or found in the environment *off the platform*; token ``GLUONIFY_<APP>_TOKEN``
   (fixed) or ``GLUONIFY_<APP>_TOKEN_FILE`` (re-read at every call), ``overrides`` first, then the environment.
2. **On the platform** (:attr:`Source.PLATFORM`, ``GLUONIFY_ENVIRONMENT`` present): the address injected for a service of
   ``uses`` and the application token file ``GLUONIFY_<APP>_TOKEN_FILE`` written and renewed by the node agent (re-read at
   every call). A missing service means the platform did not grant it.
3. **Otherwise**: :class:`GluonifyServicesError` saying what to do. The developer machine mode of the Java SDK
   (``gluonify login`` and the zone discovery document) is not supported yet.

Nothing is logged and no token ever appears in a message or a ``repr``.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

__all__ = [
    "ENV_ENVIRONMENT",
    "Endpoint",
    "GluonifyServices",
    "GluonifyServicesError",
    "Source",
    "defaults",
    "endpoint",
    "optional",
    "token_file_key",
    "token_key",
    "url_key",
]

ENV_ENVIRONMENT = "GLUONIFY_ENVIRONMENT"
"""Present on the platform only: the environment of the application (``SBX``, ``DEV``, ``QUA``, ``PRD``)."""

_SERVICE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SCHEMES = frozenset({"http", "https", "ws", "wss"})

TokenSource = Callable[[], str | None]


class GluonifyServicesError(RuntimeError):
    """A service cannot be reached; the message says what to do (never contains a token)."""


class Source(Enum):
    """Where a resolution comes from."""

    OVERRIDE = "override"
    """Explicit override: tests, special cases."""
    PLATFORM = "platform"
    """On the platform: address injected for a service of ``uses``, application token written by the node agent."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """How to reach one service: its base URL (no trailing slash), its token source and where the resolution comes from."""

    service: str
    url: str
    tokens: TokenSource = field(repr=False, compare=False)
    source: Source

    def token(self) -> str | None:
        """The current token, ``None`` if there is none (the service answers 401 if it requires one)."""
        t = self.tokens()
        if t is None or not t.strip():
            return None
        return t.strip()

    def resolve(self, path: str) -> str:
        """``url`` followed by ``path`` (``/v1/...``)."""
        return self.url.rstrip("/") + (path if path.startswith("/") else "/" + path)

    def headers(self) -> dict[str, str]:
        """``{"Authorization": "Bearer <token>"}``, or ``{}`` without a token."""
        t = self.token()
        return {"Authorization": f"Bearer {t}"} if t else {}


def _upper(service: str) -> str:
    return service.upper().replace("-", "_")


def url_key(service: str) -> str:
    """``crystallon`` -> ``GLUONIFY_SERVICE_CRYSTALLON_URL``; ``my-svc`` -> ``GLUONIFY_SERVICE_MY_SVC_URL``."""
    return f"GLUONIFY_SERVICE_{_upper(service)}_URL"


def token_key(service: str) -> str:
    """``crystallon`` -> ``GLUONIFY_CRYSTALLON_TOKEN``."""
    return f"GLUONIFY_{_upper(service)}_TOKEN"


def token_file_key(service: str) -> str:
    """``crystallon`` -> ``GLUONIFY_CRYSTALLON_TOKEN_FILE`` (same rule as ``ServiceToken.envName`` of the Java SDK)."""
    return token_key(service) + "_FILE"


def _blank(s: str | None) -> bool:
    return s is None or not s.strip()


def _name(service: str) -> str:
    s = service.strip().lower()
    if not _SERVICE.fullmatch(s):
        raise ValueError(f"not a service name: {service!r}")
    return s


def _url(value: str, key: str) -> str:
    v = value.strip()
    try:
        u = urlsplit(v)
    except ValueError as e:
        raise GluonifyServicesError(f"{key} is not a valid URL") from e
    if u.scheme.lower() not in _SCHEMES or not u.hostname:
        raise GluonifyServicesError(f"{key} must be an absolute http(s) URL")
    return v.rstrip("/")


def _fixed(token: str) -> TokenSource:
    t = token.strip()
    return lambda: t


def _file(path: Path, service: str) -> TokenSource:
    """Re-read at every call; a missing or empty file means no token yet (not written by the agent yet)."""

    def read() -> str | None:
        try:
            v = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:
            raise GluonifyServicesError(f"cannot read the token file of {service} {path} ({type(e).__name__})") from e
        return v or None

    return read


def _none() -> str | None:
    return None


class GluonifyServices:
    """Resolution of platform services. Thread-safe; reads the environment at every call (``os.environ`` by default).

    :param environ: the environment variables (default: ``os.environ``, read live)
    :param overrides: explicit values that win everywhere, even on the platform (the Java system properties): tests, tools
    """

    def __init__(self, environ: Mapping[str, str] | None = None, overrides: Mapping[str, str] | None = None) -> None:
        self._env: Mapping[str, str] = os.environ if environ is None else environ
        self._overrides: Mapping[str, str] = {} if overrides is None else dict(overrides)

    # ------------------------------------------------------------------ platform variables

    @property
    def environ(self) -> Mapping[str, str]:
        """The environment this instance reads (``os.environ`` by default)."""
        return self._env

    def env(self, key: str) -> str | None:
        """An environment variable, ``None`` when absent or blank."""
        v = self._env.get(key)
        return None if _blank(v) else v

    @property
    def on_platform(self) -> bool:
        """True on the platform (``GLUONIFY_ENVIRONMENT`` present)."""
        return not _blank(self._env.get(ENV_ENVIRONMENT))

    @property
    def environment(self) -> str | None:
        """``GLUONIFY_ENVIRONMENT`` (``SBX``, ``DEV``, ``QUA``, ``PRD``...), ``None`` off the platform."""
        v = self.env(ENV_ENVIRONMENT)
        return v.strip() if v else None

    @property
    def replica(self) -> str | None:
        """``GLUONIFY_REPLICA``: the replica rank (``1``, ``2``...)."""
        return self.env("GLUONIFY_REPLICA")

    @property
    def self_url(self) -> str | None:
        """``GLUONIFY_SELF_URL``: the address of this instance."""
        return self.env("GLUONIFY_SELF_URL")

    @property
    def node_address(self) -> str | None:
        """``GLUONIFY_NODE_ADDRESS``: the address of the node."""
        return self.env("GLUONIFY_NODE_ADDRESS")

    def granted(self) -> dict[str, str]:
        """The services whose address the platform injected (``GLUONIFY_SERVICE_<APP>_URL``), by lower-case name."""
        out: dict[str, str] = {}
        for k, v in self._env.items():
            if k.startswith("GLUONIFY_SERVICE_") and k.endswith("_URL") and not _blank(v):
                out[k[len("GLUONIFY_SERVICE_") : -len("_URL")].lower().replace("_", "-")] = v.strip()
        return dict(sorted(out.items()))

    # ------------------------------------------------------------------ resolution

    def resolve(self, service: str) -> Endpoint:
        """Resolves the service.

        :raises GluonifyServicesError: if the service cannot be reached; the message says what to do
        :raises ValueError: if the name is not a service name
        """
        s = _name(service)
        found = self._override(s)
        if found is not None:
            return found
        if self.on_platform:
            return self._platform(s)
        raise GluonifyServicesError(
            f"no address for the service {s!r}: on Gluonify the platform injects {url_key(s)} when the service is in "
            f'"uses" (inferred by Bup from the SDK markers, or declared in gluonify.json); on your machine set '
            f"{url_key(s)} (and {token_key(s)} or {token_file_key(s)} if the service needs a token)"
        )

    def resolve_optional(self, service: str) -> Endpoint | None:
        """Like :meth:`resolve`, but ``None`` when the service is simply not available (for an optional service)."""
        s = _name(service)
        found = self._override(s)
        if found is not None:
            return found
        if self.on_platform and not _blank(self._env.get(url_key(s))):
            return self._platform(s)
        return None

    def _override(self, s: str) -> Endpoint | None:
        key = url_key(s)
        url = self._overrides.get(key)
        if _blank(url) and not self.on_platform:
            url = self._env.get(key)
        if url is None or _blank(url):
            return None
        return Endpoint(s, _url(url, key), self._override_tokens(s), Source.OVERRIDE)

    def _override_tokens(self, s: str) -> TokenSource:
        for source in (self._overrides, self._env):
            token = source.get(token_key(s))
            if token is not None and not _blank(token):
                return _fixed(token)
            file = source.get(token_file_key(s))
            if file is not None and not _blank(file):
                return _file(Path(file.strip()), s)
        return _none

    def _platform(self, s: str) -> Endpoint:
        key = url_key(s)
        url = self._env.get(key)
        if url is None or _blank(url):
            raise GluonifyServicesError(
                f'the platform did not grant the service "{s}" to this application (no {key}): build it with Bup with '
                f'the Gluonify SDK distribution that reaches the service installed, or, for a call outside the SDK, add "{s}" '
                f'to "uses" in gluonify.json; then deploy again'
            )
        file = self._env.get(token_file_key(s))
        fixed = self._env.get(token_key(s))
        tokens: TokenSource
        if file is not None and not _blank(file):
            tokens = _file(Path(file.strip()), s)
        elif fixed is not None and not _blank(fixed):
            tokens = _fixed(fixed)
        else:
            tokens = _none  # platform without service tokens: the service answers 401 if it requires one
        return Endpoint(s, _url(url, key), tokens, Source.PLATFORM)


_defaults: GluonifyServices | None = None
_lock = threading.Lock()


def defaults() -> GluonifyServices:
    """The instance of the process (real environment), created on first call."""
    global _defaults
    d = _defaults
    if d is None:
        with _lock:
            if _defaults is None:
                _defaults = GluonifyServices()
            d = _defaults
    return d


def endpoint(service: str) -> Endpoint:
    """Shortcut of ``defaults().resolve(service)``."""
    return defaults().resolve(service)


def optional(service: str) -> Endpoint | None:
    """Shortcut of ``defaults().resolve_optional(service)``."""
    return defaults().resolve_optional(service)
