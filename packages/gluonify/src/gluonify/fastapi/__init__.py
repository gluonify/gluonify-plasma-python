"""FastAPI integration of the Gluonify SDK: Charm authentication and roles, health endpoints, app factory.

Optional part of the ``gluonify`` distribution: install it with ``pip install "gluonify[fastapi]"``.
"""

try:
    import fastapi as _fastapi  # noqa: F401
    import uvicorn as _uvicorn  # noqa: F401
except ImportError as e:  # tests/test_fastapi.py hides fastapi to check this message
    raise ImportError(f'gluonify.fastapi needs the "fastapi" extra ({e.name} is missing): pip install "gluonify[fastapi]"') from e

from .app import DEFAULT_PORT, PORT_ENV, create_app, serve
from .auth import GluonifyAuth, current_principal, optional_principal, require_roles
from .health import Health

__all__ = [
    "DEFAULT_PORT",
    "PORT_ENV",
    "GluonifyAuth",
    "Health",
    "create_app",
    "current_principal",
    "optional_principal",
    "require_roles",
    "serve",
]
