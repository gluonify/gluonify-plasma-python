"""Configuration and secrets of the application, kept in Top (the vault) and injected by the node agent.

The application never calls Top: Strange reads the namespace of the application from Top before the start and writes its
keys into the environment of the process. Two shapes, exactly as the Java applications see them:

* ``"top": true`` in the app spec (namespace ``<uuid>.app``): keys arrive with the ``APP_`` prefix
  (vault key ``WEBHOOK_KEY`` -> variable ``APP_WEBHOOK_KEY``; Quarkus reads it as ``${app.webhook.key}``);
* a literal ``topNamespace``: keys arrive unprefixed (``WEBHOOK_KEY``).

:meth:`Top.get` looks for the prefixed name first, then the bare one, so the same code works in both cases.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

__all__ = ["Secret", "Top", "TopKeyMissingError", "env_name"]

PREFIX = "APP_"


class TopKeyMissingError(KeyError):
    """A required key is not in the environment; the message names the variables looked for."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class Secret:
    """A secret value: never shown by ``repr`` or ``str``; read it with :meth:`reveal`."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


def env_name(key: str) -> str:
    """``webhook.key`` -> ``WEBHOOK_KEY`` (the MicroProfile Config rule: every non-alphanumeric character becomes ``_``)."""
    return re.sub(r"[^A-Za-z0-9]", "_", key.strip()).upper()


class Top:
    """Reads the keys of the Top namespace of the application from the environment.

    :param environ: the environment (default: ``os.environ``, read live)
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._env: Mapping[str, str] = os.environ if environ is None else environ

    def names(self, key: str) -> tuple[str, str]:
        """The two variables looked for, in order: ``APP_<KEY>`` then ``<KEY>``."""
        n = env_name(key)
        return (PREFIX + n, n)

    def get(self, key: str, default: str | None = None) -> str | None:
        """The value of ``key`` (``webhook.key`` or ``WEBHOOK_KEY``), ``default`` when absent or blank."""
        for n in self.names(key):
            v = self._env.get(n)
            if v is not None and v.strip():
                return v
        return default

    def require(self, key: str) -> str:
        """The value of ``key``; :class:`TopKeyMissingError` when absent."""
        v = self.get(key)
        if v is None:
            a, b = self.names(key)
            raise TopKeyMissingError(
                f"the configuration key {key!r} is missing: put it in the Top namespace of the application "
                f'(read as {a} with "top": true, or {b} with a literal topNamespace)'
            )
        return v

    def secret(self, key: str) -> Secret | None:
        """Like :meth:`get`, wrapped in a :class:`Secret` that never prints its value."""
        v = self.get(key)
        return None if v is None else Secret(v)

    def require_secret(self, key: str) -> Secret:
        return Secret(self.require(key))
