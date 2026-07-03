# policyengine-simulation-observability

Shared observability plumbing for the simulation service (gateway and
executor): the `policyengine-observability` configuration wrapper, legacy
Logfire helpers, error redaction/reporting, and the telemetry envelope.

This lives in its own lib because it has a different reason to change than
the gateway↔executor contract: Logfire is retained only while a replacement
observability platform is evaluated, so the legacy pieces here have a
planned removal date — and this dependency cluster (logfire,
policyengine-observability) is the one that caused issue #602, so it is
quarantined behind one boundary.
