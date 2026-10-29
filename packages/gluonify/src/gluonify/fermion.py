"""Fermion node client: the twin of the Java SDK's ``FermionMsg`` and of ``fermion-client``.

The messages are frozen dataclasses; :func:`to_json` writes them exactly as Jackson does on the Java side (field order, ``t``
discriminator, absent values left out, sorted slot maps), so the fixtures of ``gluonify-plasma/sdk/src/test/resources/fermion``
read and write back byte for byte in both languages. :class:`FermionNode` (extra ``gluonify[fermion]``, which adds ``websockets``)
connects a Python process to gluonify-fermion as a node: ``auth`` then ``hello``, the tasks run concurrently with a heartbeat,
``cancel`` reaches the running handler, ``done`` and ``error`` reach the callbacks, ``start`` frames wait in a bounded
queue during an outage, and the node reconnects by itself. A task's frames (``progress``, ``result``) go on the connection that is
open when they are sent: a task that outlives its connection delivers them after the reconnection, never on the closed one.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields
from datetime import UTC, datetime, time, timedelta
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

PROTOCOL = 1
"""Protocol version announced in :class:`Auth` (unchanged by the additive frames ``done`` and ``cancel``)."""

INTERRUPTED = "interrupted"
"""``error_kind`` of the :class:`Result` that acknowledges a cancelled task."""

BUSY = "busy"
"""``error_kind`` of a node that cannot take the task now: fermion gives it to another node without counting an attempt."""

DEFAULT_ZONE = "Europe/Paris"
"""Zone of a :class:`Window` that names none."""

_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_JSON = "json"
_CORRELATION_ID = "correlationId"


class FermionProtocolError(ValueError):
    """A frame that is not a valid fermion message (unknown type, malformed field)."""


def _camel(name: str) -> str:
    head, *rest = name.rstrip("_").split("_")
    return head + "".join(part.capitalize() for part in rest)


def _key(f: Any) -> str:
    return str(f.metadata.get(_JSON) or _camel(f.name))


def _plain(value: Any) -> Any:
    if isinstance(value, _Record):
        return value.to_dict()
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _sorted(values: Mapping[str, int] | None) -> dict[str, int] | None:
    return None if not values else dict(sorted(values.items()))


class _Record:
    """Common JSON form: fields in declaration order, ``None`` left out (Jackson ``NON_NULL``) unless :attr:`_keep_null`."""

    _keep_null: ClassVar[bool] = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            if value is None and not self._keep_null:
                continue
            out[_key(f)] = _plain(value)
        return out

    @classmethod
    def _kwargs(cls, data: Mapping[str, Any]) -> dict[str, Any]:
        """The known fields of ``data`` (unknown ones ignored, as ``@JsonIgnoreProperties(ignoreUnknown = true)``)."""
        return {f.name: data[_key(f)] for f in fields(cls) if _key(f) in data}  # type: ignore[arg-type]


# ----- descriptors -----


@dataclass(frozen=True, slots=True)
class Translation(_Record):
    """A localized text: ``language`` is ``EN``, ``FR``, ``ES``, ``IT`` or ``DE`` (Java writes both fields, even null)."""

    _keep_null: ClassVar[bool] = True
    language: str | None
    text: str | None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Translation:
        return cls(data.get("language"), data.get("text"))


def translations(en: str, fr: str, es: str, it: str, de: str) -> list[Translation]:
    """The five translations of a text, in the order en, fr, es, it, de (as ``SkillHandler.translations`` in Java)."""
    return [Translation(lang, text) for lang, text in zip(("EN", "FR", "ES", "IT", "DE"), (en, fr, es, it, de), strict=True)]


@dataclass(frozen=True, slots=True)
class SkillParam(_Record):
    """An input or output of a skill."""

    name: str
    type: str
    required: bool = True
    store: bool = True
    doc_url: str | None = None
    enum_values: list[str] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SkillParam:
        kw = cls._kwargs(data)
        kw.setdefault("name", None)
        kw.setdefault("type", None)
        kw.setdefault("required", False)
        kw.setdefault("store", False)
        return cls(**kw)


def _translations(data: Any) -> list[Translation] | None:
    return None if data is None else [Translation.from_dict(t) for t in data]


def _params(data: Any) -> list[SkillParam] | None:
    return None if data is None else [SkillParam.from_dict(p) for p in data]


@dataclass(frozen=True, slots=True)
class SkillDescriptor(_Record):
    """What a node can do. ``requires``: slots of the node's capacity one task holds (empty is ``None``, sorted by name)."""

    id: str
    translations: list[Translation] | None = None
    inputs: list[SkillParam] | None = None
    outputs: list[SkillParam] | None = None
    expected_duration: str | None = None
    requires: dict[str, int] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "requires", _sorted(self.requires))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SkillDescriptor:
        kw = cls._kwargs(data)
        kw.setdefault("id", None)
        kw["translations"] = _translations(kw.get("translations"))
        kw["inputs"] = _params(kw.get("inputs"))
        kw["outputs"] = _params(kw.get("outputs"))
        return cls(**kw)


