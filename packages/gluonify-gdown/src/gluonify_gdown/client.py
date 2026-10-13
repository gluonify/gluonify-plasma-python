"""gDown, the platform's graph database, on the dedicated database of the application.

Zero configuration on the platform (``"uses": ["gdown"]``, inferred from the marker of this distribution, and
``"gdownDatabase": true``):

* database, account and password: ``GLUONIFY_GDOWN_DATABASE``, ``GLUONIFY_GDOWN_USER``, ``GLUONIFY_GDOWN_PASSWORD``
  (vault keys ``APP_GLUONIFY_GDOWN_USER`` and ``APP_GLUONIFY_GDOWN_PASSWORD`` win, as in gluonify-source);
* **Bolt** when the platform injects ``GLUONIFY_GDOWN_BOLT_URL``: official ``neo4j`` driver, **TLS only**
  (``bolt+s://`` or ``neo4j+s://``), the server certificate verified against ``GLUONIFY_GDOWN_CA_FILE`` when given,
  otherwise against the system CAs. A plain ``bolt://`` URL is refused, except off the platform for the
  development identity (``insecure_dev=True``);
* otherwise the **HTTP API** of gDown at ``GLUONIFY_SERVICE_GDOWN_URL``: ``POST /db/<base>/query`` with
  ``{"statement", "parameters"}`` and basic authentication with the dedicated account, as ``GraphNoteStore`` of
  gluonify-source. The Bolt port is never derived from that address.

Both transports sit behind the same small interface: :meth:`GdownClient.query`, :meth:`GdownClient.write`,
:meth:`GdownClient.ready`, :meth:`GdownClient.close`.
"""

from __future__ import annotations

import base64
import json
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from gluonify import GluonifyServices, GluonifyServicesError, defaults

__all__ = [
    "ENV_BOLT_URL",
    "ENV_CA_FILE",
    "BoltTransport",
    "GdownClient",
    "GdownDatabase",
    "GdownError",
    "HttpTransport",
    "Transport",
    "dedicated_database",
]

ENV_DATABASE = "GLUONIFY_GDOWN_DATABASE"
ENV_USER = "GLUONIFY_GDOWN_USER"
ENV_PASSWORD = "GLUONIFY_GDOWN_PASSWORD"  # noqa: S105 (a variable name, not a password)
ENV_BOLT_URL = "GLUONIFY_GDOWN_BOLT_URL"
ENV_CA_FILE = "GLUONIFY_GDOWN_CA_FILE"
"""Proposed name (no platform design names it yet): the PEM file of the CA that signs gDown's Bolt certificate."""

TLS_SCHEMES = frozenset({"bolt+s", "neo4j+s"})
PLAIN_SCHEMES = frozenset({"bolt", "neo4j"})
COUNTERS = ("nodes_created", "nodes_deleted", "relationships_created", "relationships_deleted", "properties_set")


class GdownError(RuntimeError):
    """A statement failed (gDown's message, never a password)."""


@dataclass(frozen=True, slots=True)
class GdownDatabase:
    """The dedicated database of the application and its account. ``repr`` never shows the password."""

    database: str
    user: str
    password: str = field(repr=False)


def dedicated_database(services: GluonifyServices | None = None) -> GdownDatabase | None:
    """The dedicated database injected by the platform, ``None`` without ``GLUONIFY_GDOWN_DATABASE``.

    :raises GluonifyServicesError: when the database is given but its account or password is missing
    """
    s = services or defaults()
    db = s.env(ENV_DATABASE)
    if db is None:
        return None
    user = s.env("APP_" + ENV_USER) or s.env(ENV_USER)
    password = s.env("APP_" + ENV_PASSWORD) or s.env(ENV_PASSWORD)
    if user is None or password is None:
        raise GluonifyServicesError(
            f"{ENV_DATABASE} is set but {ENV_USER} or {ENV_PASSWORD} is missing: deploy the application with "
            f'"gdownDatabase": true (the platform provides all three)'
        )
    return GdownDatabase(db.strip(), user.strip(), password)


# ---------------------------------------------------------------------------------------------- transports


class Transport(Protocol):
    """What :class:`GdownClient` needs: run a statement, get the records and the counters."""

    kind: str

    def run(self, statement: str, parameters: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]: ...

    def close(self) -> None: ...


