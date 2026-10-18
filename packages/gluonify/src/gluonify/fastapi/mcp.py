"""MCP tools of an application: the decorator :func:`mcp_tool` and the router :func:`mcp_router` (``POST /mcp``).

The application declares business tools (diagnostics, internal state, statistics, settings); Prism, the customers' MCP,
discovers them (``tools/list`` at deployment) and relays the calls of the customers' AI assistants
(``app.<name>.<tool>``). Specification: ``gluonify-codex/app-mcp.md``. The Java counterpart is the Quarkus helper of
``gluonify-plasma``; the wire behaviour is the same.

Wire protocol: JSON-RPC 2.0 over ``POST /mcp``, methods ``initialize``, ``tools/list`` and ``tools/call`` (tools only: no
resources, prompts, sampling or streaming). A message without ``id`` is a notification: answered ``202`` with no body.

Identity: the caller is authenticated by the Charm machinery of :mod:`gluonify.fastapi` (``Authorization: Bearer`` with the
application-to-service token Prism uses, ES256, audience of the application): ``401`` without a valid token. A tool with
``write=True`` additionally needs the role :data:`WRITE_ROLE` (``403`` otherwise). ``/mcp`` must only be reachable on the
internal listener; the platform blocks it at the public ingress (``ManagementPaths`` of Higgs), nothing to do here.

Limits (the same as Prism and the Java helper): tool name ``[a-z][a-z0-9_]{0,40}``, at most 50 tools, input schema at most
16 KiB, arguments at most 64 KiB, response at most 256 KiB.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python
from starlette.concurrency import run_in_threadpool

from gluonify import Principal

from .auth import current_principal

__all__ = [
    "MAX_ARGUMENTS_BYTES",
    "MAX_BODY_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_SCHEMA_BYTES",
    "MAX_TOOLS",
    "TOOL_NAME",
    "WRITE_ROLE",
    "McpRegistry",
    "McpTool",
    "default_registry",
    "mcp_router",
    "mcp_tool",
]

log = logging.getLogger("gluonify.mcp")

WRITE_ROLE = "gluonify:mcp:write"
"""Role carried by the token Prism sends for the call of a ``write`` tool (the token of a read call does not carry it)."""
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,40}")
MAX_TOOLS = 50
MAX_SCHEMA_BYTES = 16 * 1024
MAX_ARGUMENTS_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_BODY_BYTES = MAX_ARGUMENTS_BYTES + 4 * 1024
"""Largest ``POST /mcp`` body read: the arguments plus the JSON-RPC envelope (413 beyond)."""

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "gluonify-mcp"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


@dataclass(frozen=True, slots=True)
class McpTool:
    """A declared tool. ``model`` validates the arguments when given (else the function receives the raw ``dict``)."""

    name: str
    description: str
    write: bool
    input_schema: dict[str, Any]
    func: Callable[..., Any]
    model: type[BaseModel] | None
    argument: str | None  # name of the parameter receiving the arguments
    principal: str | None  # name of the parameter receiving the caller's Principal

    def describe(self) -> dict[str, Any]:
        """The ``tools/list`` entry: MCP's ``name``, ``description``, ``inputSchema`` and ``annotations``, plus ``write``."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": not self.write},
            "write": self.write,
        }


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


