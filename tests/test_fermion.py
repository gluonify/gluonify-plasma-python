"""The Fermion node client: the protocol twin (fixtures shared byte for byte with Java) and FermionNode against a fake fermion."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from gluonify import fermion
from gluonify.fermion import (
    BUSY,
    INTERRUPTED,
    Auth,
    Cancel,
    Command,
    Done,
    Error,
    FermionNode,
    FermionProtocolError,
    FermionQueueFullError,
    Hello,
    NodeCapacity,
    Progress,
    Result,
    SkillDescriptor,
    SkillFailure,
    SkillHandler,
    SkillParam,
    Start,
    TaskContext,
    Window,
    from_json,
    to_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fermion"
JAVA_FIXTURES = Path(__file__).resolve().parents[2] / "gluonify-plasma" / "sdk" / "src" / "test" / "resources" / "fermion"
NAMES = sorted(p.stem for p in FIXTURES.glob("*.json"))
RUN = "AGENT_RUN_L0_EDGE"


# ----- protocol -----


@pytest.mark.parametrize("name", NAMES)
def test_every_fixture_is_written_back_byte_for_byte(name: str) -> None:
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    assert to_json(from_json(text)) + "\n" == text


@pytest.mark.skipif(not JAVA_FIXTURES.is_dir(), reason="gluonify-plasma is not checked out next to this repository")
def test_the_fixtures_are_those_of_the_java_sdk() -> None:
    java = sorted(p.name for p in JAVA_FIXTURES.glob("*.json"))
    assert java == sorted(p.name for p in FIXTURES.glob("*.json"))
    for name in java:
        assert (FIXTURES / name).read_bytes() == (JAVA_FIXTURES / name).read_bytes(), name


def test_done_and_cancel_carry_their_fields() -> None:
    done = from_json((FIXTURES / "done.json").read_text())
    assert isinstance(done, Done)
    assert done.payload["answer"] == "8080"
    cancel = from_json((FIXTURES / "cancel.json").read_text())
    assert isinstance(cancel, Cancel)
    assert cancel.resumable
    assert cancel.step_id == "Task_Run"
    assert to_json(Done("c", None, "w", None)) == '{"t":"done","correlationId":"c","workflow":"w","payload":{}}'  # type: ignore[arg-type]


def test_empty_values_are_left_out_like_jackson() -> None:
    command = Command("c", "g", RUN, {}, "Task_Run")
    assert Progress.of(command, None, {}).data is None
    assert "data" not in to_json(Progress.of(command))
    hello = Hello("a", "n", None, None, None, NodeCapacity())  # type: ignore[arg-type]
    assert hello.capacity is None
    assert to_json(hello) == '{"t":"hello","appId":"a","nodeId":"n","skills":[],"usedWorkflows":[]}'
    skill = SkillDescriptor(RUN, [], [], [], "PT12H", {"model": 1, "heavy": 1})
    assert list(skill.requires or {}) == ["heavy", "model"]
    assert SkillDescriptor(RUN, requires={}).requires is None


def test_unknown_fields_are_ignored_and_absent_booleans_are_false() -> None:
    msg = from_json('{"t":"result","correlationId":"c","action":"A","future":1}')
    assert isinstance(msg, Result)
    assert msg.success is False
    assert msg.payload == {}
    assert from_json('{"t":"auth","token":"x"}') == Auth("x", 1)
    param = SkillParam.from_dict({"name": "n"})
    assert (param.required, param.store, param.type) == (False, False, None)


@pytest.mark.parametrize(
    "text",
    ['{"t":"teleport"}', "not json", "[1]", '{"t":"hello","appId":"a","capacity":{"closed":[{"from":"25:00","to":"01:00"}]}}'],
)
def test_a_bad_frame_raises_a_protocol_error(text: str) -> None:
    with pytest.raises(FermionProtocolError):
        from_json(text)


def test_auth_never_shows_its_token() -> None:
    assert "secret-value" not in repr(Auth("secret-value"))


def test_results_answer_their_command() -> None:
    command = Command("c", "g", RUN, {}, "Task_Run")
    ok = Result.ok(command, {"branch": "run/T-1"})
    assert (ok.success, ok.step_id, ok.global_transaction_id, ok.payload) == (True, "Task_Run", "g", {"branch": "run/T-1"})
    failed = Result.failure(command, "stopped", INTERRUPTED)
    assert (failed.success, failed.error_kind) == (False, "interrupted")


def test_capacity_parses_its_configuration_form() -> None:
    capacity = NodeCapacity.of({"heavy": 1, "claude": 3}, ["23:30-07:00", "sat,Sun 00:00-00:00"])
    assert list(capacity.slots) == ["claude", "heavy"]
    assert [str(w) for w in capacity.closed] == ["23:30-07:00", "SAT,SUN 00:00-00:00"]
    assert capacity.closed[0].zone == "Europe/Paris"
    assert not capacity.empty
    assert NodeCapacity().empty


@pytest.mark.parametrize("window", ["23:30", "7-8", "25:00-07:00", "XYZ 01:00-02:00", "23:30-07:00 extra", None])
def test_a_malformed_window_is_refused(window: str | None) -> None:
    with pytest.raises(ValueError, match="window"):
        Window.parse(window)


def test_a_negative_slot_and_an_unknown_zone_are_refused() -> None:
    with pytest.raises(ValueError, match="zero or more"):
        NodeCapacity.of({"heavy": -1})
    with pytest.raises(ValueError, match="zone"):
        NodeCapacity.of({}, ["23:00-01:00"], "Mars/Olympus")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_a_window_crossing_midnight_closes_both_evenings() -> None:
    night = Window.parse("23:30-07:00", "UTC")
    assert night.contains(at("2026-10-20T23:30:00+00:00"))
    assert night.contains(at("2026-10-21T06:59:00+00:00"))
    assert not night.contains(at("2026-10-21T07:00:00+00:00"))
    assert not night.contains(at("2026-10-20T23:29:00+00:00"))


def test_a_window_restricted_to_days_starts_only_on_them() -> None:
    bench = Window.parse("MON 00:00-06:00", "UTC")
    assert bench.contains(at("2026-10-26T03:00:00+00:00"))
    assert not bench.contains(at("2026-10-27T03:00:00+00:00"))
    sunday_night = Window.parse("SUN 22:00-02:00", "UTC")
    assert sunday_night.contains(at("2026-10-26T01:00:00+00:00"))
    assert not sunday_night.contains(at("2026-10-27T01:00:00+00:00"))
    assert Window.parse("SAT 00:00-00:00", "UTC").contains(at("2026-10-24T12:00:00+00:00"))


def test_a_window_uses_its_zone() -> None:
    paris = NodeCapacity.of({}, ["23:30-07:00"])
    assert paris.closed_at(at("2026-10-20T22:00:00+00:00"))
    assert not paris.closed_at(at("2026-10-20T20:00:00+00:00"))
    assert fermion.now_utc().tzinfo is UTC


def test_the_node_url_and_the_durations_follow_the_java_client() -> None:
    assert fermion.ws_url("https://fermion.lan/", "superposition", "mac") == "wss://fermion.lan/ws/fermion/superposition-mac"
    assert fermion.ws_url("http://f:8080", "a", "0") == "ws://f:8080/ws/fermion/a-0"
    assert fermion.ws_url("ws://f", "a", "0") == "ws://f/ws/fermion/a-0"
    with pytest.raises(ValueError, match="fermion URL"):
        fermion.ws_url("ftp://f", "a", "0")
    assert fermion._iso(timedelta(hours=12)) == "PT12H"
    assert fermion._iso(timedelta(minutes=90, seconds=5)) == "PT1H30M5S"
    assert fermion._iso(timedelta(0)) == "PT0S"
    assert [t.language for t in fermion.translations("a", "b", "c", "d", "e")] == ["EN", "FR", "ES", "IT", "DE"]


# ----- node against a fake fermion -----


class FakeFermion:
    """Records the frames of each session; sends frames to the open connection; can cut it."""

    def __init__(self) -> None:
        self.frames: list[Any] = []
        self.connection: ServerConnection | None = None
        self.sessions = 0
        self.port = 0

    async def handler(self, connection: ServerConnection) -> None:
        self.connection = connection
        self.sessions += 1
        async for raw in connection:
            self.frames.append(from_json(raw))

    def of(self, kind: type[Any]) -> list[Any]:
        return [f for f in self.frames if isinstance(f, kind)]

    async def send(self, msg: Any) -> None:
        assert self.connection is not None
        await self.connection.send(to_json(msg))

    async def send_raw(self, text: str) -> None:
        assert self.connection is not None
        await self.connection.send(text)

    async def cut(self) -> None:
        assert self.connection is not None
        await self.connection.close()


async def until(condition: Callable[[], bool], what: str) -> None:
    for _ in range(400):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout: {what}")


class Echo(SkillHandler):
    action = "ECHO"
    inputs = (SkillParam("text", "STRING"),)
    descriptions = tuple(fermion.translations("Echo", "Écho", "Eco", "Eco", "Echo"))

    async def handle(self, context: TaskContext) -> Mapping[str, Any] | None:
        return {"upper": str(context.payload["text"]).upper()}


class Fail(SkillHandler):
    action = "FAIL"

    async def handle(self, context: TaskContext) -> Mapping[str, Any] | None:
        raise SkillFailure("STORAGE_UNAVAILABLE", "disk is gone")


class Boom(SkillHandler):
    action = "BOOM"

    async def handle(self, context: TaskContext) -> Mapping[str, Any] | None:
        raise RuntimeError("boom")


class Run(SkillHandler):
    """Holds the heavy slot, reports its phase, stops when cancelled."""

    action = RUN
    expected_duration = timedelta(hours=12)
    requires = {"heavy": 1}  # noqa: RUF012 (class-level constant of the handler)
    started: asyncio.Event
    seen: list[Cancel]

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.seen = []

    async def handle(self, context: TaskContext) -> Mapping[str, Any] | None:
        await context.progress({"phase": "edit"})
        await context.progress({})
        self.started.set()
        await context.cancelled.wait()
        raise SkillFailure(INTERRUPTED, f"stopped: {context.cancel.reason if context.cancel else '?'}")

    async def on_cancel(self, context: TaskContext, cancel: Cancel) -> None:
        self.seen.append(cancel)


class BadCancel(Run):
    action = "BAD_CANCEL"

    async def on_cancel(self, context: TaskContext, cancel: Cancel) -> None:
        raise RuntimeError("cancel hook failure")


@asynccontextmanager
async def running(
    server: FakeFermion, handlers: list[SkillHandler], token: Callable[[], str | None] = lambda: "test-token", **options: Any
) -> AsyncIterator[FermionNode]:
    async with serve(server.handler, "127.0.0.1", server.port) as ws_server:
        server.port = next(iter(ws_server.sockets)).getsockname()[1]
        node = FermionNode(
            f"http://127.0.0.1:{server.port}",
            "superposition",
            "mac",
            token,
            handlers,
            capacity=NodeCapacity.of({"heavy": 1}, ["23:30-07:00"]),
            open_api_url="http://agent/q/openapi",
            reconnect_interval=0.05,
            **options,
        )
        task = asyncio.create_task(node.run())
        try:
            yield node
        finally:
            await node.stop()
            await asyncio.wait_for(task, 5)


def play(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(scenario())


def test_the_node_authenticates_announces_itself_and_runs_tasks() -> None:
    async def scenario() -> None:
        server = FakeFermion()
        async with running(server, [Echo(), Fail(), Boom(), Run()]) as node:
            await until(lambda: len(server.frames) >= 2, "auth and hello")
            assert server.frames[0] == Auth("test-token")
            hello = server.frames[1]
            assert isinstance(hello, Hello)
            assert [s.id for s in hello.skills] == [RUN, "BOOM", "ECHO", "FAIL"]
            run = next(s for s in hello.skills if s.id == RUN)
            assert (run.expected_duration, run.requires) == ("PT12H", {"heavy": 1})
            assert hello.capacity is not None
            assert hello.capacity.slots == {"heavy": 1}
            assert node.connected
            assert "superposition-mac" in repr(node)

            tasks = (("c1", "ECHO", {"text": "abc"}), ("c2", "FAIL", {}), ("c3", "BOOM", {}), ("c4", "NOPE", {}))
            for corr, action, payload in tasks:
                await server.send(Command(corr, "g", action, payload, f"Task_{action}"))
            await until(lambda: len(server.of(Result)) == 4, "four results")
            by = {r.correlation_id: r for r in server.of(Result)}
            assert by["c1"].payload == {"upper": "ABC"}
            assert by["c1"].step_id == "Task_ECHO"
            assert (by["c2"].error_kind, by["c2"].error) == ("STORAGE_UNAVAILABLE", "disk is gone")
            assert (by["c3"].success, by["c3"].error_kind, by["c3"].error) == (False, None, "boom")
            assert by["c4"].error_kind == "NO_HANDLER"

    play(scenario)


def test_a_cancel_reaches_the_running_handler_which_stops_as_interrupted() -> None:
    async def scenario() -> None:
        server = FakeFermion()
        run = Run()
        async with running(server, [run, BadCancel()], heartbeat_interval=0.02):
            await until(lambda: len(server.frames) >= 2, "hello")
            await server.send(Command("k1", "g", RUN, {}, "Task_Run"))
            await asyncio.wait_for(run.started.wait(), 5)
            await until(lambda: len(server.of(Progress)) >= 3, "event then heartbeats")
            events = [p for p in server.of(Progress) if p.data]
            assert [e.data for e in events] == [{"phase": "edit"}], "an empty event is not sent"
            await server.send(Cancel("k1", "Task_Run", RUN, "kill switch", True))
            await until(lambda: bool(server.of(Result)), "interrupted result")
            result = server.of(Result)[0]
            assert (result.error_kind, result.error) == (INTERRUPTED, "stopped: kill switch")
            assert run.seen[0].resumable
            beats = len(server.of(Progress))
            await asyncio.sleep(0.1)
            assert len(server.of(Progress)) == beats, "no heartbeat after the result"

            # Without step id the task is found by its instance; a failing on_cancel hook is logged, the task still ends.
            await server.send(Command("k2", None, "BAD_CANCEL", {}))
            await until(lambda: any(p.correlation_id == "k2" for p in server.of(Progress)), "k2 runs")
            await server.send(Cancel("k2", None, "BAD_CANCEL", "deleted", False))
            await until(lambda: len(server.of(Result)) == 2, "k2 interrupted")

            # A cancel for a task that does not run here is acknowledged at once.
            await server.send(Cancel("gone", "Task_X", "ECHO", "deleted", False))
            await until(lambda: len(server.of(Result)) == 3, "ack")
            ack = server.of(Result)[2]
            assert (ack.correlation_id, ack.step_id, ack.error_kind, ack.error) == ("gone", "Task_X", INTERRUPTED, "deleted")

    play(scenario)


def test_done_and_errors_reach_the_callbacks_and_bad_frames_are_ignored() -> None:
    async def scenario() -> None:
        server = FakeFermion()
        done: list[tuple[str, dict[str, Any]]] = []
        errors: list[tuple[str | None, str | None]] = []
        async with running(server, [Echo()]) as node:
            node.on_done(lambda corr, payload: done.append((corr, payload)))

            async def on_error(corr: str | None, message: str | None) -> None:
                errors.append((corr, message))

            node.on_error(on_error)
            await until(lambda: len(server.frames) >= 2, "hello")
            await server.send(Done("q1", "g", "ask-question", {"answer": "8080"}))
            await server.send(Error("q2", "workflow inactive"))
            await until(lambda: bool(done) and bool(errors), "callbacks called")
            await server.send_raw("not json")
            await server.send(Hello("x", "y"))
            node.on_done(lambda corr, payload: 1 / 0)
            await server.send(Done("q3", None, "ask-question", {}))
            await asyncio.sleep(0.1)  # q3 reaches the failing callback, which is logged
            node.on_done(None)
            await server.send(Done("q4", None, "ask-question", {}))
            await server.send(Command("e1", None, "ECHO", {"text": "alive"}))
            await until(lambda: bool(server.of(Result)), "the node still answers")
            assert done == [("q1", {"answer": "8080"})]
            assert errors == [("q2", "workflow inactive")]

    play(scenario)


def test_starts_are_sent_queued_during_an_outage_replayed_and_bounded() -> None:
    async def scenario() -> None:
        server = FakeFermion()
        async with running(server, [], queue_size=2) as node:
            await until(lambda: node.connected, "connected")
            await node.start_workflow("ask-question", "s1", {"q": "port?"}, "run-7")
            await until(lambda: bool(server.of(Start)), "start")
            assert server.of(Start)[0] == Start("ask-question", "s1", "run-7", {"q": "port?"})

            # A send that fails is queued (the connection died under the node).
            sessions = server.sessions
            await server.cut()
            await until(lambda: server.sessions > sessions, "reconnected")
            await until(lambda: node.connected, "connected again")
            assert sum(isinstance(f, Auth) for f in server.frames) == 2

    play(scenario)

    async def outage() -> None:
        node = FermionNode("http://127.0.0.1:9", "a", "0", lambda: "t", reconnect_interval=0.01)
        await node.start_workflow("w", "q1")
        await node.start_workflow("w", "q2")
        assert node.pending_count == 2
        node._queue_size = 2
        with pytest.raises(FermionQueueFullError):
            await node.start_workflow("w", "q3")

        class Broken:
            async def send(self, message: str) -> None:
                raise ConnectionError("gone")

        node._pending.clear()
        node._ws = Broken()
        await node.start_workflow("w", "q4")
        assert node.pending_count == 1, "a failed send is queued"

    play(outage)


def test_queued_starts_are_replayed_after_auth_and_hello() -> None:
    async def scenario() -> None:
        server = FakeFermion()
        node_holder: list[FermionNode] = []

        def token() -> str | None:
            return "test-token" if node_holder and node_holder[0].pending_count else None

        async with running(server, [], token=token) as node:
            node_holder.append(node)
            await asyncio.sleep(0.1)
            assert not node.connected, "no token yet: no connection"
            await node.start_workflow("w", "late")
            await until(lambda: bool(server.of(Start)), "replayed start")
            assert [type(f).__name__ for f in server.frames[:3]] == ["Auth", "Hello", "Start"]
            assert node.pending_count == 0

    play(scenario)


def test_busy_is_a_kind_the_handler_may_raise() -> None:
    assert BUSY == "busy"
    assert SkillFailure(BUSY, "model loaded").error_kind == "busy"
    assert json.loads(to_json(Result.failure(Command("c", None, RUN), "x", BUSY)))["errorKind"] == "busy"


def test_edge_paths_of_the_node() -> None:
    with pytest.raises(ValueError, match="HH:mm"):
        Window("07:00:00", "08:00")

    async def scenario() -> None:
        idle = FermionNode("http://127.0.0.1:9", "a", "0", lambda: "t")
        await idle.stop()
        await asyncio.wait_for(idle.run(), 1)  # stopped before it started: returns at once

        async def failing(_msg: Any) -> None:
            raise ConnectionError("gone")

        await fermion._safe(failing, Progress("c", None, RUN))  # logged, never raised

        server = FakeFermion()
        run = Run()
        async with running(server, [run], heartbeat_interval=0) as node:
            await until(lambda: len(server.frames) >= 2, "hello")
            await server.send(Command("k3", "g", RUN, {}, "Task_Run"))
            await asyncio.wait_for(run.started.wait(), 5)
            # A cancel without step id finds the task by its instance.
            await server.send(Cancel("k3", None, RUN, "by instance", False))
            await until(lambda: bool(server.of(Result)), "interrupted")
            assert not any(p.data is None for p in server.of(Progress)), "no heartbeat when disabled"

            second = Run()
            node._handlers[RUN] = second
            await server.send(Command("k4", "g", RUN, {}, "Task_Run"))
            await asyncio.wait_for(second.started.wait(), 5)
        assert not node._tasks, "stop cancels the running tasks"

    play(scenario)
