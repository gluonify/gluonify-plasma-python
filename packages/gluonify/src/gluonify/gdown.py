"""gDown, the platform's graph database, through the official ``neo4j`` driver over Bolt, on the dedicated database.

Zero configuration on the platform (``"uses": ["gdown"]``, inferred from the marker of this module, and a dedicated
database, ``"gdownDatabase": true``):

* database, account and password: ``GLUONIFY_GDOWN_DATABASE``, ``GLUONIFY_GDOWN_USER``, ``GLUONIFY_GDOWN_PASSWORD``
  (vault keys ``APP_GLUONIFY_GDOWN_USER`` and ``APP_GLUONIFY_GDOWN_PASSWORD`` win, as in gluonify-source);
* Bolt address: ``GLUONIFY_GDOWN_BOLT_URL`` when given, otherwise derived from ``GLUONIFY_SERVICE_GDOWN_URL``
  (``http`` -> ``bolt``, ``https`` -> ``bolt+s``, port 7687).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from .services import GluonifyServices, GluonifyServicesError, defaults

__all__ = [
    "BOLT_PORT",
    "GdownClient",
    "GdownDatabase",
    "bolt_url",
    "dedicated_database",
]

BOLT_PORT = 7687
ENV_DATABASE = "GLUONIFY_GDOWN_DATABASE"
ENV_USER = "GLUONIFY_GDOWN_USER"
ENV_PASSWORD = "GLUONIFY_GDOWN_PASSWORD"  # noqa: S105 (a variable name, not a password)
ENV_BOLT_URL = "GLUONIFY_GDOWN_BOLT_URL"


@dataclass(frozen=True, slots=True)
class GdownDatabase:
    """The dedicated database of the application and its account. ``repr`` never shows the password."""

    database: str
    user: str
    password: str = field(repr=False)


def _first(env: GluonifyServices, *keys: str) -> str | None:
    for k in keys:
        v = env.env(k)
        if v is not None:
            return v.strip()
    return None


def dedicated_database(services: GluonifyServices | None = None) -> GdownDatabase | None:
    """The dedicated database injected by the platform, ``None`` without ``GLUONIFY_GDOWN_DATABASE``.

    :raises GluonifyServicesError: when the database is given but its account or password is missing
    """
    s = services or defaults()
    db = s.env(ENV_DATABASE)
    if db is None:
        return None
    user = _first(s, "APP_" + ENV_USER, ENV_USER)
    password = s.env("APP_" + ENV_PASSWORD) or s.env(ENV_PASSWORD)
    if user is None or password is None:
        raise GluonifyServicesError(
            f"{ENV_DATABASE} is set but {ENV_USER} or {ENV_PASSWORD} is missing: deploy the application with "
            f'"gdownDatabase": true (the platform provides all three)'
        )
    return GdownDatabase(db.strip(), user, password)


def bolt_url(services: GluonifyServices | None = None) -> str:
    """The Bolt address of gDown (see the module documentation).

    :raises GluonifyServicesError: when gDown is not granted
    """
    s = services or defaults()
    explicit = s.env(ENV_BOLT_URL)
    if explicit is not None:
        u = urlsplit(explicit.strip())
        if u.scheme not in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"} or not u.hostname:
            raise GluonifyServicesError(f"{ENV_BOLT_URL} must be a bolt:// or neo4j:// URL")
        return explicit.strip().rstrip("/")
    http = urlsplit(s.resolve("gdown").url)
    scheme = "bolt+s" if http.scheme in {"https", "wss"} else "bolt"
    host = http.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{BOLT_PORT}"


class _Driver(Protocol):
    def execute_query(self, query: str, parameters: Mapping[str, Any] | None = ..., /, **kwargs: Any) -> Any: ...

    def verify_connectivity(self) -> None: ...

    def close(self) -> None: ...


class GdownClient:
    """A thin client over the ``neo4j`` driver, bound to one database. The driver is created on first use.

    :param url: the Bolt address
    :param database: the database
    :param auth: ``(user, password)``
    :param driver: an already built driver (tests)
    """

    def __init__(
        self,
        url: str,
        database: str,
        auth: tuple[str, str] | None,
        *,
        driver: _Driver | None = None,
    ) -> None:
        self.url = url
        self.database = database
        self._auth = auth
        self._driver = driver
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"GdownClient(url={self.url!r}, database={self.database!r})"

    @classmethod
    def from_env(cls, services: GluonifyServices | None = None) -> GdownClient:
        """The client of the dedicated database of the application.

        :raises GluonifyServicesError: without a dedicated database or without gDown in ``uses``
        """
        s = services or defaults()
        db = dedicated_database(s)
        if db is None:
            raise GluonifyServicesError(
                f'no dedicated gDown database ({ENV_DATABASE} is absent): deploy with "gdownDatabase": true'
            )
        return cls(bolt_url(s), db.database, (db.user, db.password))

    @classmethod
    def optional(cls, services: GluonifyServices | None = None) -> GdownClient | None:
        """Like :meth:`from_env`, ``None`` when the platform provides no dedicated database."""
        s = services or defaults()
        return cls.from_env(s) if s.env(ENV_DATABASE) is not None else None

    @property
    def driver(self) -> _Driver:
        d = self._driver
        if d is None:
            with self._lock:
                if self._driver is None:
                    import neo4j  # imported on first use: the module stays importable without a server

                    self._driver = cast(_Driver, neo4j.GraphDatabase.driver(self.url, auth=self._auth))
                d = self._driver
        assert d is not None
        return d

    def query(self, statement: str, parameters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Runs a parameterised statement on the database; the records as dictionaries."""
        result = self.driver.execute_query(statement, dict(parameters or {}), database_=self.database)
        return [dict(r) for r in result.records]

    def write(self, statement: str, parameters: Mapping[str, Any] | None = None) -> Mapping[str, int]:
        """Runs a write statement; its counters (``nodes_created``, ``nodes_deleted``...)."""
        result = self.driver.execute_query(statement, dict(parameters or {}), database_=self.database)
        counters = result.summary.counters
        return {
            "nodes_created": int(counters.nodes_created),
            "nodes_deleted": int(counters.nodes_deleted),
            "relationships_created": int(counters.relationships_created),
            "relationships_deleted": int(counters.relationships_deleted),
            "properties_set": int(counters.properties_set),
        }

    def ready(self) -> bool:
        """True when the database answers ``RETURN 1`` (readiness)."""
        try:
            self.query("RETURN 1 AS ok")
        except Exception:
            return False
        return True

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                self._driver.close()
                self._driver = None
