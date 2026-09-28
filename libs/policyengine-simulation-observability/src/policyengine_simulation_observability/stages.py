"""Canonical stage registry for every simulation execution configuration.

Runtime code imports the named registry entry for the configuration it is
executing.  This keeps trace span names and their expected order in one place
instead of distributing string literals across the gateway and workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class RunConfiguration(StrEnum):
    ANNUAL_IMPACT = "annual_impact"
    BUDGET_WINDOW = "budget_window"
    SEGMENTED_NATIONAL = "segmented_national"
    STAGE12_SHADOW_REPORT = "stage12_shadow_report"
    STAGE12_CANONICAL_REPORT = "stage12_canonical_report"
    STAGE12_SIMULATION = "stage12_simulation"


class Stage(StrEnum):
    ANNUAL_EXECUTION = "run_simulation"
    BUDGET_WINDOW_EXECUTION = "run_budget_window_batch"
    SEGMENTED_NATIONAL_EXECUTION = "run_simulation_segment"

    REQUEST_PARSE = "request_parse"
    ROUTE_RESOLUTION = "route_resolution"
    POLICYENGINE_BUNDLE = "policyengine_bundle"
    MODAL_FUNCTION_SPAWN = "modal_function_spawn"
    MODAL_DICT_READ = "modal_dict_read"
    MODAL_JOB_METADATA_WRITE = "modal_job_metadata_write"
    MODAL_JOB_STATUS_POLL = "modal_job_status_poll"

    CREDENTIAL_SETUP = "credential_setup"
    COUNTRY_MODULE_LOAD = "country_module_load"
    REGION_RESOLUTION = "region_resolution"
    DATASET_RESOLUTION = "dataset_resolution"
    DATASET_LOAD = "dataset_load"
    POLICY_NORMALIZATION = "policy_normalization"
    SIMULATION_BUILD = "simulation_build"
    CALCULATION = "calculation"
    RESPONSE_SERIALIZATION = "response_serialization"

    BUDGET_WINDOW_CONTEXT = "budget_window_context"
    BUDGET_WINDOW_STATE_LOAD = "budget_window_state_load"
    BUDGET_WINDOW_STATE_WRITE = "budget_window_state_write"
    BUDGET_WINDOW_CHILD_SPAWN = "budget_window_child_spawn"
    BUDGET_WINDOW_RESULT_PARSE = "budget_window_result_parse"
    BUDGET_WINDOW_AGGREGATION = "budget_window_aggregation"
    BUDGET_WINDOW_CHILD_REQUEST_BUILD = "budget_window_child_request_build"
    BUDGET_WINDOW_STATUS_SERIALIZATION = "budget_window_status_serialization"

    SEGMENTED_NATIONAL_CHILD_SPAWN = "segmented_national_child_spawn"
    SEGMENTED_NATIONAL_REDUCE = "segmented_national_reduce"
    MODAL_JOB_METADATA_READ = "modal_job_metadata_read"

    SIMULATION_OUTPUT_BUILD = "simulation_output_build"
    SIMULATION_OUTPUT_MODEL_DUMP = "simulation_output_model_dump"
    ECONOMIC_IMPACT_ANALYSIS = "economic_impact_analysis"
    OUTPUT_BUDGETARY_IMPACT = "output_budgetary_impact"
    OUTPUT_DETAILED_BUDGET = "output_detailed_budget"
    OUTPUT_DECILE = "output_decile"
    OUTPUT_INEQUALITY = "output_inequality"
    OUTPUT_POVERTY = "output_poverty"
    OUTPUT_INTRA_DECILE = "output_intra_decile"
    OUTPUT_WEALTH_DECILE = "output_wealth_decile"
    OUTPUT_INTRA_WEALTH_DECILE = "output_intra_wealth_decile"
    OUTPUT_LABOR_SUPPLY = "output_labor_supply"
    OUTPUT_CONGRESSIONAL_DISTRICT = "output_congressional_district"
    OUTPUT_UK_CONSTITUENCY = "output_uk_constituency"
    OUTPUT_UK_LOCAL_AUTHORITY = "output_uk_local_authority"
    OUTPUT_CLIFF = "output_cliff"
    OUTPUT_MODEL_VERSION = "output_model_version"
    OUTPUT_DATA_VERSION = "output_data_version"

    STAGE12_ENTRY_DISPATCH = "stage12_entry_dispatch"
    STAGE12_COORDINATOR_EXECUTION = "stage12_coordinator_execution"
    STAGE12_COORDINATOR_CLAIM = "stage12_coordinator_claim"
    STAGE12_CHILD_STATE_CREATE = "stage12_child_state_create"
    STAGE12_CHILD_DISPATCH = "stage12_child_dispatch"
    STAGE12_CHILD_WAIT = "stage12_child_wait"
    STAGE12_INPUT_WRITE = "stage12_input_write"
    STAGE12_SIMULATION_EXECUTION = "stage12_simulation_execution"
    STAGE12_CALCULATION = "stage12_calculation"
    STAGE12_SIMULATION_ARTIFACT_WRITE = "stage12_simulation_artifact_write"
    STAGE12_ARTIFACT_READ = "stage12_artifact_read"
    STAGE12_AGGREGATION = "stage12_aggregation"
    STAGE12_AGGREGATE_ARTIFACT_WRITE = "stage12_aggregate_artifact_write"
    STAGE12_RESULT_COMPARISON = "stage12_result_comparison"


@dataclass(frozen=True)
class StagePlan:
    configuration: RunConfiguration
    stages: tuple[Stage, ...]

    def name(self, stage: Stage) -> str:
        """Return a configured stage name, rejecting unregistered use."""

        if stage not in self.stages:
            raise ValueError(
                f"{stage.value!r} is not registered for {self.configuration.value!r}"
            )
        return stage.value


_COMMON_SIMULATION = (
    Stage.CREDENTIAL_SETUP,
    Stage.COUNTRY_MODULE_LOAD,
    Stage.REGION_RESOLUTION,
    Stage.DATASET_RESOLUTION,
    Stage.DATASET_LOAD,
    Stage.POLICY_NORMALIZATION,
    Stage.SIMULATION_BUILD,
    Stage.CALCULATION,
    Stage.SIMULATION_OUTPUT_BUILD,
    Stage.ECONOMIC_IMPACT_ANALYSIS,
    Stage.OUTPUT_MODEL_VERSION,
    Stage.OUTPUT_DATA_VERSION,
    Stage.OUTPUT_BUDGETARY_IMPACT,
    Stage.OUTPUT_DETAILED_BUDGET,
    Stage.OUTPUT_DECILE,
    Stage.OUTPUT_INEQUALITY,
    Stage.OUTPUT_POVERTY,
    Stage.OUTPUT_INTRA_DECILE,
    Stage.OUTPUT_WEALTH_DECILE,
    Stage.OUTPUT_INTRA_WEALTH_DECILE,
    Stage.OUTPUT_LABOR_SUPPLY,
    Stage.OUTPUT_CONGRESSIONAL_DISTRICT,
    Stage.OUTPUT_UK_CONSTITUENCY,
    Stage.OUTPUT_UK_LOCAL_AUTHORITY,
    Stage.OUTPUT_CLIFF,
    Stage.RESPONSE_SERIALIZATION,
    Stage.SIMULATION_OUTPUT_MODEL_DUMP,
)

_ANNUAL_IMPACT = (
    Stage.ANNUAL_EXECUTION,
    Stage.ROUTE_RESOLUTION,
    Stage.REQUEST_PARSE,
    Stage.POLICYENGINE_BUNDLE,
    Stage.MODAL_FUNCTION_SPAWN,
    Stage.MODAL_JOB_METADATA_WRITE,
    Stage.MODAL_JOB_METADATA_READ,
    Stage.MODAL_JOB_STATUS_POLL,
    Stage.MODAL_DICT_READ,
    *_COMMON_SIMULATION,
)

_BUDGET_WINDOW = (
    Stage.BUDGET_WINDOW_EXECUTION,
    Stage.ROUTE_RESOLUTION,
    Stage.POLICYENGINE_BUNDLE,
    Stage.REQUEST_PARSE,
    Stage.MODAL_FUNCTION_SPAWN,
    Stage.MODAL_JOB_METADATA_WRITE,
    Stage.BUDGET_WINDOW_CONTEXT,
    Stage.BUDGET_WINDOW_STATE_LOAD,
    Stage.BUDGET_WINDOW_STATE_WRITE,
    Stage.BUDGET_WINDOW_CHILD_REQUEST_BUILD,
    Stage.BUDGET_WINDOW_CHILD_SPAWN,
    Stage.BUDGET_WINDOW_RESULT_PARSE,
    Stage.BUDGET_WINDOW_AGGREGATION,
    Stage.BUDGET_WINDOW_STATUS_SERIALIZATION,
    Stage.MODAL_JOB_STATUS_POLL,
    *_COMMON_SIMULATION,
)

_SEGMENTED_NATIONAL = (
    Stage.SEGMENTED_NATIONAL_EXECUTION,
    Stage.REQUEST_PARSE,
    Stage.SEGMENTED_NATIONAL_CHILD_SPAWN,
    Stage.MODAL_JOB_STATUS_POLL,
    Stage.SEGMENTED_NATIONAL_REDUCE,
    *_COMMON_SIMULATION,
)

_STAGE12_COMMON = (
    Stage.STAGE12_ENTRY_DISPATCH,
    Stage.STAGE12_COORDINATOR_EXECUTION,
    Stage.STAGE12_COORDINATOR_CLAIM,
    Stage.STAGE12_CHILD_STATE_CREATE,
    Stage.STAGE12_CHILD_DISPATCH,
    Stage.STAGE12_CHILD_WAIT,
    Stage.STAGE12_ARTIFACT_READ,
    Stage.STAGE12_AGGREGATION,
    Stage.STAGE12_AGGREGATE_ARTIFACT_WRITE,
)

RUN_STAGE_REGISTRY: Mapping[RunConfiguration, StagePlan] = MappingProxyType(
    {
        RunConfiguration.ANNUAL_IMPACT: StagePlan(
            RunConfiguration.ANNUAL_IMPACT, _ANNUAL_IMPACT
        ),
        RunConfiguration.BUDGET_WINDOW: StagePlan(
            RunConfiguration.BUDGET_WINDOW, _BUDGET_WINDOW
        ),
        RunConfiguration.SEGMENTED_NATIONAL: StagePlan(
            RunConfiguration.SEGMENTED_NATIONAL, _SEGMENTED_NATIONAL
        ),
        RunConfiguration.STAGE12_SHADOW_REPORT: StagePlan(
            RunConfiguration.STAGE12_SHADOW_REPORT,
            (*_STAGE12_COMMON, Stage.STAGE12_RESULT_COMPARISON),
        ),
        RunConfiguration.STAGE12_CANONICAL_REPORT: StagePlan(
            RunConfiguration.STAGE12_CANONICAL_REPORT, _STAGE12_COMMON
        ),
        RunConfiguration.STAGE12_SIMULATION: StagePlan(
            RunConfiguration.STAGE12_SIMULATION,
            (
                Stage.STAGE12_SIMULATION_EXECUTION,
                Stage.STAGE12_INPUT_WRITE,
                Stage.CREDENTIAL_SETUP,
                Stage.COUNTRY_MODULE_LOAD,
                Stage.REGION_RESOLUTION,
                Stage.DATASET_RESOLUTION,
                Stage.DATASET_LOAD,
                Stage.POLICY_NORMALIZATION,
                Stage.SIMULATION_BUILD,
                Stage.STAGE12_CALCULATION,
                Stage.STAGE12_SIMULATION_ARTIFACT_WRITE,
            ),
        ),
    }
)

ANNUAL_IMPACT_STAGES = RUN_STAGE_REGISTRY[RunConfiguration.ANNUAL_IMPACT]
BUDGET_WINDOW_STAGES = RUN_STAGE_REGISTRY[RunConfiguration.BUDGET_WINDOW]
SEGMENTED_NATIONAL_STAGES = RUN_STAGE_REGISTRY[RunConfiguration.SEGMENTED_NATIONAL]
STAGE12_SHADOW_REPORT_STAGES = RUN_STAGE_REGISTRY[
    RunConfiguration.STAGE12_SHADOW_REPORT
]
STAGE12_CANONICAL_REPORT_STAGES = RUN_STAGE_REGISTRY[
    RunConfiguration.STAGE12_CANONICAL_REPORT
]
STAGE12_SIMULATION_STAGES = RUN_STAGE_REGISTRY[RunConfiguration.STAGE12_SIMULATION]
