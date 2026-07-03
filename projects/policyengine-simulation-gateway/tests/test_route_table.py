"""The real router and the OpenAPI stub app must expose the same routes.

generate_openapi.py rebuilds the route signatures without Modal
dependencies; this pins it against drifting from the deployed router.
"""

from fastapi.routing import APIRoute

from policyengine_simulation_gateway.generate_openapi import create_openapi_app
from policyengine_simulation_gateway.testing import create_gateway_app

EXPECTED_ROUTES = {
    ("GET", "/budget-window-jobs/{batch_job_id}"),
    ("GET", "/health"),
    ("GET", "/jobs/{job_id}"),
    ("GET", "/versions"),
    ("GET", "/versions/{kind}"),
    ("POST", "/ping"),
    ("POST", "/simulate/economy/budget-window"),
    ("POST", "/simulate/economy/comparison"),
}


def route_set(app):
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def test_gateway_router_exposes_expected_routes():
    assert route_set(create_gateway_app()) == EXPECTED_ROUTES


def test_openapi_stub_matches_real_router():
    assert route_set(create_openapi_app()) == route_set(create_gateway_app())