class McpRegistry:
    """The tools of an application. :func:`mcp_tool` fills :data:`default_registry`; tests use their own."""

    def __init__(self) -> None:
        self._tools: dict[str, McpTool] = {}

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def get(self, name: str) -> McpTool | None:
        return self._tools.get(name)

    def tools(self) -> list[McpTool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def clear(self) -> None:
        self._tools.clear()

    def tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        write: bool = False,
        input_schema: dict[str, Any] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator declaring a tool in this registry; see :func:`mcp_tool`."""

        def register(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add(self._build(func, name, description, write, input_schema))
            return func

        return register

    def add(self, tool: McpTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"MCP tool declared twice: {tool.name}")
        if len(self._tools) >= MAX_TOOLS:
            raise ValueError(f"at most {MAX_TOOLS} MCP tools per application")
        self._tools[tool.name] = tool

    @staticmethod
    def _build(
        func: Callable[..., Any], name: str | None, description: str | None, write: bool, input_schema: dict[str, Any] | None
    ) -> McpTool:
        name = name or func.__name__
        if not TOOL_NAME.fullmatch(name):
            raise ValueError(f"invalid MCP tool name {name!r}: must match [a-z][a-z0-9_]{{0,40}}")
        description = description or (inspect.getdoc(func) or "").strip().split("\n\n")[0].replace("\n", " ")
        if not description:
            raise ValueError(f"MCP tool {name}: a description is required (argument or docstring)")
        hints = typing.get_type_hints(func)
        model: type[BaseModel] | None = None
        argument: str | None = None
        principal: str | None = None
        for p in inspect.signature(func).parameters.values():
            hint = hints.get(p.name)
            if hint is Principal:
                principal = p.name
            elif argument is None:
                argument = p.name
                if inspect.isclass(hint) and issubclass(hint, BaseModel):
                    model = hint
            else:
                raise TypeError(f"MCP tool {name}: one arguments parameter and optionally a Principal, found also {p.name!r}")
        if input_schema is None:
            input_schema = model.model_json_schema() if model else {"type": "object", "properties": {}}
        if input_schema.get("type") != "object":
            raise ValueError(f"MCP tool {name}: the input schema must be an object schema")
        if _size(input_schema) > MAX_SCHEMA_BYTES:
            raise ValueError(f"MCP tool {name}: the input schema is larger than {MAX_SCHEMA_BYTES // 1024} KiB")
        return McpTool(name, description, write, input_schema, func, model, argument, principal)


default_registry = McpRegistry()


def mcp_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    write: bool = False,
    input_schema: dict[str, Any] | None = None,
    registry: McpRegistry | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declares a function as an MCP tool of the application (sync or async).

    * ``name``: ``[a-z][a-z0-9_]{0,40}`` (default: the function name); Prism publishes it as ``app.<application>.<name>``;
    * ``description``: what the AI reads (default: the first paragraph of the docstring);
    * ``write``: true when the tool changes something; it then needs the role :data:`WRITE_ROLE`, and Prism turns the call
      into a proposal to approve. Default false;
    * the function takes at most one arguments parameter, annotated with a pydantic ``BaseModel`` (the input schema is the
      model's, the arguments are validated against it: invalid ones are a ``-32602``) or, with ``input_schema=`` (a JSON
      Schema of type object), a ``dict``; and optionally a parameter annotated :class:`gluonify.Principal` receiving the
      caller's identity;
    * the result: a ``str`` (as is), or anything JSON-serialisable by pydantic (a ``BaseModel``, ``dict``, ``list``…). An
      exception becomes an ``isError`` result with a generic message (the detail goes to the log, never to the AI).
    """
    return (registry or default_registry).tool(name=name, description=description, write=write, input_schema=input_schema)


def _error(id_: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _result(id_: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


def _tool_error(id_: Any, message: str) -> JSONResponse:
    return _result(id_, {"content": [{"type": "text", "text": message}], "isError": True})


async def _call(tool: McpTool, arguments: dict[str, Any], principal: Principal) -> Any:
    kwargs: dict[str, Any] = {}
    if tool.argument is not None:
        kwargs[tool.argument] = tool.model.model_validate(arguments) if tool.model else arguments
    if tool.principal is not None:
        kwargs[tool.principal] = principal
    if inspect.iscoroutinefunction(tool.func):
        return await tool.func(**kwargs)
    return await run_in_threadpool(lambda: tool.func(**kwargs))


async def _tools_call(id_: Any, params: Any, registry: McpRegistry, principal: Principal) -> Response:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _error(id_, INVALID_PARAMS, "params.name (string) is required")
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(id_, INVALID_PARAMS, "params.arguments must be an object")
    tool = registry.get(params["name"])
    if tool is None:
        return _error(id_, INVALID_PARAMS, f"unknown tool: {params['name'][:64]}")
    if _size(arguments) > MAX_ARGUMENTS_BYTES:
        return _error(id_, INVALID_PARAMS, f"arguments larger than {MAX_ARGUMENTS_BYTES // 1024} KiB")
    if tool.write and not principal.has_role(WRITE_ROLE):
        return JSONResponse({"error": f"missing role: {WRITE_ROLE}"}, status_code=403)
    try:
        value = await _call(tool, arguments, principal)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        return _error(id_, INVALID_PARAMS, f"invalid arguments: {problems}"[:1024])
    except Exception:
        log.exception("MCP tool %s failed", tool.name)
        return _tool_error(id_, "the tool failed")
    try:
        data = to_jsonable_python(value)
    except Exception:
        log.exception("MCP tool %s returned a value that is not serialisable", tool.name)
        return _tool_error(id_, "the tool returned a value that cannot be serialised")
    text = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if isinstance(data, dict):
        result["structuredContent"] = data
    if _size(result) > MAX_RESPONSE_BYTES:
        return _tool_error(id_, f"the response is larger than {MAX_RESPONSE_BYTES // 1024} KiB")
    return _result(id_, result)


def mcp_router(registry: McpRegistry | None = None, *, path: str = "/mcp", server_version: str = "0.1.0") -> APIRouter:
    """The router serving ``POST /mcp`` for ``registry`` (default: :data:`default_registry`, filled by :func:`mcp_tool`).

    Include it in an application created by :func:`gluonify.fastapi.create_app` (which also does it with
    ``create_app(..., mcp=True)``): it uses the application's Charm authentication. Tools may be declared before or after
    the router is included: the registry is read at each request.
    """
    reg = registry if registry is not None else default_registry
    router = APIRouter(tags=["mcp"])

    @router.post(path, include_in_schema=False)
    async def mcp(request: Request, principal: Principal = Depends(current_principal)) -> Response:  # noqa: B008
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"error": "request too large"}, status_code=413)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"error": "request too large"}, status_code=413)
        try:
            message = json.loads(raw)
        except ValueError:
            return _error(None, PARSE_ERROR, "parse error")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        if "id" not in message:
            return Response(status_code=202)  # notification: no answer
        id_ = message["id"]
        if isinstance(id_, bool) or not isinstance(id_, str | int | float):
            return _error(None, INVALID_REQUEST, "id must be a string or a number")
        method, params = message["method"], message.get("params")
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            return _result(
                id_,
                {
                    "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": server_version},
                },
            )
        if method == "tools/list":
            return _result(id_, {"tools": [t.describe() for t in reg.tools()]})
        if method == "tools/call":
            return await _tools_call(id_, params, reg, principal)
        return _error(id_, METHOD_NOT_FOUND, f"method not found: {method[:64]}")

    return router
