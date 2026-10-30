"""FastAPI endpoints for the Gateway API.

The routes live in job_creation_routes, job_status_routes and
version_routes, which register them on ``router`` when this module imports
them, in that order; this module re-exports the public names.
"""

from policyengine_simulation_gateway.job_creation_routes import (
    submit_budget_window_batch,
    submit_simulation,
)
from policyengine_simulation_gateway.job_status_routes import (
    get_budget_window_job_status,
    get_job_status,
)
from policyengine_simulation_gateway.modal_routing import (
    JOB_METADATA_DICT_NAME,
    POLICYENGINE_VERSION_DICT_NAME,
    ROUTING_STATE_ACTIVE_KEY,
    ROUTING_STATE_DICT_NAME,
    get_app_name,
    resolve_route,
)
from policyengine_simulation_gateway.route_resolution import (
    SUPPORTED_ROUTE_KINDS,
    RouteResolution,
)
from policyengine_simulation_gateway.routes_common import logger, router
from policyengine_simulation_gateway.version_routes import (
    get_country_versions,
    health,
    list_versions,
    ping,
)

__all__ = [
    "JOB_METADATA_DICT_NAME",
    "POLICYENGINE_VERSION_DICT_NAME",
    "ROUTING_STATE_ACTIVE_KEY",
    "ROUTING_STATE_DICT_NAME",
    "SUPPORTED_ROUTE_KINDS",
    "RouteResolution",
    "get_app_name",
    "get_budget_window_job_status",
    "get_country_versions",
    "get_job_status",
    "health",
    "list_versions",
    "logger",
    "ping",
    "resolve_route",
    "router",
    "submit_budget_window_batch",
    "submit_simulation",
]