class _Driver(Protocol):
    def execute_query(self, query: str, parameters: Mapping[str, Any] | None = ..., /, **kwargs: Any) -> Any: ...

    def close(self) -> None: ...


def _bolt_driver(url: str, auth: tuple[str, str], ca_file: Path | None) -> _Driver:
    import neo4j  # imported on first use

    if ca_file is None:
        return cast(_Driver, neo4j.GraphDatabase.driver(url, auth=auth))
    # A custom CA cannot be combined with a "+s" scheme in the driver: same TLS, explicit trust.
    u = urlsplit(url)
    plain = urlunsplit((u.scheme.removesuffix("+s"), u.netloc, u.path, u.query, u.fragment))
    return cast(
        _Driver,
        neo4j.GraphDatabase.driver(plain, auth=auth, encrypted=True, trusted_certificates=neo4j.TrustCustomCAs(str(ca_file))),
    )


class BoltTransport:
    """The ``neo4j`` driver over Bolt with TLS. The driver is created on first use.

    :param factory: builds the driver (tests); default: the real driver
    """

    kind = "bolt"

    def __init__(
        self,
        url: str,
        database: str,
        auth: tuple[str, str],
        *,
        ca_file: Path | None = None,
        insecure_dev: bool = False,
        factory: Callable[[str, tuple[str, str], Path | None], _Driver] = _bolt_driver,
    ) -> None:
        u = urlsplit(url.strip())
        scheme = u.scheme.lower()
        if not u.hostname or scheme not in TLS_SCHEMES | PLAIN_SCHEMES:
            raise GluonifyServicesError(f"{ENV_BOLT_URL} must be a bolt+s:// or neo4j+s:// URL")
        if scheme in PLAIN_SCHEMES and not insecure_dev:
            raise GluonifyServicesError(
                f"{ENV_BOLT_URL} uses {scheme}:// without TLS: only bolt+s:// or neo4j+s:// are accepted "
                "(plain Bolt is allowed only for the development identity, off the platform)"
            )
        if scheme in PLAIN_SCHEMES and ca_file is not None:
            raise GluonifyServicesError(f"{ENV_CA_FILE} is set but {ENV_BOLT_URL} has no TLS")
        self.url = url.strip().rstrip("/")
        self.database = database
        self.ca_file = ca_file
        self._auth = auth
        self._factory = factory
        self._driver: _Driver | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"BoltTransport(url={self.url!r}, database={self.database!r})"

    @property
    def driver(self) -> _Driver:
        with self._lock:
            if self._driver is None:
                self._driver = self._factory(self.url, self._auth, self.ca_file)
            return self._driver

    def run(self, statement: str, parameters: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        result = self.driver.execute_query(statement, dict(parameters), database_=self.database)
        c = result.summary.counters
        return [dict(r) for r in result.records], {k: int(getattr(c, k, 0)) for k in COUNTERS}

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                self._driver.close()
                self._driver = None


Poster = Callable[[urllib.request.Request, float], bytes]


class HttpTransport:
    """gDown's HTTP API: ``POST <url>/db/<base>/query``, basic authentication. Response
    ``{"columns": [...], "rows": [[...]], "stats": {"nodesDeleted": 1, ...}}``.

    :param post: sends the request and returns the body (tests); default: ``urllib``, CA file honoured for https
    """

    kind = "http"

    def __init__(
        self,
        url: str,
        database: str,
        auth: tuple[str, str],
        *,
        ca_file: Path | None = None,
        timeout: float = 10.0,
        post: Poster | None = None,
    ) -> None:
        u = urlsplit(url.strip())
        if u.scheme not in {"http", "https"} or not u.hostname:
            raise GluonifyServicesError("GLUONIFY_SERVICE_GDOWN_URL must be an absolute http(s) URL")
        self.url = url.strip().rstrip("/")
        self.database = database
        self._auth = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode("ascii")
        self._timeout = timeout
        self._ca_file = ca_file
        self._post = post or self._urlopen

    def __repr__(self) -> str:
        return f"HttpTransport(url={self.url!r}, database={self.database!r})"

    def _urlopen(self, req: urllib.request.Request, timeout: float) -> bytes:
        context = ssl.create_default_context(cafile=str(self._ca_file)) if self._ca_file else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as r:  # noqa: S310 (scheme checked)
                body: bytes = r.read()
                return body
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise GdownError(f"gDown: HTTP {e.code} {detail}") from e
        except OSError as e:
            raise GdownError(f"gDown unreachable ({type(e).__name__})") from e

    def run(self, statement: str, parameters: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        req = urllib.request.Request(  # noqa: S310 (scheme checked in __init__)
            f"{self.url}/db/{self.database}/query",
            data=json.dumps({"statement": statement, "parameters": dict(parameters)}).encode("utf-8"),
            headers={"Authorization": self._auth, "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            doc = json.loads(self._post(req, self._timeout).decode("utf-8"))
        except ValueError as e:
            raise GdownError("gDown: the answer is not JSON") from e
        if not isinstance(doc, dict):
            raise GdownError("gDown: the answer is not a JSON object")
        columns = [str(c) for c in doc.get("columns", [])]
        rows = [dict(zip(columns, row, strict=False)) for row in doc.get("rows", []) if isinstance(row, list)]
        stats = doc.get("stats", {}) if isinstance(doc.get("stats"), dict) else {}
        camel = {k: "".join(w.capitalize() if i else w for i, w in enumerate(k.split("_"))) for k in COUNTERS}
        return rows, {k: int(stats.get(camel[k], 0) or 0) for k in COUNTERS}

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------------------------- client


class GdownClient:
    """The client of one database, over a :class:`Transport`."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def __repr__(self) -> str:
        return f"GdownClient({self.transport!r})"

    @property
    def kind(self) -> str:
        """``bolt`` or ``http``."""
        return self.transport.kind

    @classmethod
    def from_env(cls, services: GluonifyServices | None = None, *, insecure_dev: bool = False) -> GdownClient:
        """The client of the dedicated database: Bolt with TLS when ``GLUONIFY_GDOWN_BOLT_URL`` is injected, HTTP otherwise.

        :param insecure_dev: accept a plain ``bolt://`` URL (development identity only; refused on the platform)
        :raises GluonifyServicesError: without a dedicated database, or without any gDown address
        """
        s = services or defaults()
        if insecure_dev and s.on_platform:
            raise GluonifyServicesError("plain Bolt is refused on the platform (GLUONIFY_ENVIRONMENT is set)")
        db = dedicated_database(s)
        if db is None:
            raise GluonifyServicesError(
                f'no dedicated gDown database ({ENV_DATABASE} is absent): deploy with "gdownDatabase": true'
            )
        ca = s.env(ENV_CA_FILE)
        ca_file = Path(ca.strip()) if ca else None
        if ca_file is not None and not ca_file.is_file():
            raise GluonifyServicesError(f"{ENV_CA_FILE} names a file that does not exist: {ca_file}")
        auth = (db.user, db.password)
        bolt = s.env(ENV_BOLT_URL)
        if bolt is not None:
            return cls(BoltTransport(bolt, db.database, auth, ca_file=ca_file, insecure_dev=insecure_dev))
        return cls(HttpTransport(s.resolve("gdown").url, db.database, auth, ca_file=ca_file))

    @classmethod
    def optional(cls, services: GluonifyServices | None = None, *, insecure_dev: bool = False) -> GdownClient | None:
        """Like :meth:`from_env`, ``None`` when the platform provides no dedicated database."""
        s = services or defaults()
        return cls.from_env(s, insecure_dev=insecure_dev) if s.env(ENV_DATABASE) is not None else None

    def query(self, statement: str, parameters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Runs a parameterised statement; the records as dictionaries (keys = the ``RETURN`` aliases)."""
        return self.transport.run(statement, parameters or {})[0]

    def write(self, statement: str, parameters: Mapping[str, Any] | None = None) -> dict[str, int]:
        """Runs a write statement; its counters (``nodes_created``, ``nodes_deleted``...)."""
        return self.transport.run(statement, parameters or {})[1]

    def ready(self) -> bool:
        """True when the database answers ``RETURN 1`` (readiness)."""
        try:
            self.query("RETURN 1 AS ok")
        except Exception:
            return False
        return True

    def close(self) -> None:
        self.transport.close()
