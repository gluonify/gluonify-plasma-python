"""A fake of the neo4j driver: records the statements, answers from a callback (as the Java tests fake Gdown)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Counters:
    nodes_created: int = 0
    nodes_deleted: int = 0
    relationships_created: int = 0
    relationships_deleted: int = 0
    properties_set: int = 0


@dataclass
class Summary:
    counters: Counters = field(default_factory=Counters)


@dataclass
class Result:
    records: list[Mapping[str, Any]]
    summary: Summary = field(default_factory=Summary)


class FakeDriver:
    def __init__(self, answer: Callable[[str, dict[str, Any]], Result] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.answer = answer or (lambda q, p: Result([]))
        self.closed = False
        self.fail = False

    def execute_query(self, query: str, parameters: Mapping[str, Any] | None = None, /, **kwargs: Any) -> Result:
        if self.fail:
            raise OSError("gdown down")
        p = dict(parameters or {})
        self.calls.append((query, p, kwargs.get("database_")))
        return self.answer(query, p)

    def verify_connectivity(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
