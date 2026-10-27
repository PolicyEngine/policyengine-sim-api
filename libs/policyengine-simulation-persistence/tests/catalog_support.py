"""Public API catalog and policy rows for the catalog read tests.

Two published PolicyEngine.py versions of the US catalog (6.5.0 and 6.7.5)
and one of the UK's (6.7.5), each with its own regions, datasets and
parameters, as each publication creates new rows; a US policy saved on 6.5.0,
another naming a parameter 6.7.5 no longer has, and a dynamic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from public_api_support import superuser_engine
from sqlalchemy import text

CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
INCOME_ELASTICITY = "gov.simulation.labor_supply_responses.elasticities.income"
RETIRED_PARAMETER = "gov.irs.credits.retired_credit.amount"
US_STATES = {
    "state/ca": ("6", "CA"),
    "state/ny": ("36", "NY"),
    "state/de": ("10", "DE"),
}
ENGLAND_REGIONS = '["NORTH_EAST","LONDON"]'


@dataclass(frozen=True)
class CatalogRows:
    """The identifiers of the inserted catalog and policy rows."""

    us_version_id: UUID
    older_us_version_id: UUID
    uk_version_id: UUID
    us_national_dataset_id: UUID
    us_state_dataset_id: UUID
    uk_dataset_id: UUID
    policy_id: UUID
    retired_policy_id: UUID
    dynamic_id: UUID


def _datetime(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


class _Inserts:
    """Collect insert statements and run them as the tables' owner."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, object]]] = []

    def add(self, statement: str, **parameters: object) -> None:
        self.statements.append((statement, parameters))

    def model(self, name: str) -> UUID:
        model_id = uuid4()
        self.add(
            "INSERT INTO public.tax_benefit_models (id, name) VALUES (:id, :name)",
            id=model_id,
            name=name,
        )
        return model_id

    def version(self, model_id: UUID, version: str) -> UUID:
        version_id = uuid4()
        self.add(
            "INSERT INTO public.tax_benefit_model_versions (id, model_id, version) "
            "VALUES (:id, :model, :version)",
            id=version_id,
            model=model_id,
            version=version,
        )
        return version_id

    def dataset(self, version_id: UUID, name: str) -> UUID:
        dataset_id = uuid4()
        self.add(
            "INSERT INTO public.datasets (id, name, tax_benefit_model_version_id) "
            "VALUES (:id, :name, :version)",
            id=dataset_id,
            name=name,
            version=version_id,
        )
        return dataset_id

    def region(  # noqa: PLR0913  # one keyword per region column
        self,
        version_id: UUID,
        code: str,
        dataset_id: UUID,
        *,
        label: str,
        region_type: str,
        row_filter: tuple[str, str] | None = None,
        filter_strategy: str = "row_filter",
        state_code: str | None = None,
    ) -> None:
        self.add(
            "INSERT INTO public.regions (id, code, tax_benefit_model_version_id, "
            "default_dataset_id, label, region_type, requires_filter, filter_field, "
            "filter_value, filter_strategy, state_code) VALUES (gen_random_uuid(), "
            ":code, :version, :dataset, :label, :type, :filtered, :field, :value, "
            ":strategy, :state)",
            code=code,
            version=version_id,
            dataset=dataset_id,
            label=label,
            type=region_type,
            filtered=row_filter is not None,
            field=None if row_filter is None else row_filter[0],
            value=None if row_filter is None else row_filter[1],
            strategy=None if row_filter is None else filter_strategy,
            state=state_code,
        )

    def parameter(self, version_id: UUID, name: str) -> UUID:
        parameter_id = uuid4()
        self.add(
            "INSERT INTO public.parameters (id, name, tax_benefit_model_version_id) "
            "VALUES (:id, :name, :version)",
            id=parameter_id,
            name=name,
            version=version_id,
        )
        return parameter_id

    def value(  # noqa: PLR0913  # one keyword per value column
        self,
        parameter_id: UUID,
        value: object,
        start: str,
        end: str | None = None,
        *,
        policy_id: UUID | None = None,
        dynamic_id: UUID | None = None,
    ) -> None:
        self.add(
            "INSERT INTO public.parameter_values (id, parameter_id, value_json, "
            "start_date, end_date, policy_id, dynamic_id) VALUES (gen_random_uuid(), "
            ":parameter, CAST(:value AS json), :start, :end, :policy, :dynamic)",
            parameter=parameter_id,
            value=json.dumps(value),
            start=_datetime(start),
            end=None if end is None else _datetime(end),
            policy=policy_id,
            dynamic=dynamic_id,
        )

    def run(self, superuser_url: str) -> None:
        engine = superuser_engine(superuser_url)
        try:
            with engine.begin() as connection:
                for statement, parameters in self.statements:
                    connection.execute(text(statement), parameters)
        finally:
            engine.dispose()


