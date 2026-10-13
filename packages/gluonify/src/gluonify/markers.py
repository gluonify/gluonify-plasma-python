"""Reference reader of the ``uses`` markers, exactly as Bup reads them (contract: README, "The uses markers").

Bup never imports anything: it reads, in the ``site-packages`` folder of the venv it built (``pip install
--require-hashes`` of the lock file, without the development dependencies), every ``*.dist-info`` folder whose
distribution is trusted (``Name`` of ``METADATA`` = ``gluonify`` or ``gluonify-*`` after PEP 503 normalisation), and in
its ``entry_points.txt`` the section ``[gluonify.uses]``: each entry name is a service. Invalid names and ``higgs`` are
ignored. The services found are added to the declared ``uses`` as ``?name`` (optional).
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GROUP", "Inference", "infer", "is_trusted", "normalize"]

GROUP = "gluonify.uses"
_SERVICE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_REFUSED = frozenset({"higgs"})


def normalize(name: str) -> str:
    """PEP 503 normalisation: ``Gluonify_FastAPI`` -> ``gluonify-fastapi``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def is_trusted(name: str, trusted: tuple[str, ...] = ("gluonify",)) -> bool:
    n = normalize(name)
    return any(n == t or n.startswith(t + "-") for t in trusted)


@dataclass(frozen=True, slots=True)
class Inference:
    """``services``: sorted bare names; ``sources``: for each service, the distributions carrying its marker;
    ``ignored``: ``(distribution, entry, reason)`` for the journal."""

    services: tuple[str, ...]
    sources: dict[str, tuple[str, ...]]
    ignored: tuple[tuple[str, str, str], ...]

    def uses(self, declared: list[str] | None = None) -> list[str]:
        """The merge of Bup: the declared services as they are, then the inferred ones not declared, as ``?name``."""
        out = list(declared or [])
        bare = {d.lstrip("?") for d in out}
        out += [f"?{s}" for s in self.services if s not in bare]
        return out


def _name(dist_info: Path) -> str | None:
    meta = dist_info / "METADATA"
    if not meta.is_file():
        return None
    for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            break  # end of the headers
        if line.lower().startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def infer(site_packages: Path, trusted: tuple[str, ...] = ("gluonify",)) -> Inference:
    """Reads the markers of the trusted distributions installed in ``site_packages``."""
    sources: dict[str, set[str]] = {}
    ignored: list[tuple[str, str, str]] = []
    for d in sorted(site_packages.glob("*.dist-info")):
        name = _name(d)
        if name is None:
            continue
        ep = d / "entry_points.txt"
        if not ep.is_file():
            continue
        parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
        parser.optionxform = str  # type: ignore[assignment,method-assign]
        try:
            parser.read_string(ep.read_text(encoding="utf-8"))
        except configparser.Error:
            ignored.append((name, "", "unreadable entry_points.txt"))
            continue
        if not parser.has_section(GROUP):
            continue
        dist = normalize(name)
        for entry in parser.options(GROUP):
            if not is_trusted(name, trusted):
                ignored.append((dist, entry, "untrusted distribution"))
            elif not _SERVICE.fullmatch(entry) or entry in _REFUSED:
                ignored.append((dist, entry, "invalid service name"))
            else:
                sources.setdefault(entry, set()).add(dist)
    return Inference(
        tuple(sorted(sources)),
        {k: tuple(sorted(v)) for k, v in sorted(sources.items())},
        tuple(ignored),
    )
