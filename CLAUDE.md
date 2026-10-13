# CLAUDE.md: gluonify-plasma-python

The **Python SDK** of Gluonify (two distributions, `gluonify` and `gluonify-fastapi`), the counterpart of the Java SDK `gluonify-plasma` for Python applications run by path 3 of `gluonify-codex/python.md` (§ 3 bis: CPython 3.13 of Debian, FastAPI). Read `README.md` first. This is an **"en" repository**: code, comments, README, this file and commit messages in English.

Shared context for every gluonify-* repository (rules, conventions, Edge model): `/Users/a/dev/gluonify/CLAUDE.md` (in French).

## Commands
- `uv sync` then `uv run pytest --cov` (102 tests, coverage gate 90 %), `uv run ruff check . && uv run ruff format --check .`, `uv run mypy` (strict). Python 3.13 (`.python-version`). uv is the workspace and lock tool; hatchling is the build backend.
- `uv build --all-packages -o dist`: both wheels. Never publish by hand: `publish.yml` (tag `vX.Y.Z`, environment `pypi` with manual approval, Trusted Publishing).

## Rules
- **Mirror the Java SDK**: same variable names, same resolution order, same messages as `gluonify-plasma/services/.../GluonifyServices.java`. A change of contract is made in Java and Python together.
- **Zero configuration**: never add a setting for something the platform injects. No token, password or secret in a message, a log or a `repr` (tests check it).
- **Token files are re-read at every call** (the node agent renews them); a missing or empty file means "no token yet", not an error.
- **The `[gluonify.uses]` entry points are a contract read by Bup** (README § 8, python.md § 3 bis.6): adding a client that reaches a new service adds its marker in the distribution that reaches it; `gluonify-fastapi` carries none; never `higgs`. `tests/test_markers.py` guards it.
- Charm: ES256 only, `exp` and `sub` required. Do not loosen it.
- `create_app` refuses a development identity on the platform (`GLUONIFY_ENVIRONMENT` set): keep it that way.
- README "What is supported" matrix, the configuration table and `plan.md` are updated with every change.
- Commit only on request; never push from an agent.