@dataclass(frozen=True, slots=True)
class WorkflowDescriptor(_Record):
    """A workflow a node submits."""

    id: str
    translations: list[Translation] | None = None
    inputs: list[SkillParam] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkflowDescriptor:
        return cls(data.get("id"), _translations(data.get("translations")), _params(data.get("inputs")))  # type: ignore[arg-type]


def _time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"window time '{value}' is not HH:mm") from e
    if len(value) != 5:
        raise ValueError(f"window time '{value}' is not HH:mm")
    return parsed


def _day(value: str) -> str:
    code = (value or "").strip().upper()
    if code not in _DAYS:
        raise ValueError(f"window day '{value}' is not one of MON, TUE, WED, THU, FRI, SAT, SUN")
    return code


@dataclass(frozen=True, slots=True)
class Window(_Record):
    """A daily window in local time of ``zone``: ``from_`` included to ``to`` excluded, crossing midnight when ``to`` is not after
    ``from_``, the whole day when they are equal; ``days`` are the days on which it starts (empty: every day)."""

    from_: str = field(metadata={_JSON: "from"})
    to: str
    days: list[str] = field(default_factory=list)
    zone: str = DEFAULT_ZONE

    def __post_init__(self) -> None:
        _time(self.from_)
        _time(self.to)
        object.__setattr__(self, "days", [_day(d) for d in self.days or []])
        zone = self.zone if self.zone and self.zone.strip() else DEFAULT_ZONE
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"window zone '{zone}' is unknown") from e
        object.__setattr__(self, "zone", zone)

    @classmethod
    def parse(cls, text: str | None, zone: str | None = None) -> Window:
        """The text form ``[MON,TUE ]HH:mm-HH:mm``."""
        spec = (text or "").strip()
        days, _, rng = spec.rpartition(" ") if " " in spec else ("", "", spec)
        start, dash, end = rng.strip().partition("-")
        if not dash:
            raise ValueError(f"window '{spec}' is not [DAYS ]HH:mm-HH:mm")
        return cls(start, end, days.split(",") if days else [], zone or DEFAULT_ZONE)

    def contains(self, instant: datetime) -> bool:
        """Whether the window covers ``instant`` (an aware datetime)."""
        local = instant.astimezone(ZoneInfo(self.zone))
        now, start, end = local.time(), _time(self.from_), _time(self.to)
        today = _DAYS[local.weekday()]
        yesterday = _DAYS[(local.weekday() - 1) % 7]
        if start < end:
            return self._starts(today) and start <= now < end
        return (self._starts(today) and now >= start) or (self._starts(yesterday) and now < end)

    def _starts(self, day: str) -> bool:
        return not self.days or day in self.days

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"from": self.from_, "to": self.to}
        if self.days:
            out["days"] = list(self.days)
        out["zone"] = self.zone
        return out

    def __str__(self) -> str:
        return (",".join(self.days) + " " if self.days else "") + f"{self.from_}-{self.to}"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Window:
        return cls(data.get("from"), data.get("to"), list(data.get("days") or []), data.get("zone") or DEFAULT_ZONE)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class NodeCapacity(_Record):
    """Concurrent tasks per resource name (sorted) and the windows during which the node takes no new task."""

    slots: dict[str, int] = field(default_factory=dict)
    closed: list[Window] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "slots", _sorted(self.slots) or {})
        object.__setattr__(self, "closed", list(self.closed or []))

    @property
    def empty(self) -> bool:
        """Neither slots nor windows: fermion ignores the capacity."""
        return not self.slots and not self.closed

    def closed_at(self, instant: datetime) -> bool:
        """Whether one of the windows covers ``instant``."""
        return any(w.contains(instant) for w in self.closed)

    @classmethod
    def of(cls, slots: Mapping[str, int], windows: Sequence[str] = (), zone: str | None = None) -> NodeCapacity:
        """The configuration form: slot counts (zero or more) and window texts."""
        negative = sorted(name for name, count in slots.items() if count is None or count < 0)
        if negative:
            raise ValueError(f"capacity slots {negative} must be zero or more")
        return cls(dict(slots), [Window.parse(w, zone) for w in windows])

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NodeCapacity:
        return cls(dict(data.get("slots") or {}), [Window.from_dict(w) for w in data.get("closed") or []])


