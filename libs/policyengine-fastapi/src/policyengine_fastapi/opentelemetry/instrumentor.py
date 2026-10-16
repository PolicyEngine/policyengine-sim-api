"""FastAPI instrumentation that adds per-route OpenTelemetry metrics."""

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from .middleware import Middleware


class FastAPIEnhancedInstrumenter:
    """Enhanced FastAPIInstrumentor that also records per-operation metrics.

    The default instrumentor only produces metrics for the whole API.
    """

    def instrument(self, app: FastAPI) -> None:
        """Instrument ``app`` and add the per-route metrics middleware.

        Call this after every route is registered: routes added later get no
        per-route metrics.
        """
        FastAPIInstrumentor.instrument_app(app)
        middleware = Middleware(app)
        app.middleware("http")(middleware)
