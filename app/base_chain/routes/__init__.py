"""Importing this package populates app.base_chain.registry.BASE_RPC_SPECS -
every module below calls register(...) at import time. Imported once from
app/main.py before build_rpc_routers()/build_rpc_route_configs() are ever
called."""

from app.base_chain.routes import chain_meta  # noqa: F401
from app.base_chain.routes import blocks  # noqa: F401
from app.base_chain.routes import accounts  # noqa: F401
from app.base_chain.routes import transactions  # noqa: F401
from app.base_chain.routes import tokens  # noqa: F401
from app.base_chain.routes import logs  # noqa: F401
from app.base_chain.routes import gas  # noqa: F401
