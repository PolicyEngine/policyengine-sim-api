from collections.abc import Callable

from pydantic import BaseModel


class ProbeStatus(BaseModel):
    name: str
    healthy: bool
    message: str | None = None


class SystemStatus(BaseModel):
    name: str
    healthy: bool
    detail: list[ProbeStatus]


class HealthStatus(BaseModel):
    healthy: bool
    systems: list[SystemStatus]


HealthProbe = Callable[[], ProbeStatus]


class HealthSystemReporter:
    def __init__(self, name: str, probes: dict[str, HealthProbe]) -> None:
        self.name = name
        self.probes = probes

    def report(self) -> SystemStatus:
        detail = [self.probes[key]() for key in self.probes.keys()]
        first_unhealthy = next((d for d in detail if d.healthy is not True), None)
        return SystemStatus(
            name=self.name, healthy=first_unhealthy is None, detail=detail
        )


class HealthRegistry:
    """
    Mechanism for collecting and reporting sevice health.
    NOTE: any error will result in the service being restarted
    so only report real errors.
    """

    systems: dict[str, HealthSystemReporter]

    def __init__(self) -> None:
        self.systems = {}

    def register(self, reporter: HealthSystemReporter) -> None:
        self.systems[reporter.name] = reporter

    def report(self) -> HealthStatus:
        system_status = [self.systems[key].report() for key in self.systems.keys()]
        first_unhealthy = next(
            (s for s in system_status if s.healthy is not True), None
        )
        return HealthStatus(healthy=first_unhealthy is None, systems=system_status)
