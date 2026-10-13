"""Health models and the registry behind the ``/ping/alive`` probe."""

from collections.abc import Callable

from pydantic import BaseModel


class ProbeStatus(BaseModel):  # noqa: D101  # docstring would change the published OpenAPI description
    name: str
    healthy: bool
    message: str | None = None


class SystemStatus(BaseModel):  # noqa: D101  # docstring would change the published OpenAPI description
    name: str
    healthy: bool
    detail: list[ProbeStatus]


class HealthStatus(BaseModel):  # noqa: D101  # docstring would change the published OpenAPI description
    healthy: bool
    systems: list[SystemStatus]


HealthProbe = Callable[[], ProbeStatus]


class HealthSystemReporter:
    """Named group of health probes reported together as one system."""

    def __init__(self, name: str, probes: dict[str, HealthProbe]) -> None:
        """Hold the system name and its probes, keyed by probe name."""
        self.name = name
        self.probes = probes

    def report(self) -> SystemStatus:
        """Run every probe; the system is healthy only if all of its probes are."""
        detail = [self.probes[key]() for key in self.probes.keys()]
        first_unhealthy = next((d for d in detail if d.healthy is not True), None)
        return SystemStatus(
            name=self.name, healthy=first_unhealthy is None, detail=detail
        )


class HealthRegistry:
    """Registry of system reporters that together make up the service's health.

    NOTE: any error will result in the service being restarted
    so only report real errors.
    """

    systems: dict[str, HealthSystemReporter]

    def __init__(self) -> None:
        """Start with no systems, which reports as healthy."""
        self.systems = {}

    def register(self, reporter: HealthSystemReporter) -> None:
        """Add a system reporter, replacing any registered under the same name."""
        self.systems[reporter.name] = reporter

    def report(self) -> HealthStatus:
        """Report every system; the service is healthy only if all systems are."""
        system_status = [self.systems[key].report() for key in self.systems.keys()]
        first_unhealthy = next(
            (s for s in system_status if s.healthy is not True), None
        )
        return HealthStatus(healthy=first_unhealthy is None, systems=system_status)