# ----- messages -----


@dataclass(frozen=True, slots=True)
class Auth(_Record):
    """First frame of the node: Charm token (audience ``gluonify-fermion``) and protocol version."""

    T: ClassVar[str] = "auth"
    token: str = field(repr=False)
    protocol: int = PROTOCOL


@dataclass(frozen=True, slots=True)
class Hello(_Record):
    """Announcement of the node: skills, workflows used, OpenAPI address, optional capacity (empty is ``None``)."""

    T: ClassVar[str] = "hello"
    app_id: str
    node_id: str
    skills: list[SkillDescriptor] = field(default_factory=list)
    used_workflows: list[WorkflowDescriptor] = field(default_factory=list)
    open_api_url: str | None = None
    capacity: NodeCapacity | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "skills", list(self.skills or []))
        object.__setattr__(self, "used_workflows", list(self.used_workflows or []))
        if self.capacity is not None and self.capacity.empty:
            object.__setattr__(self, "capacity", None)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Hello:
        kw = cls._kwargs(data)
        kw.setdefault("app_id", None)
        kw.setdefault("node_id", None)
        kw["skills"] = [SkillDescriptor.from_dict(s) for s in kw.get("skills") or []]
        kw["used_workflows"] = [WorkflowDescriptor.from_dict(w) for w in kw.get("used_workflows") or []]
        if kw.get("capacity") is not None:
            kw["capacity"] = NodeCapacity.from_dict(kw["capacity"])
        return cls(**kw)


def _payload(self: Any) -> None:
    object.__setattr__(self, "payload", dict(self.payload) if self.payload is not None else {})


@dataclass(frozen=True, slots=True)
class Start(_Record):
    """Submits a workflow (node -> fermion)."""

    T: ClassVar[str] = "start"
    workflow: str
    correlation_id: str
    global_transaction_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    organization: str | None = None

    def __post_init__(self) -> None:
        _payload(self)


@dataclass(frozen=True, slots=True)
class Command(_Record):
    """Order to run a task (fermion -> node): ``action`` is the skill, ``step_id`` the BPMN node."""

    T: ClassVar[str] = "command"
    correlation_id: str
    global_transaction_id: str | None
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    step_id: str | None = None

    def __post_init__(self) -> None:
        _payload(self)


@dataclass(frozen=True, slots=True)
class Result(_Record):
    """End of a task: success with a payload, or failure with a message and a machine-readable kind."""

    T: ClassVar[str] = "result"
    correlation_id: str
    global_transaction_id: str | None
    action: str
    success: bool
    error: str | None = None
    error_kind: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    step_id: str | None = None

    def __post_init__(self) -> None:
        _payload(self)

    @classmethod
    def ok(cls, command: Command, payload: Mapping[str, Any] | None = None) -> Result:
        """Success in response to ``command``."""
        return cls(
            command.correlation_id,
            command.global_transaction_id,
            command.action,
            True,
            None,
            None,
            dict(payload or {}),
            command.step_id,
        )

    @classmethod
    def failure(cls, command: Command, error: str | None, error_kind: str | None = None) -> Result:
        """Failure in response to ``command``."""
        return cls(
            command.correlation_id, command.global_transaction_id, command.action, False, error, error_kind, {}, command.step_id
        )


