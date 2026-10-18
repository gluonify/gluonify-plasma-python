"""MCP tools of an application: tools/list, tools/call, identity, write right, limits, errors, marker."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeCharm
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from gluonify import CharmVerifier, GluonifyServices, Principal, markers
from gluonify.fastapi import WRITE_ROLE, GluonifyAuth, McpRegistry, create_app, mcp_tool
from gluonify.fastapi import mcp as mcp_module

ISS = "https://id.gluonify.lan/realms/gluonify"
ROOT = Path(__file__).resolve().parents[1]


class Echo(BaseModel):
    text: str = Field(min_length=1, max_length=20)
    times: int = 1


def build(charm: FakeCharm, reg: McpRegistry) -> FastAPI:
    auth = GluonifyAuth(lambda: CharmVerifier("u", audience="gluonify-source", issuer=ISS, fetch=charm.jwks))
    return create_app("test", audience="gluonify-source", services=GluonifyServices({}), auth=auth, mcp=reg)


@pytest.fixture
def reg() -> McpRegistry:
    r = McpRegistry()

    @r.tool(name="echo", description="Repeats a text.")
    def echo(args: Echo) -> dict[str, Any]:
        return {"text": args.text * args.times}

    @r.tool(name="whoami", description="The caller.")
    async def whoami(p: Principal) -> str:
        return p.subject

    @r.tool(name="raw", description="Raw arguments.", input_schema={"type": "object", "properties": {"a": {"type": "integer"}}})
    def raw(arguments: dict[str, Any]) -> list[int]:
        return [arguments.get("a", 0)]

    @r.tool(name="reset_counter", description="Resets the counter.", write=True)
    def reset() -> str:
        return "done"

    @r.tool(name="boom", description="Fails.")
    def boom() -> str:
        raise RuntimeError("secret detail")

    @r.tool(name="big", description="Large response.")
    def big() -> str:
        return "x" * (mcp_module.MAX_RESPONSE_BYTES + 1)

    @r.tool(name="opaque", description="Not serialisable.")
    def opaque() -> object:
        return object()

    return r


def rpc(c: TestClient, token: str | None, method: str, params: Any = None, id_: Any = 1, **kw: Any) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return c.post("/mcp", json=body, headers=headers, **kw)


def call(c: TestClient, token: str, name: str, arguments: Any = None) -> Any:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return rpc(c, token, "tools/call", params)


@pytest.fixture
def client(charm: FakeCharm, reg: McpRegistry) -> TestClient:
    return TestClient(build(charm, reg))


def test_initialize(client: TestClient, charm: FakeCharm) -> None:
    r = rpc(client, charm.token(), "initialize", {"protocolVersion": "2025-03-26"}).json()
    assert r["id"] == 1
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert r["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert rpc(client, charm.token(), "initialize").json()["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_shape(client: TestClient, charm: FakeCharm) -> None:
    tools = rpc(client, charm.token(), "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["big", "boom", "echo", "opaque", "raw", "reset_counter", "whoami"]
    echo = next(t for t in tools if t["name"] == "echo")
    assert echo["description"] == "Repeats a text."
    assert echo["inputSchema"]["type"] == "object"
    assert set(echo["inputSchema"]["properties"]) == {"text", "times"}
    assert echo["inputSchema"]["required"] == ["text"]
    assert echo["write"] is False
    assert echo["annotations"] == {"readOnlyHint": True}
    reset = next(t for t in tools if t["name"] == "reset_counter")
    assert reset["write"] is True
    assert reset["annotations"] == {"readOnlyHint": False}
    assert reset["inputSchema"] == {"type": "object", "properties": {}}
    assert next(t for t in tools if t["name"] == "raw")["inputSchema"]["properties"] == {"a": {"type": "integer"}}


def test_read_tool_call(client: TestClient, charm: FakeCharm) -> None:
    r = call(client, charm.token(sub="prism"), "echo", {"text": "ab", "times": 2}).json()["result"]
    assert r["isError"] is False
    assert r["structuredContent"] == {"text": "abab"}
    assert r["content"] == [{"type": "text", "text": '{"text":"abab"}'}]
    assert call(client, charm.token(sub="prism"), "whoami").json()["result"]["content"][0]["text"] == "prism"
    assert call(client, charm.token(), "raw", {"a": 7}).json()["result"]["content"][0]["text"] == "[7]"
    assert call(client, charm.token(), "whoami", None).status_code == 200
    null_args = rpc(client, charm.token(), "tools/call", {"name": "whoami", "arguments": None}).json()
    assert null_args["result"]["isError"] is False


def test_write_tool_needs_the_write_role(client: TestClient, charm: FakeCharm) -> None:
    r = call(client, charm.token(roles=["gluonify:mcp:read"]), "reset_counter")
    assert r.status_code == 403
    assert r.json() == {"error": f"missing role: {WRITE_ROLE}"}
    assert call(client, charm.token(), "reset_counter").status_code == 403
    ok = call(client, charm.token(roles=[WRITE_ROLE]), "reset_counter").json()["result"]
    assert ok["content"][0]["text"] == "done"
    # the write role does not matter for reads, and listing needs no write role
    assert call(client, charm.token(roles=[WRITE_ROLE]), "whoami").status_code == 200
    assert WRITE_ROLE == "gluonify:mcp:write"


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer ", "Bearer not-a-jwt"])
def test_401_without_a_valid_token(client: TestClient, header: str | None) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    r = client.post("/mcp", json=body, headers={} if header is None else {"Authorization": header})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_wrong_audience_and_expired_are_401(client: TestClient, charm: FakeCharm) -> None:
    assert rpc(client, charm.token(aud="other"), "tools/list").status_code == 401
    assert rpc(client, charm.token(ttl=-600), "tools/list").status_code == 401


def test_unknown_method_and_tool(client: TestClient, charm: FakeCharm) -> None:
    e = rpc(client, charm.token(), "resources/list", id_="abc").json()
    assert e["id"] == "abc"
    assert e["error"]["code"] == -32601
    e = call(client, charm.token(), "nope").json()["error"]
    assert e["code"] == -32602
    assert "unknown tool" in e["message"]


def test_invalid_params(client: TestClient, charm: FakeCharm) -> None:
    t = charm.token()
    assert rpc(client, t, "tools/call").json()["error"]["code"] == -32602
    assert rpc(client, t, "tools/call", {"name": 3}).json()["error"]["code"] == -32602
    assert rpc(client, t, "tools/call", {"name": "echo", "arguments": [1]}).json()["error"]["code"] == -32602
    e = call(client, t, "echo", {"text": ""}).json()["error"]
    assert e["code"] == -32602
    assert "text" in e["message"]
    assert call(client, t, "echo", {}).json()["error"]["code"] == -32602


def test_protocol_errors(client: TestClient, charm: FakeCharm) -> None:
    h = {"Authorization": f"Bearer {charm.token()}"}
    assert client.post("/mcp", content=b"{not json", headers=h).json()["error"]["code"] == -32700
    assert client.post("/mcp", json=[1], headers=h).json()["error"]["code"] == -32600
    assert client.post("/mcp", json={"jsonrpc": "1.0", "id": 1, "method": "x"}, headers=h).json()["error"]["code"] == -32600
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": True, "method": "x"}, headers=h).json()["error"]["code"] == -32600
    n = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    assert n.status_code == 202
    assert n.content == b""


def test_limits(client: TestClient, charm: FakeCharm) -> None:
    t = charm.token()
    huge = {"text": "x" * (mcp_module.MAX_ARGUMENTS_BYTES + 1)}
    assert call(client, t, "echo", huge).json()["error"]["code"] == -32602
    # just over the arguments limit but under the body cap: refused as invalid params
    near = {"text": "x" * (mcp_module.MAX_ARGUMENTS_BYTES - 5)}
    assert call(client, t, "echo", near).json()["error"]["code"] == -32602  # text longer than 20: model rejects it
    just_over = {"pad": "x" * mcp_module.MAX_ARGUMENTS_BYTES}
    r = call(client, t, "raw", just_over)
    assert r.json()["error"]["code"] == -32602
    assert "64 KiB" in r.json()["error"]["message"]
    way_over = call(client, t, "raw", {"pad": "x" * (mcp_module.MAX_BODY_BYTES + 10)})
    assert way_over.status_code == 413
    big = call(client, t, "big").json()["result"]
    assert big["isError"] is True
    assert "256 KiB" in big["content"][0]["text"]


def test_tool_failures_leak_nothing(client: TestClient, charm: FakeCharm) -> None:
    for name in ("boom", "opaque"):
        r = call(client, charm.token(), name).json()["result"]
        assert r["isError"] is True
        assert "secret" not in r["content"][0]["text"]


def test_registration_rules() -> None:
    r = McpRegistry()

    @r.tool(name="a1", description="d")
    def a1() -> str:
        return ""

    with pytest.raises(ValueError, match="twice"):
        r.tool(name="a1", description="d")(a1)
    for bad in ("", "A", "1a", "a-b", "a" * 42, "é"):
        with pytest.raises(ValueError, match="invalid MCP tool name"):
            r.tool(name=bad or " ", description="d")(a1)
    r.tool(name="a" * 41, description="d")(a1)
    with pytest.raises(ValueError, match="description"):
        r.tool(name="nodesc")(a1)
    with pytest.raises(ValueError, match="object schema"):
        r.tool(name="arr", description="d", input_schema={"type": "array"})(a1)
    with pytest.raises(ValueError, match="16 KiB"):
        r.tool(name="fat", description="d", input_schema={"type": "object", "description": "x" * 17000})(a1)

    def two(a: dict[str, Any], b: dict[str, Any]) -> None: ...

    with pytest.raises(TypeError, match="one arguments parameter"):
        r.tool(name="two", description="d")(two)

    def documented() -> str:
        """First line
        continues.

        Second paragraph."""
        return ""

    r.tool()(documented)
    assert r.get("documented") is not None
    assert r.get("documented").description == "First line continues."  # type: ignore[union-attr]
    assert "documented" in r
    assert len(r) == 3
    r.clear()
    assert len(r) == 0


def test_at_most_50_tools() -> None:
    r = McpRegistry()

    def f() -> str:
        return ""

    for i in range(50):
        r.tool(name=f"t{i}", description="d")(f)
    with pytest.raises(ValueError, match="at most 50"):
        r.tool(name="t50", description="d")(f)


def test_default_registry_and_create_app_switch(charm: FakeCharm) -> None:
    mcp_module.default_registry.clear()
    try:

        @mcp_tool(name="ping", description="Pings.")
        def ping() -> str:
            return "pong"

        auth = GluonifyAuth(lambda: CharmVerifier("u", audience="gluonify-source", issuer=ISS, fetch=charm.jwks))
        on = TestClient(create_app("t", audience="a", services=GluonifyServices({}), auth=auth, mcp=True))
        assert call(on, charm.token(), "ping").json()["result"]["content"][0]["text"] == "pong"
        off = TestClient(create_app("t", audience="a", services=GluonifyServices({}), auth=auth))
        assert off.post("/mcp", json={}).status_code == 404
    finally:
        mcp_module.default_registry.clear()


def test_mcp_is_not_in_the_openapi_contract(client: TestClient) -> None:
    assert "/mcp" not in client.get("/openapi.json").json()["paths"]


def test_marker_in_the_distribution_metadata() -> None:
    data = tomllib.loads((ROOT / "packages/gluonify/pyproject.toml").read_text())
    assert data["project"]["entry-points"]["gluonify.mcp"] == {"mcp": "gluonify.fastapi.mcp"}
    assert "mcp" not in data["project"]["entry-points"]["gluonify.uses"]  # a capability, never a service


def test_serves_mcp_reader(tmp_path: Path) -> None:
    def dist(name: str, entry_points: str | None) -> None:
        d = tmp_path / f"{name}-0.1.0.dist-info"
        d.mkdir()
        (d / "METADATA").write_text(f"Name: {name}\n")
        if entry_points is not None:
            (d / "entry_points.txt").write_text(entry_points)

    assert not markers.serves_mcp(tmp_path)
    dist("evil", "[gluonify.mcp]\nmcp = evil\n")  # untrusted
    dist("gluonify-a", None)
    dist("gluonify-b", "[gluonify.uses]\ncharm = x\n")
    dist("gluonify-c", "garbage [\n")
    dist("gluonify-d", "[gluonify.mcp]\nother = x\n")
    (tmp_path / "nometa-1.dist-info").mkdir()
    assert not markers.serves_mcp(tmp_path)
    dist("gluonify", "[gluonify.uses]\ncharm = gluonify.charm\n\n[gluonify.mcp]\nmcp = gluonify.fastapi.mcp\n")
    assert markers.serves_mcp(tmp_path)
    assert markers.infer(tmp_path).services == ("charm",)  # mcp never leaks into uses
