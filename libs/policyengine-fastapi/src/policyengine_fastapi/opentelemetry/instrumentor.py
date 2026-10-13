from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from .middleware import Middleware


class FastAPIEnhancedInstrumenter:
    """
    Enhances the default FastAPIInstrumentor to generate per operation
    metrics instead of global for the whole api.
    """

    def instrument(self, app: FastAPI) -> None:
        FastAPIInstrumentor.instrument_app(app)
        middleware = Middleware(app)
        app.middleware("http")(middleware)