@dataclass(frozen=True, slots=True)
class Progress(_Record):
    """Heartbeat of a running task, with an optional structured event ``data`` (empty is ``None``)."""

    T: ClassVar[str] = "progress"
    correlation_id: str
    global_transaction_id: str | None
    action: str
    step_id: str | None = None
    message: str | None = None
    data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.data:
            object.__setattr__(self, "data", None)

    @classmethod
    def of(cls, command: Command, message: str | None = None, data: Mapping[str, Any] | None = None) -> Progress:
        """Progress of ``command``."""
        return cls(
            command.correlation_id,
            command.global_transaction_id,
            command.action,
            command.step_id,
            message,
            dict(data) if data else None,
        )


@dataclass(frozen=True, slots=True)
class Done(_Record):
    """Successful end of a workflow this node submitted (fermion -> node): its final variables."""

    T: ClassVar[str] = "done"
    correlation_id: str
    global_transaction_id: str | None
    workflow: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _payload(self)


@dataclass(frozen=True, slots=True)
class Cancel(_Record):
    """Order to stop the running task (fermion -> node); ``resumable`` asks the node to keep its checkpoint."""

    T: ClassVar[str] = "cancel"
    correlation_id: str
    step_id: str | None
    action: str
    reason: str | None = None
    resumable: bool = False


@dataclass(frozen=True, slots=True)
class Error(_Record):
    """Refusal: connection (no ``correlation_id``) or workflow (missing, inactive, abandoned)."""

    T: ClassVar[str] = "error"
    correlation_id: str | None = None
    message: str | None = None


FermionMsg = Auth | Hello | Start | Command | Result | Progress | Done | Cancel | Error
_TYPES: dict[str, type[Any]] = {c.T: c for c in (Auth, Hello, Start, Command, Result, Progress, Done, Cancel, Error)}


def to_json(msg: FermionMsg) -> str:
    """The frame as Jackson writes it: ``t`` first, compact, UTF-8 characters kept."""
    return json.dumps({"t": msg.T, **msg.to_dict()}, separators=(",", ":"), ensure_ascii=False)


def from_json(text: str | bytes) -> FermionMsg:
    """Reads a frame; unknown fields are ignored, an unknown type raises :class:`FermionProtocolError`."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise FermionProtocolError(f"frame is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise FermionProtocolError("frame is not a JSON object")
    cls = _TYPES.get(str(data.get("t")))
    if cls is None:
        raise FermionProtocolError(f"unknown frame type {data.get('t')!r}")
    try:
        if cls is Hello:
            return Hello.from_dict(data)
        kw = cls._kwargs(data)
        for f in fields(cls):
            # An absent field reads as Jackson does: false for a boolean, null otherwise.
            kw.setdefault(f.name, f.default if f.default is not MISSING else _absent(f.type))
        return cls(**kw)  # type: ignore[no-any-return]
    except (TypeError, ValueError, AttributeError) as e:
        raise FermionProtocolError(f"malformed {cls.T} frame: {e}") from e


# ----- node -----


class SkillFailure(Exception):
    """A failure with a machine-readable kind (``INTERRUPTED``, ``BUSY``, ``STORAGE_UNAVAILABLE``…)."""

    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


class _Sender(Protocol):
    async def send(self, message: str) -> None: ...


@dataclass
class TaskContext:
    """What a handler receives: the command, a way to report progress, and the cancellation when fermion sends one."""

    command: Command
    _send: Callable[[FermionMsg], Awaitable[None]] = field(repr=False)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    cancel: Cancel | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return self.command.payload

    async def progress(self, data: Mapping[str, Any]) -> None:
        """Sends a structured progress event (phase, tool call, cost so far; never a secret). An empty event is not sent."""
        if data:
            await self._send(Progress.of(self.command, None, data))


class SkillHandler(ABC):
    """A skill the node runs. Subclass it, set :attr:`action` and implement :meth:`handle`."""

    action: ClassVar[str]
    inputs: ClassVar[Sequence[SkillParam]] = ()
    outputs: ClassVar[Sequence[SkillParam]] = ()
    descriptions: ClassVar[Sequence[Translation]] = ()
    expected_duration: ClassVar[timedelta | None] = None
    requires: ClassVar[Mapping[str, int]] = {}

    @abstractmethod
    async def handle(self, context: TaskContext) -> Mapping[str, Any] | None:
        """Runs the task; raise :class:`SkillFailure` for a failure with a kind."""

    # A no-op default, as in Java: a handler overrides it when it can stop its work.
    async def on_cancel(self, context: TaskContext, cancel: Cancel) -> None:  # noqa: B027
        """Called when fermion cancels the running task; ``context.cancelled`` is already set. The default does nothing."""

    def descriptor(self) -> SkillDescriptor:
        """The ``hello`` form of the skill."""
        duration = None if self.expected_duration is None else _iso(self.expected_duration)
        return SkillDescriptor(
            self.action, list(self.descriptions), list(self.inputs), list(self.outputs), duration, dict(self.requires)
        )


def _iso(delta: timedelta) -> str:
    """ISO-8601 duration as Java's ``Duration.toString`` (``PT2H``, ``PT90S``)."""
    total = int(delta.total_seconds())
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    out = "PT" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "") + (f"{seconds}S" if seconds else "")
    return out if out != "PT" else "PT0S"


