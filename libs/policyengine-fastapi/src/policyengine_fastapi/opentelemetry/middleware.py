from dataclasses import dataclass
from time import time

from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from opentelemetry import metrics
from starlette.middleware.base import RequestResponseEndpoint
from starlette.routing import Match


@dataclass
class Route:
    route: APIRoute
    execution: metrics.Counter
    latency: metrics.Histogram


class Middleware:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.meter = metrics.get_meter("policyengine")

        self.routes = [
            self._create_route(r) for r in app.routes if isinstance(r, APIRoute)
        ]

    def _create_route(self, route: APIRoute) -> Route:
        name = route.name or route.unique_id
        return Route(
            route=route,
            execution=self.meter.create_counter(f"{name}_count"),
            latency=self.meter.create_histogram(f"{name}_latency"),
        )

    async def __call__(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        route: Route | None = None
        for r in self.routes:
            if r.route.matches(request.scope)[0] != Match.NONE:
                route = r
                break

        start = time()
        if route:
            route.execution.add(amount=1)
        try:
            return await call_next(request)
        finally:
            duration = time() - start
            if route:
                route.latency.record(amount=duration)