def _us_regions(
    inserts: _Inserts, version_id: UUID, national: UUID, states: UUID
) -> None:
    inserts.region(
        version_id, "us", national, label="United States", region_type="national"
    )
    for code, (fips, state_code) in US_STATES.items():
        inserts.region(
            version_id,
            code,
            states,
            label=code,
            region_type="state",
            row_filter=("state_fips", fips),
            state_code=state_code,
        )
    inserts.region(
        version_id,
        "congressional_district/CA-01",
        states,
        label="California's 1st congressional district",
        region_type="congressional_district",
        row_filter=("congressional_district_geoid", "601"),
        state_code="CA",
    )
    # The current catalog publishes US places with no household filter.
    inserts.region(
        version_id,
        "place/CA-44000",
        national,
        label="A place without a household filter",
        region_type="place",
        state_code="CA",
    )
    inserts.region(
        version_id,
        "place/CA-57000",
        national,
        label="A place filtered by weights",
        region_type="place",
        row_filter=("place_fips", "57000"),
        filter_strategy="weight_replacement",
        state_code="CA",
    )


def _uk_regions(inserts: _Inserts, version_id: UUID, dataset: UUID) -> None:
    inserts.region(
        version_id, "uk", dataset, label="United Kingdom", region_type="national"
    )
    inserts.region(
        version_id,
        "country/wales",
        dataset,
        label="Wales",
        region_type="country",
        row_filter=("region", "WALES"),
    )
    inserts.region(
        version_id,
        "country/england",
        dataset,
        label="England",
        region_type="country",
        row_filter=("region", ENGLAND_REGIONS),
    )
    inserts.region(
        version_id,
        "constituency/E14001063",
        dataset,
        label="Aldershot",
        region_type="constituency",
        row_filter=("constituency_code_oa", "E14001063"),
    )
    inserts.region(
        version_id,
        "local_authority/E06000063",
        dataset,
        label="Cumberland",
        region_type="local_authority",
        row_filter=("la_code_oa", "E06000063"),
    )


def insert_catalog_rows(superuser_url: str) -> CatalogRows:
    """Insert the catalog versions, regions, datasets, parameters and policies."""
    inserts = _Inserts()
    us_model = inserts.model("policyengine-us")
    uk_model = inserts.model("policyengine-uk")
    us_version = inserts.version(us_model, "6.7.5")
    older_us_version = inserts.version(us_model, "6.5.0")
    uk_version = inserts.version(uk_model, "6.7.5")
    us_national = inserts.dataset(us_version, "populace_us_2024")
    us_states = inserts.dataset(us_version, "populace_us_2024_acs_local")
    older_national = inserts.dataset(older_us_version, "populace_us_2024")
    uk_dataset = inserts.dataset(uk_version, "enhanced_frs_2023_24")
    _us_regions(inserts, us_version, us_national, us_states)
    inserts.region(
        older_us_version,
        "state/ca",
        older_national,
        label="California",
        region_type="state",
        row_filter=("state_fips", "6"),
        state_code="CA",
    )
    _uk_regions(inserts, uk_version, uk_dataset)
    for name in (CTC_AMOUNT, INCOME_ELASTICITY):
        inserts.parameter(us_version, name)
    older_ctc = inserts.parameter(older_us_version, CTC_AMOUNT)
    older_elasticity = inserts.parameter(older_us_version, INCOME_ELASTICITY)
    retired = inserts.parameter(older_us_version, RETIRED_PARAMETER)
    policy_id, retired_policy_id, dynamic_id = uuid4(), uuid4(), uuid4()
    for row_id in (policy_id, retired_policy_id):
        inserts.add(
            "INSERT INTO public.policies (id, country_id, "
            "tax_benefit_model_version_id) VALUES (:id, 'us', :version)",
            id=row_id,
            version=older_us_version,
        )
    inserts.add("INSERT INTO public.dynamics (id) VALUES (:id)", id=dynamic_id)
    inserts.value(older_ctc, 3000, "2026-01-01", policy_id=policy_id)
    inserts.value(older_ctc, 2500, "2025-01-01", "2025-12-31", policy_id=policy_id)
    inserts.value(older_ctc, 3000, "2026-01-01", policy_id=retired_policy_id)
    inserts.value(retired, 100, "2026-01-01", policy_id=retired_policy_id)
    inserts.value(older_elasticity, 0.1, "2026-01-01", dynamic_id=dynamic_id)
    inserts.run(superuser_url)
    return CatalogRows(
        us_version_id=us_version,
        older_us_version_id=older_us_version,
        uk_version_id=uk_version,
        us_national_dataset_id=us_national,
        us_state_dataset_id=us_states,
        uk_dataset_id=uk_dataset,
        policy_id=policy_id,
        retired_policy_id=retired_policy_id,
        dynamic_id=dynamic_id,
    )
