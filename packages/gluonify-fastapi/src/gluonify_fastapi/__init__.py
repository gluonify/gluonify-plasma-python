"""FastAPI integration of the Gluonify SDK: Charm authentication and roles, health endpoints, app factory."""

from .app import DEFAULT_PORT, PORT_ENV, create_app, serve
from .auth import GluonifyAuth, current_principal, optional_principal, require_roles
from .health import Health

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_PORT",
    "PORT_ENV",
    "GluonifyAuth",
    "Health",
    "__version__",
    "create_app",
    "current_principal",
    "optional_principal",
    "require_roles",
    "serve",
]
