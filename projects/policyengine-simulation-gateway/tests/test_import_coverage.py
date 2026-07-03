"""Import-coverage tests: parity between the test env and the image env.

CI runs this suite in the environment resolved from this project's
uv.lock — the same lock the Modal image installs with uv_sync(frozen=True).
Importing every module and building the real ASGI app here means a
package missing from the lock fails unit tests deterministically instead
of crashing the deployed container (issue #602).
"""

import importlib
import pkgutil

import policyengine_simulation_gateway
from policyengine_simulation_gateway.testing import create_gateway_app


def test_every_gateway_module_imports():
    for module in pkgutil.walk_packages(
        policyengine_simulation_gateway.__path__,
        prefix="policyengine_simulation_gateway.",
    ):
        importlib.import_module(module.name)


def test_asgi_factory_builds_and_logfire_imports(monkeypatch):
    # The exact #602 crash path: configure_logfire only touches the
    # logfire package lazily; import it explicitly so a broken logfire
    # resolution fails here, not at container startup.
    import logfire  # noqa: F401

    from policyengine_simulation_observability.logfire_legacy import (
        configure_logfire,
    )

    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    assert configure_logfire("import-coverage-test") is False

    app = create_gateway_app()
    assert app.routes
