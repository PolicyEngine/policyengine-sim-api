"""One-time migration provisioning: v1 report definitions into the simulation schema.

An operator runs this once per environment, during the one-time migration. It
reads the Public API's export of v1 report definitions and creates each
report's simulations and the report itself, with no run. Item 5.04 removes this
package. ``README.md`` next to it describes the input file and how to run it.

It needs the library's ``provisioning`` extra, which installs Pydantic. The
library's own package does not import it.
"""

from policyengine_simulation_persistence.migration_provisioning.definitions import (
    REPORT_DEFINITIONS_SCHEMA_VERSION,
    EconomyReportDefinition,
    EconomySimulationDefinition,
    HouseholdReportDefinition,
    HouseholdSimulationDefinition,
    ReportDefinition,
    ReportDefinitionsFile,
    SimulationDefinition,
)
from policyengine_simulation_persistence.migration_provisioning.provisioning import (
    ProvisionedReport,
    ProvisioningFailure,
    ProvisioningResult,
    provision_reports,
)

__all__ = [
    "REPORT_DEFINITIONS_SCHEMA_VERSION",
    "EconomyReportDefinition",
    "EconomySimulationDefinition",
    "HouseholdReportDefinition",
    "HouseholdSimulationDefinition",
    "ProvisionedReport",
    "ProvisioningFailure",
    "ProvisioningResult",
    "ReportDefinition",
    "ReportDefinitionsFile",
    "SimulationDefinition",
    "provision_reports",
]
