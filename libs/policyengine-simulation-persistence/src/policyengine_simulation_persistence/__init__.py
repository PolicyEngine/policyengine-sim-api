"""Tables and connections for the Simulation Entrypoint's ``simulation`` schema.

Importing the package registers every table in ``simulation_metadata``, which
the migration environment compares with the database.
"""

from policyengine_simulation_persistence.budget_window_runs import (
    ONE_REPORT_RUN_PER_YEAR,
    BudgetWindowRunRecord,
    BudgetWindowStatus,
    BudgetWindowYear,
    NewBudgetWindowRun,
    StartedBudgetWindowRun,
    YearBudget,
)
from policyengine_simulation_persistence.budget_window_store import (
    BudgetWindowRunStore,
)
from policyengine_simulation_persistence.catalog_reads import (
    CATALOG_READ_TABLES,
    CatalogError,
    CatalogReader,
    CatalogVersion,
    PolicyParametersMissingError,
    RegionFilter,
    RegionPopulation,
)
from policyengine_simulation_persistence.economy_results import (
    OPTIONAL_SECTIONS,
    RESULT_FIELD_STORAGE,
    EconomyComparisonRecorder,
    EconomyResultLabels,
    GeographyLabel,
    check_storable,
    read_economy_comparison,
    write_economy_comparison,
)
from policyengine_simulation_persistence.engine import (
    create_simulation_engine,
    normalized_database_url,
    read_schema_revision,
)
from policyengine_simulation_persistence.household_results import (
    HouseholdOutput,
    read_household_output,
    write_household_output,
)
from policyengine_simulation_persistence.output_files import (
    OUTPUT_FILE_RETENTION,
    OutputFile,
    OutputFileRecord,
    available_output_files,
    record_output_files,
)
from policyengine_simulation_persistence.reports import (
    MAX_BUDGET_WINDOW_YEARS,
    NewReport,
    ReportKind,
    ReportRecord,
    ReportStore,
)
from policyengine_simulation_persistence.run_store import (
    SUCCEEDED_RUN_REUSE_WINDOW,
    RunStore,
)
from policyengine_simulation_persistence.run_transitions import (
    AbandonedDispatches,
    DueSimulationCall,
    RunError,
    RunErrorCode,
    RunTransitions,
)
from policyengine_simulation_persistence.runs import (
    NewReportRun,
    NewSimulationRun,
    ReportRunRecord,
    RunStatus,
    RunTrigger,
    SimulationRunRecord,
    StartedReportRun,
    StartedSimulationRun,
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
    "CATALOG_READ_TABLES",
    "HEAD_REVISION",
    "MAX_BUDGET_WINDOW_YEARS",
    "ONE_REPORT_RUN_PER_YEAR",
    "OPTIONAL_SECTIONS",
    "OUTPUT_FILE_RETENTION",
    "RESULT_FIELD_STORAGE",
    "RUNTIME_ROLE",
    "SIMULATION_SCHEMA",
    "SUCCEEDED_RUN_REUSE_WINDOW",
    "AbandonedDispatches",
    "BudgetWindowRunRecord",
    "BudgetWindowRunStore",
    "BudgetWindowStatus",
    "BudgetWindowYear",
    "CatalogError",
    "CatalogReader",
    "CatalogVersion",
    "DueSimulationCall",
    "EconomyComparisonRecorder",
    "EconomyResultLabels",
    "GeographyLabel",
    "HouseholdOutput",
    "NewBudgetWindowRun",
    "NewReport",
    "NewReportRun",
    "NewSimulation",
    "NewSimulationRun",
    "OutputFile",
    "OutputFileRecord",
    "PolicyParametersMissingError",
    "RegionFilter",
    "RegionPopulation",
    "ReportKind",
    "ReportRecord",
    "ReportRunRecord",
    "ReportStore",
    "RunError",
    "RunErrorCode",
    "RunStatus",
    "RunStore",
    "RunTransitions",
    "RunTrigger",
    "SimulationKind",
    "SimulationRecord",
    "SimulationRunRecord",
    "SimulationStore",
    "StartedBudgetWindowRun",
    "StartedReportRun",
    "StartedSimulationRun",
    "YearBudget",
    "available_output_files",
    "check_storable",
    "create_simulation_engine",
    "normalized_database_url",
    "read_economy_comparison",
    "read_household_output",
    "read_schema_revision",
    "record_output_files",
    "simulation_metadata",
    "write_economy_comparison",
    "write_household_output",
]
