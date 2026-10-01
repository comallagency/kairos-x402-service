"""Importing this package populates app.purecalc.registry.COMPUTE_SPECS -
every category module below calls register(...) at import time. Imported
once from app/main.py before build_compute_routers()/build_compute_route_configs()
are ever called."""

from app.purecalc.routes import geo  # noqa: F401
from app.purecalc.routes import dates  # noqa: F401
