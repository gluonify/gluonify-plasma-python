# gluonify-plasma-python

The **Python SDK of [Gluonify](https://gluonify.io)**: what a Python application needs to run on the platform with **zero configuration**, the counterpart of the Java SDK (`gluonify-plasma`: `services`, `quarkus` and the `META-INF/gluonify/services/<service>` markers).

Python applications run on Gluonify through **path 3** of `gluonify-codex/python.md` (§ 3 bis): the `python3` of Debian 13 (CPython **3.13**) on each node, the code and its venv in a signed gzip built by Bup, the same systemd sandbox as native applications (`MemoryDenyWriteExecute=yes`). The web server is **FastAPI** with uvicorn, one process per instance (question 4 of python.md).

One distribution, **`gluonify`**, published on PyPI under the `gluonify` organisation (question 5), with an optional extra:

| Install | Import | Content |
|---|---|---|
| `pip install gluonify` | `gluonify` | discovery of platform services (`GLUONIFY_SERVICE_<APP>_URL`, service tokens), Charm (JWT verification against the JWKS, ES256), Top (configuration and secrets), the `uses` marker reader |
| `pip install "gluonify[fastapi]"` | `gluonify.fastapi` | adds fastapi and uvicorn: a dependency that authenticates a request with Charm and checks roles, health endpoints, the app factory |

Importing `gluonify.fastapi` without the extra raises an `ImportError` that gives the `pip install` command.

The starter application that uses it is [`gluonify-source-python`](https://github.com/gluonify/gluonify-source-python).

## Contents
1. [What is supported, and what is not yet](#1-what-is-supported-and-what-is-not-yet)
2. [Quick start](#2-quick-start)
3. [Services: discovery and tokens](#3-services-discovery-and-tokens)
4. [Charm: incoming tokens](#4-charm-incoming-tokens)
5. [Top: configuration and secrets](#5-top-configuration-and-secrets)
6. [How to reach gDown](#6-how-to-reach-gdown)
7. [FastAPI](#7-fastapi)
8. [The uses markers (contract read by Bup)](#8-the-uses-markers-contract-read-by-bup)
   - [MCP tools of an application](#8-bis-mcp-tools-of-an-application)
9. [Configuration reference](#9-configuration-reference)
10. [Development](#10-development)
11. [Publishing on PyPI](#11-publishing-on-pypi)

## 1. What is supported, and what is not yet

| Feature | State | Notes |
|---|---|---|
| Service discovery on the platform (`GLUONIFY_SERVICE_<APP>_URL`, `GLUONIFY_<APP>_TOKEN_FILE` re-read at every call, `GLUONIFY_<APP>_TOKEN`) | ✅ | same keys, order and messages as `GluonifyServices` of the Java SDK; unit tests |
| Explicit override (`overrides=`, environment off the platform) | ✅ | `overrides` plays the role of Java system properties: it wins even on the platform |
| Platform variables (`GLUONIFY_ENVIRONMENT`, `GLUONIFY_REPLICA`, `GLUONIFY_SELF_URL`, `GLUONIFY_NODE_ADDRESS`) | ✅ | |
| Developer machine (`gluonify login <zone>`, `~/.gluonify`, discovery document `/.well-known/gluonify`, developer token refresh) | ❌ | Java only for now; off the platform, set `GLUONIFY_SERVICE_<APP>_URL` (and a token) by hand |
| Charm: verification of incoming JWT (ES256 only, `aud`, `iss`, `exp`, `sub`, `roles`) | ✅ | JWKS kept 5 min, refetched for an unknown `kid` (30 s at most), last keys kept during a Charm outage |
| Charm: issuing tokens, OIDC sign-in of a browser (authorization code + PKCE) | ❌ | the browser side is the `@gluonify/ui` kit; a Python app only verifies |
| Top: configuration and secrets injected by the node agent (`APP_<KEY>` with `"top": true`, `<KEY>` with a literal `topNamespace`) | ✅ | no network call: Strange writes the keys into the environment before the start |
| Top: reading the vault at run time (HTTP API) | ❌ | not needed by applications; values change at the next start |
| gDown: no SDK client, the official `neo4j` driver used directly over Bolt with TLS (`GLUONIFY_GDOWN_BOLT_URL`, CA `GLUONIFY_GDOWN_CA_FILE`) | ✅ by design | snippet in [§6](#6-how-to-reach-gdown); `neomodel` as an OGM; `?gdown` inferred by Bup from the `neo4j` or `neomodel` distribution |
| FastAPI: `require_roles`, `current_principal`, `optional_principal`; 401 / 403 / 503 as `{"error": …}` | ✅ | |
| FastAPI: health `/health/live`, `/health/ready` (MicroProfile Health body, 503 when DOWN) | ✅ | not under `/q/` (the Quarkus namespace); Bup sends both paths to the platform, blocked at the ingress in PRD |
| FastAPI: app factory (`create_app`: OpenAPI `/openapi.json`, Swagger UI `/docs`, read-only on the platform, 400 with `violations`) | ✅ | Swagger UI does not follow a light/dark theme (no interface here) |
| FastAPI: MCP tools of an application (`@mcp_tool`, `mcp_router`, `create_app(mcp=True)`: JSON-RPC 2.0 `POST /mcp`, `initialize` / `tools/list` / `tools/call`, write tools need `gluonify:mcp:write`) | ✅ SDK side | [§8 bis](#8-bis-mcp-tools-of-an-application); Prism does not relay yet (app-mcp.md phases 2-3) |
| `uses` inference (`[gluonify.uses]` entry points of `gluonify`/`gluonify-*`: `gluonify` -> `charm`; distributions `neo4j`/`neomodel` -> `gdown`) | ✅ SDK side | Bup does not read them yet (contract in [§8](#8-the-uses-markers-contract-read-by-bup)) |
| FastAPI as the extra `gluonify[fastapi]` of the one distribution (`gluonify.fastapi`; clear `ImportError` without the extra) | ✅ | the former `gluonify-fastapi` distribution is gone (never published); the name stays reserved |
| Python ≥ 3.13, typed (`py.typed`, mypy strict), ruff | ✅ | |
| Publication on PyPI by Trusted Publishing with PEP 740 attestations | 🟡 | workflow ready for `gluonify`; nothing published, the trusted publisher is to be added by the user ([§11](#11-publishing-on-pypi)) |
| Clients for Crystallon, Fermion, Photon | ❌ | Java only |
| End-to-end test on a real cluster | ✅ | phase V3-4 of python.md: `gluonify-chamber/scripts/e2e-source-python.sh` with gluonify-source-python, 29/29 on 2026-10-14 (arm64): Charm tokens, Top (`APP_GREETING`), gDown over Bolt+TLS, `uses` inferred by Bup, sleep and wake; the SDK wheel is built from this checkout until it is on PyPI |

## 2. Quick start

```bash
pip install "gluonify[fastapi]"         # gluonify with fastapi and uvicorn
```

```python
from fastapi import Depends
from gluonify import Principal
from gluonify.fastapi import create_app, require_roles, serve

app = create_app("shop", audience="gluonify-shop")


@app.get("/api/hello")
async def hello(p: Principal = Depends(require_roles("shop:read"))):
    return {"hello": p.subject}


if __name__ == "__main__":
    serve(app)  # 0.0.0.0:$PORT (8080), one process
```

## 3. Services: discovery and tokens

`gluonify.endpoint("crystallon")` (or `GluonifyServices().resolve(...)`) returns an `Endpoint` (`url`, `token()`, `headers()`, `resolve(path)`, `source`). Resolution order, exactly as the Java SDK:

1. **Override**: `GLUONIFY_SERVICE_<APP>_URL` in `overrides=` (tests, tools), or in the environment **off the platform**; token `GLUONIFY_<APP>_TOKEN` (fixed) or `GLUONIFY_<APP>_TOKEN_FILE` (re-read at every call), `overrides` first.
2. **Platform** (`GLUONIFY_ENVIRONMENT` present): the address injected for a service of `uses` and the application token `GLUONIFY_<APP>_TOKEN_FILE`, written and renewed by the node agent, **re-read at every call** (a missing or empty file means "no token yet"). Absent: `GluonifyServicesError` saying the platform did not grant the service.
3. **Otherwise**: `GluonifyServicesError` saying which variable to set.

`resolve_optional` returns `None` instead when the service is simply not there. `<APP>` is the upper-case name with `-` turned into `_` (`my-svc` -> `GLUONIFY_SERVICE_MY_SVC_URL`). No token ever appears in a message or a `repr`.

## 4. Charm: incoming tokens

`CharmVerifier.from_env(audience="gluonify-shop")` builds the verifier of the platform: JWKS at `GLUONIFY_SERVICE_CHARM_URL` + `/realms/gluonify/protocol/openid-connect/certs`, issuer `GLUONIFY_OIDC_ISSUER`, audience `GLUONIFY_OIDC_AUDIENCE` (injected by Higgs with the sign-in client of the application; the `audience` argument is only the default). `verify(token)` returns a `Principal` (`subject`, `roles`, `claims`, `organization`) or raises `InvalidTokenError`. Only **ES256** is accepted (`none`, `HS256` refused), `exp` and `sub` are required, 30 s of leeway.

## 5. Top: configuration and secrets

Strange reads the Top namespace of the application before the start and writes its keys into the environment. `Top().get("webhook.key")` reads `APP_WEBHOOK_KEY` (`"top": true`, namespace `<uuid>.app`), then `WEBHOOK_KEY` (literal `topNamespace`): the same code works in both cases, as `${app.webhook.key}` in Quarkus. `require`, `secret` (a `Secret` that never prints) and `require_secret` complete it.

## 6. How to reach gDown

The SDK has **no gDown client**: use the official **`neo4j`** driver directly (or **`neomodel`**, an OGM on top of it, if you prefer mapped classes). Installing either is enough for Bup to infer `?gdown` ([§8](#8-the-uses-markers-contract-read-by-bup)). With `"gdownDatabase": true` the platform injects everything, and Bolt is **TLS only**:

| Variable | Content |
|---|---|
| `GLUONIFY_GDOWN_BOLT_URL` | `bolt+s://<node>:7687`: Bolt with TLS, internal network only |
| `GLUONIFY_GDOWN_CA_FILE` | path of the platform's internal CA (PEM) that signs gDown's certificate |
| `GLUONIFY_GDOWN_DATABASE` | the application's dedicated database |
| `GLUONIFY_GDOWN_USER`, `GLUONIFY_GDOWN_PASSWORD` | its account, limited to that database |

```python
import os

import neo4j  # pip install neo4j  (Bup then infers "?gdown")


def gdown_driver(dev: bool = False) -> neo4j.Driver:
    url = os.environ["GLUONIFY_GDOWN_BOLT_URL"]  # bolt+s://… or neo4j+s://…
    scheme, rest = url.split("://", 1)
    if not scheme.endswith("+s") and not dev:
        raise RuntimeError("gDown: TLS required (bolt+s:// or neo4j+s://)")
    auth = (os.environ["GLUONIFY_GDOWN_USER"], os.environ["GLUONIFY_GDOWN_PASSWORD"])
    ca = os.environ.get("GLUONIFY_GDOWN_CA_FILE")
    if ca and scheme.endswith("+s"):  # the driver refuses a custom CA with "+s": same TLS, explicit trust
        return neo4j.GraphDatabase.driver(
            f"{scheme[:-2]}://{rest}", auth=auth, encrypted=True, trusted_certificates=neo4j.TrustCustomCAs(ca)
        )
    return neo4j.GraphDatabase.driver(url, auth=auth)


driver = gdown_driver()
records, _, _ = driver.execute_query("MATCH (n:Note) RETURN n.title AS title", database_=os.environ["GLUONIFY_GDOWN_DATABASE"])
```

The driver refuses a custom CA with a `+s` scheme, hence `encrypted=True` with `TrustCustomCAs` on the same host and port: the connection is still TLS, verified against the platform CA. Refuse a plain `bolt://` URL outside local development (the `dev` argument). This is the code of [`gluonify-source-python`](https://github.com/gluonify/gluonify-source-python) (`src/gluonify_source/notes/gdown.py`).

## 7. FastAPI

`pip install "gluonify[fastapi]"`, then `from gluonify.fastapi import ...`. Without the extra, `import gluonify.fastapi` raises an `ImportError` naming the missing module and the install command; the rest of `gluonify` never imports FastAPI.

`create_app(title, audience=..., version=..., services=..., dev_identity=...)` returns a FastAPI application with:

- `app.state.gluonify` (`GluonifyServices`), `app.state.top` (`Top`), `app.state.health` (`Health`), `app.state.gluonify_auth` (`GluonifyAuth`, the Charm verifier being built on the **first** authenticated request, so the app starts even if Charm is not ready);
- dependencies `require_roles("a", "b")` (403 `missing role: …`), `current_principal` (401 with `WWW-Authenticate: Bearer`), `optional_principal`; 503 when Charm is not granted;
- health `/health`, `/health/live`, `/health/ready`: `app.state.health.readiness("store", check)`, a check returning `bool` or `(bool, data)`;
- OpenAPI `/openapi.json`, Swagger UI `/docs` (FastAPI's conventional paths: `/q/` belongs to Quarkus; no ReDoc). On the platform (`GLUONIFY_ENVIRONMENT` set) Swagger UI is **read-only**, as for the Quarkus services in production: no Authorize, no Try it out, every request but the contract refused (`swagger_read_only=False` or `True` overrides; `docs_url`/`openapi_url` move them). In PRD the Higgs ingress blocks `/docs` and `/openapi.json` of a Python app on public routes, like `/q/openapi` (an operator can reopen them per route with `allowManagementPaths`); the console reads the contract through Higgs; errors `{"error": "…"}`; an invalid body is a 400 `{"error": "invalid body", "violations": [{"field", "message"}]}`.

`dev_identity` gives an identity to requests **without** a token, for local development only: `create_app` refuses it when `GLUONIFY_ENVIRONMENT` is set. `serve(app)` runs uvicorn on `0.0.0.0:$PORT` (8080 by default), one process.

## 8. The uses markers (contract read by Bup)

Bup infers `uses` for Java from the `META-INF/gluonify/services/<service>` resources of the SDK jars. For Python the marker is an **entry point of the distribution metadata**, group **`gluonify.uses`**:

```toml
# packages/gluonify/pyproject.toml
[project.entry-points."gluonify.uses"]
charm = "gluonify.charm"
```

which each wheel turns into `<name>-<version>.dist-info/entry_points.txt`:

```ini
[gluonify.uses]
charm = gluonify.charm
```

**What Bup does** (the reference reader is `gluonify.markers.infer`, tested here):

1. After `pip install --require-hashes --no-deps -r <lock export>` into the venv of the build (runtime dependencies only, never the development group), Bup lists `<venv>/lib/python3.13/site-packages/*.dist-info/`. **Nothing is imported or executed**: only two text files are read.
2. A distribution is **trusted** when the `Name` of its `METADATA`, normalised by PEP 503 (`[-_.]+` -> `-`, lower case), is `gluonify` or starts with `gluonify-` (setting `gluonify.bup.uses.trusted-python`, default `gluonify`; the counterpart of `gluonify.bup.uses.trusted-groups`). Names under that prefix are reserved on PyPI by the organisation, against typosquatting.
3. In `entry_points.txt`, section `[gluonify.uses]`, each **entry name** is a service (`[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`); the value (the module that reaches it) is informative, for the journal. An invalid name, `higgs`, or an entry of an untrusted distribution is ignored with a warning.
3 bis. **Known client distributions**: whatever their marker, a distribution whose normalised name is `neo4j` or `neomodel` infers **`gdown`** (`usesSources`: `{"gdown": ["neo4j"]}`). Only its `METADATA` is read; nothing is imported. The list lives in `gluonify.markers.CLIENTS`.
4. **Merge**, as for Java: the declared services (request, else `gluonify.json`) as they are, then the inferred ones not declared, as **`?name`** (optional). Result fields `usesInferred`, `usesSources` (`{"charm": ["gluonify"], "gdown": ["neo4j"]}`), `usesEffective`.

What infers what: `gluonify` -> `charm` (its `[gluonify.uses]` entry, JWKS); the distribution `neo4j` or `neomodel` -> `gdown`, so only the apps that really reach gDown get `?gdown`; the extra `fastapi` -> none (it adds dependencies, not markers: `gluonify.fastapi` reaches services only through `gluonify`, whose `charm` marker is there with or without the extra); Top has no marker (the agent injects its values, `"top": true` is not a `uses`). `gdownDatabase` is **not** inferred for Python yet: declare `"gdownDatabase": true` in `gluonify.json`.

## 8 bis. MCP tools of an application

An application can declare **business tools** (diagnostics, internal state, statistics, settings) that a customer's AI assistant calls through **Prism**, the customers' MCP, as `app.<application>.<tool>`. The application manages no AI token, rate limit or audit: Prism does. Specification: `gluonify-codex/app-mcp.md`; the Java counterpart is the Quarkus helper of `gluonify-plasma`, with the same wire behaviour.

```python
from pydantic import BaseModel, Field

from gluonify import Principal
from gluonify.fastapi import create_app, mcp_tool

app = create_app("shop", audience="gluonify-shop", mcp=True)  # mounts POST /mcp


class StatsInput(BaseModel):
    days: int = Field(7, ge=1, le=90)


@mcp_tool(name="order_stats", description="Number of orders per day over the last N days.")
def order_stats(args: StatsInput, caller: Principal) -> dict:
    return {"days": args.days, "orders": count_orders(args.days)}


@mcp_tool(name="reset_cache", description="Empties the product cache.", write=True)
async def reset_cache() -> str:
    ...
    return "cache emptied"
```

**API** (`gluonify.fastapi`):

| Item | Role |
|---|---|
| `@mcp_tool(name=, description=, write=False, input_schema=None, registry=None)` | declares a function (sync or async). `name` defaults to the function name, `description` to the first paragraph of the docstring |
| arguments | at most one parameter for the arguments: annotated with a pydantic `BaseModel` (the input schema is the model's JSON Schema, the arguments are validated against it) or, with `input_schema=` (JSON Schema of type object), a `dict`; no parameter = no argument. A parameter annotated `gluonify.Principal` receives the caller's identity |
| result | a `str` (as is) or anything pydantic serialises to JSON (`BaseModel`, `dict`, `list`...). A `dict`/model result is also returned as `structuredContent`. An exception becomes an `isError` result saying only `the tool failed` (detail in the `gluonify.mcp` log, never to the AI) |
| `mcp_router(registry=None, path="/mcp")` | the `APIRouter` serving `POST /mcp`; the registry is read at each request. `create_app(..., mcp=True)` includes it for the tools of `@mcp_tool` (`mcp=<McpRegistry>` for your own registry) |
| `McpRegistry` | the declared tools (`.tool(...)` decorator, `.tools()`, `.clear()`); `mcp_tool` fills `gluonify.fastapi.mcp.default_registry` |

**Wire behaviour** (identical in Java): JSON-RPC 2.0 over `POST /mcp`, `application/json`, one message per request (no batch, no streaming).

| Message | Answer |
|---|---|
| `initialize` | `protocolVersion` (the client's when it is `2025-06-18`, `2025-03-26` or `2024-11-05`, else `2025-06-18`), `capabilities: {"tools": {"listChanged": false}}`, `serverInfo` |
| `tools/list` | `{"tools": [{"name", "description", "inputSchema", "annotations": {"readOnlyHint": !write}, "write": bool}]}` sorted by name |
| `tools/call` `{"name", "arguments"}` | `{"content": [{"type": "text", "text": ...}], "structuredContent"?: {...}, "isError": false}`; a tool failure or a response over the limit is `"isError": true` with a generic text |
| message without `id` (notification) | `202`, empty body |
| unknown method | JSON-RPC error `-32601` |
| invalid params: missing or non-string `name`, `arguments` not an object, **unknown tool**, arguments invalid for the model, arguments over 64 KiB | JSON-RPC error `-32602` |
| not JSON / not a JSON-RPC 2.0 request | `-32700` / `-32600` |
| body over 68 KiB (64 KiB + envelope) | HTTP `413` `{"error": "request too large"}` |

Errors of JSON-RPC are HTTP 200 with an `error` object; authentication and rights are HTTP errors (below).

**Limits** (constants of `gluonify.fastapi.mcp`, same as Prism and the Java helper): tool name `[a-z][a-z0-9_]{0,40}`; at most **50** tools; input schema at most **16 KiB**; arguments at most **64 KiB**; response at most **256 KiB**. The first three are checked **at declaration** (`ValueError`, so the application does not start with an invalid tool); a name declared twice is refused too.

**Caller identity.** Prism calls with the existing **application-to-service Charm token** (ES256, JWKS, audience of the application), no new header (app-mcp.md decision 3). `POST /mcp` uses the application's `GluonifyAuth` (the dependency `current_principal`): `401` `{"error": ...}` with `WWW-Authenticate: Bearer` without a valid token (missing, bad signature, wrong audience, expired); `503` when Charm is not granted. **Only the relay may call**: on top of that check, the token must carry the claims Higgs puts in application-to-service tokens, `gluonify_kind` = `service` and `gluonify_app` = `prism` (configurable: `mcp_router(relay_app="prism")`, `create_app(..., mcp_relay_app=...)`). Any other valid token (a customer's user token for the same audience, another application's service token, a node token) is `403` `{"error": "only the platform's relay (Prism) may call /mcp"}`; 401 stays for a missing or invalid token. Prism's token may list and call **read** tools. A tool declared `write=True` needs the role **`gluonify:mcp:write`** (`gluonify.fastapi.WRITE_ROLE`) in the `roles` claim of the token: `403` `{"error": "missing role: gluonify:mcp:write"}` otherwise, before the tool runs. That role is the choice of this SDK (app-mcp.md fixes none): Prism puts it in the token of a call it relays for an **approved** write proposal (decision 4), and in no other token. The Java helper must use the same role and the same `403`. Prism's token never reaches the application as the AI's token: the application only sees Prism's own identity, and the response is untrusted data for Prism (it never interprets it).

**Exposure.** `/mcp` is meant for the **internal listener** only; Higgs blocks it at the public ingress in PRD (`ManagementPaths`, like `/health/`), nothing to configure here. It is not in the OpenAPI contract.

**The marker Bup reads.** The distribution carries a second entry-point group, **`gluonify.mcp`** (it is a capability, not a service, so it is **not** in `gluonify.uses` and must never become a `?mcp` service):

```ini
[gluonify.mcp]
mcp = gluonify.fastapi.mcp
```

Same reading rules as the `uses` markers (nothing imported, only `entry_points.txt` of the trusted distributions `gluonify` / `gluonify-*`, PEP 503 names); the reference reader is `gluonify.markers.serves_mcp(site_packages)`. Bup sets `mcp: true` for the application when it finds it (app-mcp.md § 3, decision 2). Limitation: the marker says that the SDK able to serve MCP is installed, not that the application mounted the router (a single distribution; the `fastapi` extra adds no marker). Prism's discovery at deployment (`tools/list`) is the confirmation: an application without `/mcp` answers 404 and nothing is relayed. A stricter signal (a dedicated `gluonify-mcp` distribution behind an extra `gluonify[mcp]`) stays possible if false positives matter.

## 9. Configuration reference

There is nothing to configure on the platform. Variables read:

| Variable | Read by | Role |
|---|---|---|
| `GLUONIFY_ENVIRONMENT` | services | present = on the platform |
| `GLUONIFY_SERVICE_<APP>_URL` | services | address of a service of `uses` |
| `GLUONIFY_<APP>_TOKEN_FILE` / `GLUONIFY_<APP>_TOKEN` | services | application token (file re-read at every call) / fixed token |
| `GLUONIFY_REPLICA`, `GLUONIFY_SELF_URL`, `GLUONIFY_NODE_ADDRESS` | services | instance information |
| `GLUONIFY_OIDC_ISSUER`, `GLUONIFY_OIDC_AUDIENCE` | Charm | expected issuer and audience |
| `APP_<KEY>`, `<KEY>` | Top | configuration and secrets |
| `PORT` | `serve` | listening port (default 8080), host always `0.0.0.0` |

## 10. Development

**Why uv** (and not hatch): uv gives a **workspace** (the distribution in `packages/`, one lock file, editable install) and a **universal lock file with hashes** (`uv.lock`), and `uv export --format requirements-txt` produces the `--require-hashes` file that Bup installs with plain pip (python.md § 3 bis.3). Hatch has no cross-project lock. The build backend stays **hatchling** (standard PEP 517, what PyPI and pip see), so uv is only a development tool.

```bash
uv sync                    # Python 3.13 (.python-version), gluonify[fastapi] in editable mode, dev group
uv run pytest --cov        # 114 tests, coverage >= 90 % required (99 % today)
uv run ruff check . && uv run ruff format --check .
uv run mypy                # strict
uv build --package gluonify  # dist/gluonify-*.whl and sdist
```

Layout: `packages/gluonify/src/gluonify/` (`services.py`, `charm.py`, `top.py`, `markers.py`), `packages/gluonify/src/gluonify/fastapi/` (`auth.py`, `health.py`, `mcp.py`, `app.py`), `tests/` (Charm faked by a P-256 key pair and its JWKS). CI: `.github/workflows/ci.yml` on every push.

## 11. Publishing on PyPI

`.github/workflows/publish.yml` publishes the distribution **`gluonify`** by **Trusted Publishing** (OIDC of GitHub Actions, no token stored) with **PEP 740 attestations**, only on a tag `vX.Y.Z` whose value equals its version, after the tests, in the GitHub environment **`pypi`**, which must require a manual approval. To do once, by the user:

1. **GitHub**, repository `gluonify/gluonify-plasma-python` -> Settings -> Environments -> New environment `pypi` -> *Required reviewers*: yourself; *Deployment branches and tags*: tags `v*` only.
2. **PyPI**, account of the `gluonify` organisation -> *Your projects* -> *Publishing* -> *Add a new pending publisher* (the project does not exist yet), **once**: PyPI project name `gluonify`, owner `gluonify`, repository `gluonify-plasma-python`, workflow `publish.yml`, environment `pypi`. FastAPI is the extra `gluonify[fastapi]`, not a project of its own.
3. Reserve the derived names against typosquatting (python.md question 5), for example `gluonify-fastapi`, `gluonify-gdown`, `gluonify-charm`, `gluonify-top`, `gluonify-sdk`, `gluonify-flask`, as pending publishers too, or as empty placeholder releases.
4. Release: set the version in `packages/gluonify/pyproject.toml` (and `__version__` in `gluonify/__init__.py`), commit, `git tag v0.1.0 && git push origin v0.1.0`, then approve the `pypi` deployment in the Actions tab.
5. Check: `pip download gluonify==0.1.0` and the "Provenance" of the files on PyPI.

## License

Copyright (c) 2026 Gluonify.

This repository is released under the [Apache License, Version 2.0](LICENSE). You may use, modify and
redistribute it, including commercially, under the terms of that license; it is provided "as is",
without warranty. Third-party components remain under their own licenses (see `NOTICE` where present).
