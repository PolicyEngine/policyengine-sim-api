"""The foreign keys from the service's tables to the Public API's own tables.

Each service writes only its own tables, and the foreign keys between them
stay, so PostgreSQL checks that every row the service writes names rows of
the Public API's that exist. Tests expect exactly these keys.
"""

from __future__ import annotations

# Every key from the service's tables to the Public API's own, by its table
# and its name as the Public API's chain gives it (a name is unique only
# within its table): a name past PostgreSQL's 63 characters is shortened and
# ends in four characters of its hash.
KEYS_TO_PUBLIC_API_TABLES = {
    ("simulations", "fk_simulations_dataset_id_datasets"): (
        ("dataset_id",),
        "datasets(id)",
        "RESTRICT",
    ),
    ("simulations", "fk_simulations_household_id_households"): (
        ("household_id",),
        "households(id)",
        "RESTRICT",
    ),
    ("simulations", "fk_simulations_policy_id_policies"): (
        ("policy_id",),
        "policies(id)",
        "SET NULL",
    ),
    ("simulations", "fk_simulations_dynamic_id_dynamics"): (
        ("dynamic_id",),
        "dynamics(id)",
        "SET NULL",
    ),
    ("simulations", "fk_simulations_tax_benefit_model_version_id_tax_benefit_84f8"): (
        ("tax_benefit_model_version_id",),
        "tax_benefit_model_versions(id)",
        "RESTRICT",
    ),
    ("simulations", "fk_simulations_region_id_regions"): (
        ("region_id",),
        "regions(id)",
        "SET NULL",
    ),
    ("reports", "fk_reports_user_id_users"): (("user_id",), "users(id)", "SET NULL"),
    ("reports", "fk_reports_tax_benefit_model_id_tax_benefit_models"): (
        ("tax_benefit_model_id",),
        "tax_benefit_models(id)",
        "RESTRICT",
    ),
    ("reports", "fk_reports_policy_id_policies"): (
        ("policy_id",),
        "policies(id)",
        "SET NULL",
    ),
    ("reports", "fk_reports_household_id_households"): (
        ("household_id",),
        "households(id)",
        "RESTRICT",
    ),
    ("reports", "fk_reports_dataset_id_datasets"): (
        ("dataset_id",),
        "datasets(id)",
        "SET NULL",
    ),
    ("reports", "fk_reports_region_id_regions"): (
        ("region_id",),
        "regions(id)",
        "SET NULL",
    ),
    (
        "simulation_runs",
        "fk_simulation_runs_tax_benefit_model_version_id_tax_ben_1a94",
    ): (
        ("tax_benefit_model_version_id",),
        "tax_benefit_model_versions(id)",
        "RESTRICT",
    ),
    ("simulation_runs", "fk_simulation_runs_dataset_id_datasets"): (
        ("dataset_id",),
        "datasets(id)",
        "RESTRICT",
    ),
    ("simulation_runs", "fk_simulation_runs_region_id_regions"): (
        ("region_id",),
        "regions(id)",
        "SET NULL",
    ),
    ("household_jobs", "fk_household_jobs_household_id_households"): (
        ("household_id",),
        "households(id)",
        "RESTRICT",
    ),
    ("household_jobs", "fk_household_jobs_policy_id_policies"): (
        ("policy_id",),
        "policies(id)",
        "SET NULL",
    ),
    ("household_jobs", "fk_household_jobs_dynamic_id_dynamics"): (
        ("dynamic_id",),
        "dynamics(id)",
        "SET NULL",
    ),
    (
        "household_jobs",
        "fk_household_jobs_tax_benefit_model_version_id_tax_bene_82b2",
    ): (
        ("tax_benefit_model_version_id",),
        "tax_benefit_model_versions(id)",
        "RESTRICT",
    ),
    (
        "budget_window_runs",
        "fk_budget_window_runs_tax_benefit_model_version_id_tax__5106",
    ): (
        ("tax_benefit_model_version_id",),
        "tax_benefit_model_versions(id)",
        "RESTRICT",
    ),
}