class FermionQueueFullError(RuntimeError):
    """The outage lasts and the queue of ``start`` frames is full."""


def _absent(kind: object) -> bool | None:
    """The value of an absent field, as Jackson reads it: false for a boolean, null otherwise."""
    return False if kind == "bool" else None


_SECURE = {"https": "wss", "wss": "wss"}
_PLAIN = {"http": "ws", "ws": "ws"}


def _internal(host: str) -> bool:
    """Loopback, private or link-local address (the platform's internal network), or ``localhost``."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def ws_url(base: str, app_id: str, node_id: str) -> str:
    """The node URL: ``http(s)``/``ws(s)`` base without a path, then ``/ws/fermion/<app>-<node>``.

    The token of the first frame must never travel in clear outside the platform's network: ``https``/``wss`` are always
    accepted, ``http``/``ws`` only toward a loopback, private or link-local address (the internal service URLs the
    platform injects, a laboratory, the tests); any other plaintext URL raises :class:`ValueError`.
    """
    parts = urlsplit(base.rstrip("/"))
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if scheme in _SECURE:
        ws = _SECURE[scheme]
    elif scheme in _PLAIN:
        if not _internal(host):
            raise ValueError(
                f"fermion URL '{base}': plaintext is accepted only toward a loopback or private address, use https://"
            )
        ws = _PLAIN[scheme]
    else:
        raise ValueError(f"fermion URL '{base}' is not http(s):// nor ws(s)://")
    return f"{ws}://{parts.netloc}{parts.path}/ws/fermion/{app_id}-{node_id}"


Connector = Callable[[str], Any]
"""Opens a WebSocket: returns an async context manager yielding an object with ``send``, ``close`` and async iteration."""


def _websockets_connect(url: str) -> Any:
    try:
        from websockets.asyncio.client import connect
    except ImportError as e:  # pragma: no cover - depends on the installed extra
        raise ImportError('FermionNode needs the extra: pip install "gluonify[fermion]"') from e
    return connect(url, max_size=80 * 1024 * 1024, ping_interval=20)


class FermionNode:
    """A Python process as a fermion node. Call :meth:`run` in a task; :meth:`stop` ends it."""

    def __init__(
        self,
        url: str,
        app_id: str,
        node_id: str,
        token: Callable[[], str | None],
        handlers: Sequence[SkillHandler] = (),
        *,
        used_workflows: Sequence[WorkflowDescriptor] = (),
        capacity: NodeCapacity | None = None,
        open_api_url: str | None = None,
        reconnect_interval: float = 10.0,
        heartbeat_interval: float = 60.0,
        queue_size: int = 100,
        connector: Connector = _websockets_connect,
    ) -> None:
        self.url = ws_url(url, app_id, node_id)
        self.app_id = app_id
        self.node_id = node_id
        self._token = token
        self._handlers = {h.action: h for h in handlers}
        self._used = list(used_workflows)
        self._capacity = capacity
        self._open_api_url = open_api_url
        self._reconnect = reconnect_interval
        self._heartbeat = heartbeat_interval
        self._queue_size = queue_size
        self._connect = connector
        self._pending: list[Start] = []
        self._outbox: list[FermionMsg] = []
        self._ws: Any = None
        self._running: dict[tuple[str, str | None], tuple[SkillHandler, TaskContext]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._on_done: Callable[[str, dict[str, Any]], Any] | None = None
        self._on_error: Callable[[str | None, str | None], Any] | None = None
        self.sessions = 0

    def __repr__(self) -> str:
        return f"FermionNode(url={self.url!r}, skills={sorted(self._handlers)})"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def outbox_count(self) -> int:
        """The task frames (``progress``, ``result``) waiting for a connection."""
        return len(self._outbox)

    def on_done(self, callback: Callable[[str, dict[str, Any]], Any] | None) -> None:
        """Callback (correlation id, final variables) of a submitted workflow's successful end; may be a coroutine function."""
        self._on_done = callback

    def on_error(self, callback: Callable[[str | None, str | None], Any] | None) -> None:
        """Callback (correlation id, message) of a refusal or an abandon; may be a coroutine function."""
        self._on_error = callback

    def hello(self) -> Hello:
        """The announcement sent after ``auth``."""
        skills = [h.descriptor() for h in sorted(self._handlers.values(), key=lambda h: h.action)]
        return Hello(self.app_id, self.node_id, skills, self._used, self._open_api_url, self._capacity)

    async def start_workflow(
        self,
        workflow: str,
        correlation_id: str,
        payload: Mapping[str, Any] | None = None,
        global_transaction_id: str | None = None,
        organization: str | None = None,
    ) -> None:
        """Submits a workflow; during an outage the frame waits in the bounded queue (full: :class:`FermionQueueFullError`)."""
        start = Start(workflow, correlation_id, global_transaction_id, dict(payload or {}), organization)
        if self._ws is not None and not self._pending:
            try:
                await self._ws.send(to_json(start))
                return
            except Exception as e:
                log.warning("fermion-node=send correlationId=%s error=%s, queued", correlation_id, e)
        if len(self._pending) >= self._queue_size:
            raise FermionQueueFullError(f"fermion is unreachable and {self._queue_size} start frames already wait")
        self._pending.append(start)

    async def stop(self) -> None:
        """Ends :meth:`run`: closes the connection; running tasks are cancelled."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            await ws.close()
        for task in self._tasks:
            task.cancel()

    async def run(self) -> None:
        """Connects, serves, reconnects every ``reconnect_interval`` seconds, until :meth:`stop`."""
        while not self._stop.is_set():
            try:
                async with self._connect(self.url) as ws:
                    await self._session(ws)
            except Exception as e:
                log.warning("fermion-node=connect url=%s error=%s", self.url, e)
            finally:
                self._ws = None
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), self._reconnect)
            except TimeoutError:
                continue

    async def _session(self, ws: Any) -> None:
        token = self._token()
        if not token:
            raise RuntimeError("no fermion token")
        await ws.send(to_json(Auth(token)))
        await ws.send(to_json(self.hello()))
        self._ws = ws
        self.sessions += 1
        log.info("fermion-node=connected url=%s", self.url)
        while self._pending:
            await ws.send(to_json(self._pending[0]))
            self._pending.pop(0)
        while self._outbox:
            await ws.send(to_json(self._outbox[0]))
            self._outbox.pop(0)
        async for raw in ws:
            await self._receive(raw, ws)

    async def _receive(self, raw: str | bytes, ws: Any) -> None:
        try:
            msg = from_json(raw)
        except FermionProtocolError as e:
            log.warning("fermion-node=frame error=%s", e)
            return
        match msg:
            case Command():
                task = asyncio.create_task(self._execute(msg))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            case Cancel():
                await self._cancel(msg, ws)
            case Done():
                await _call(self._on_done, msg.correlation_id, msg.payload)
            case Error():
                await _call(self._on_error, msg.correlation_id, msg.message)
            case _:
                log.warning("fermion-node=unexpected frame type=%s", msg.T)

    async def _deliver(self, msg: FermionMsg) -> None:
        """Sends a task's frame on the current connection, never on the one the command came by (closed when fermion
        restarted while the task ran). Without a connection, or when the send fails, the frame waits in the outbox, sent
        after the next ``auth`` and ``hello``; a full outbox drops its oldest ``progress`` first, so that a ``result`` is
        the last frame to be lost."""
        ws = self._ws
        if ws is not None and not self._outbox:
            try:
                await ws.send(to_json(msg))
                return
            except Exception as e:
                log.warning("fermion-node=send type=%s error=%s, kept until the reconnection", msg.T, e)
        if len(self._outbox) >= self._queue_size:
            dropped = next((m for m in self._outbox if isinstance(m, Progress)), self._outbox[0])
            self._outbox.remove(dropped)
            log.warning("fermion-node=outbox full: a %s frame dropped", dropped.T)
        self._outbox.append(msg)

    async def _execute(self, command: Command) -> None:
        send = self._deliver
        handler = self._handlers.get(command.action)
        if handler is None:
            await send(Result.failure(command, f"No handler for action {command.action}", "NO_HANDLER"))
            return
        context = TaskContext(command, send)
        key = (command.correlation_id, command.step_id)
        self._running[key] = (handler, context)
        beat = asyncio.create_task(self._beat(command, send)) if self._heartbeat > 0 else None
        try:
            out = await handler.handle(context)
            result = Result.ok(command, out)
        except SkillFailure as e:
            result = Result.failure(command, str(e), e.error_kind)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("fermion-node=task correlationId=%s action=%s", command.correlation_id, command.action)
            result = Result.failure(command, str(e) or type(e).__name__)
        finally:
            if beat is not None:
                beat.cancel()
            self._running.pop(key, None)
        await send(result)

    async def _beat(self, command: Command, send: Callable[[FermionMsg], Awaitable[None]]) -> None:
        while True:
            await asyncio.sleep(self._heartbeat)
            await send(Progress.of(command))

    async def _cancel(self, cancel: Cancel, ws: Any) -> None:
        found = self._running.get((cancel.correlation_id, cancel.step_id))
        if found is None and cancel.step_id is None:
            found = next((v for (corr, _), v in self._running.items() if corr == cancel.correlation_id), None)
        if found is None:
            ack = Result(cancel.correlation_id, None, cancel.action, False, cancel.reason, INTERRUPTED, {}, cancel.step_id)
            await _safe(lambda m: ws.send(to_json(m)), ack)
            return
        handler, context = found
        context.cancel = cancel
        context.cancelled.set()
        try:
            await handler.on_cancel(context, cancel)
        except Exception:
            log.exception("fermion-node=cancel correlationId=%s", cancel.correlation_id)


async def _safe(send: Callable[[FermionMsg], Awaitable[None]], msg: FermionMsg) -> None:
    try:
        await send(msg)
    except Exception as e:
        log.warning("fermion-node=send type=%s error=%s", msg.T, e)


async def _call(callback: Callable[..., Any] | None, *args: Any) -> None:
    if callback is None:
        return
    try:
        out = callback(*args)
        if asyncio.iscoroutine(out):
            await out
    except Exception:
        log.exception("fermion-node=callback failed")


def now_utc() -> datetime:
    """The current instant (for :meth:`NodeCapacity.closed_at`)."""
    return datetime.now(UTC)


__all__ = [
    "BUSY",
    "DEFAULT_ZONE",
    "INTERRUPTED",
    "PROTOCOL",
    "Auth",
    "Cancel",
    "Command",
    "Done",
    "Error",
    "FermionMsg",
    "FermionNode",
    "FermionProtocolError",
    "FermionQueueFullError",
    "Hello",
    "NodeCapacity",
    "Progress",
    "Result",
    "SkillDescriptor",
    "SkillFailure",
    "SkillHandler",
    "SkillParam",
    "Start",
    "TaskContext",
    "Translation",
    "Window",
    "WorkflowDescriptor",
    "from_json",
    "now_utc",
    "to_json",
    "translations",
    "ws_url",
]
