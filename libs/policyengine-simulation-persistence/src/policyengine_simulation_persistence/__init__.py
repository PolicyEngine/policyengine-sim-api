"""Tables and connections for the Simulation Entrypoint's ``simulation`` schema.

Importing the package registers every table in ``simulation_metadata``, which
the migration environment compares with the database.
"""

from policyengine_simulation_persistence.engine import (
    create_simulation_engine,
    normalized_database_url,
    read_schema_revision,
)
from policyengine_simulation_persistence.reports import (
    NewReport,
    ReportKind,
    ReportRecord,
    ReportStore,
)
from policyengine_simulation_persistence.schema import (
    ALEMBIC_VERSION_TABLE,
    HEAD_REVISION,
    RUNTIME_ROLE,
    SIMULATION_SCHEMA,
    simulation_metadata,
)
from policyengine_simulation_persistence.simulations import (
    NewSimulation,
    SimulationKind,
    SimulationRecord,
    SimulationStore,
)

__all__ = [
    "ALEMBIC_VERSION_TABLE",
    "HEAD_REVISION",
    "RUNTIME_ROLE",
    "SIMULATION_SCHEMA",
    "NewReport",
    "NewSimulation",
    "ReportKind",
    "ReportRecord",
    "ReportStore",
    "SimulationKind",
    "SimulationRecord",
    "SimulationStore",
    "create_simulation_engine",
    "normalized_database_url",
    "read_schema_revision",
    "simulation_metadata",
]
