"""The ORM mapped classes of API v1's tables.

Each table has one typed mapped class. The stores' statements still use the
class's Core table, under the name the table had before it was mapped, until
they are converted.
"""

from __future__ import annotations

import inspect as python_inspect
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin
from uuid import UUID, uuid4

import pytest
from conftest import SERVICE_TEST_ROLE, simulation_engine
from policyengine_simulation_persistence import (
    BudgetSummary,
    BudgetWindowRun,
    CongressionalDistrictImpact,
    ConstituencyImpact,
    Dataset,
    DecileImpact,
    Dynamic,
    Household,
    HouseholdJob,
    HouseholdJobStatus,
    Inequality,
    InputBase,
    IntraDecileImpact,
    LaborSupplyDecileResponse,
    LaborSupplyResponse,
    LocalAuthorityImpact,
    Parameter,
    ParameterValue,
    Policy,
    Poverty,
    ProgramStatistics,
    ReferencedBase,
    Region,
    Report,
    ReportRun,
    ResultBase,
    ServiceBase,
    Simulation,
    SimulationRun,
    SimulationStatus,
    SimulationType,
    TaxBenefitModel,
    TaxBenefitModelVersion,
    WithheldAreaCode,
    create_session_factory,
    input_metadata,
    input_tables,
    referenced_metadata,
    referenced_tables,
    result_metadata,
    result_tables,
    service_metadata,
    tables,
)
from public_api_keys import KEYS_TO_PUBLIC_API_TABLES
from schema_comparison import describe_tables
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Mapped, configure_mappers

SERVICE_CLASSES = {
    "simulations": Simulation,
    "reports": Report,
    "simulation_runs": SimulationRun,
    "budget_window_runs": BudgetWindowRun,
    "report_runs": ReportRun,
    "household_jobs": HouseholdJob,
}
REFERENCED_CLASSES = {
    "datasets": referenced_tables.ReferencedDataset,
    "dynamics": referenced_tables.ReferencedDynamic,
    "households": referenced_tables.ReferencedHousehold,
    "policies": referenced_tables.ReferencedPolicy,
    "regions": referenced_tables.ReferencedRegion,
    "tax_benefit_model_versions": referenced_tables.ReferencedTaxBenefitModelVersion,
    "tax_benefit_models": referenced_tables.ReferencedTaxBenefitModel,
    "users": referenced_tables.ReferencedUser,
}
INPUT_CLASSES = {
    "tax_benefit_models": TaxBenefitModel,
    "tax_benefit_model_versions": TaxBenefitModelVersion,
    "regions": Region,
    "datasets": Dataset,
    "households": Household,
    "policies": Policy,
    "parameters": Parameter,
    "parameter_values": ParameterValue,
    "dynamics": Dynamic,
}
RESULT_CLASSES = {
    "labor_supply_responses": LaborSupplyResponse,
    "labor_supply_decile_responses": LaborSupplyDecileResponse,
    "budget_summary": BudgetSummary,
    "decile_impacts": DecileImpact,
    "intra_decile_impacts": IntraDecileImpact,
    "program_statistics": ProgramStatistics,
    "congressional_district_impacts": CongressionalDistrictImpact,
    "constituency_impacts": ConstituencyImpact,
    "local_authority_impacts": LocalAuthorityImpact,
    "withheld_area_codes": WithheldAreaCode,
    "poverty": Poverty,
    "inequality": Inequality,
}
GROUPS = [
    pytest.param(ServiceBase, service_metadata, SERVICE_CLASSES, id="service"),
    pytest.param(ReferencedBase, referenced_metadata, REFERENCED_CLASSES, id="ref"),
    pytest.param(InputBase, input_metadata, INPUT_CLASSES, id="input"),
    pytest.param(ResultBase, result_metadata, RESULT_CLASSES, id="result"),
]
# The modules whose Core table names the stores import.
TABLE_NAME_MODULES = [
    pytest.param(tables, SERVICE_CLASSES, id="service"),
    pytest.param(input_tables, INPUT_CLASSES, id="input"),
    pytest.param(result_tables, RESULT_CLASSES, id="result"),
]
ALL_CLASSES = [
    *SERVICE_CLASSES.values(),
    *REFERENCED_CLASSES.values(),
    *INPUT_CLASSES.values(),
    *RESULT_CLASSES.values(),
]


def _allows_none(annotation: object) -> bool:
    return get_origin(annotation) in (Union, UnionType) and NoneType in get_args(
        annotation
    )


@pytest.mark.parametrize(("base", "metadata", "classes"), GROUPS)
def test_each_base_maps_exactly_the_tables_of_its_metadata(
    base, metadata, classes
) -> None:
    mapped_classes = {mapper.class_ for mapper in base.registry.mappers}

    assert base.metadata is metadata
    assert metadata.schema == "public"
    assert mapped_classes == set(classes.values())
    assert {table.name for table in metadata.tables.values()} == set(classes)
    for table_name, mapped_class in classes.items():
        assert mapped_class.__tablename__ == table_name
        assert mapped_class.__table__.metadata is metadata


def test_the_groups_keep_the_tables_they_share_a_name_with_apart() -> None:
    """A read model and a key's target of one table are separate classes."""
    assert INPUT_CLASSES["households"].__table__ is not (
        REFERENCED_CLASSES["households"].__table__
    )
    assert [column.name for column in REFERENCED_CLASSES["households"].__table__.c] == [
        "id"
    ]


@pytest.mark.parametrize(("module", "classes"), TABLE_NAME_MODULES)
def test_each_table_name_the_stores_import_is_its_classs_table(module, classes) -> None:
    for table_name, mapped_class in classes.items():
        assert getattr(module, table_name) is mapped_class.__table__, table_name


@pytest.mark.parametrize("mapped_class", ALL_CLASSES, ids=lambda item: item.__name__)
def test_each_column_is_the_attribute_of_its_own_name(mapped_class) -> None:
    mapper = inspect(mapped_class)

    assert [attribute.key for attribute in mapper.column_attrs] == [
        column.name for column in mapped_class.__table__.columns
    ]


@pytest.mark.parametrize("mapped_class", ALL_CLASSES, ids=lambda item: item.__name__)
def test_each_annotation_states_whether_its_column_allows_null(mapped_class) -> None:
    annotations = python_inspect.get_annotations(mapped_class, eval_str=True)

    for column in mapped_class.__table__.columns:
        annotation = annotations[column.name]
        assert get_origin(annotation) is Mapped, column.name
        (value_type,) = get_args(annotation)
        # A JSON value may be anything, so its annotation can't say.
        if value_type is not Any:
            assert _allows_none(value_type) is column.nullable, column.name


@pytest.mark.parametrize("mapped_class", ALL_CLASSES, ids=lambda item: item.__name__)
def test_each_class_is_identified_by_the_id_column(mapped_class) -> None:
    primary_key = inspect(mapped_class).primary_key

    assert [column.name for column in primary_key] == ["id"]


def test_the_keys_to_api_v1s_tables_name_the_referenced_classes() -> None:
    referenced = {model.__table__ for model in REFERENCED_CLASSES.values()}
    named_tables = {
        constraint.referred_table
        for model in SERVICE_CLASSES.values()
        for constraint in model.__table__.foreign_key_constraints
        if constraint.referred_table in referenced
    }

    assert named_tables == referenced
    assert len(KEYS_TO_PUBLIC_API_TABLES) == 20


def test_every_mapper_configures() -> None:
    configure_mappers()


def _postgres_sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_statements_on_a_mapped_class_name_its_table_in_the_public_schema() -> None:
    read = select(HouseholdJob).where(HouseholdJob.status == HouseholdJobStatus.PENDING)
    claim = (
        update(HouseholdJob)
        .where(HouseholdJob.modal_call_id.is_(None))
        .values(modal_call_id="call")
        .returning(HouseholdJob.id)
    )
    find_or_create = (
        postgresql.insert(Simulation)
        .values(id=uuid4())
        .on_conflict_do_nothing(
            index_elements=[Simulation.key_version, Simulation.definition_key]
        )
    )

    assert "FROM public.household_jobs" in _postgres_sql(read)
    assert "UPDATE public.household_jobs" in _postgres_sql(claim)
    assert "ON CONFLICT (key_version, definition_key)" in _postgres_sql(find_or_create)


@pytest.mark.postgres
def test_a_session_writes_and_reads_a_household_job_through_its_class(
    disposable_database,
) -> None:
    """Python and server defaults, the enum and JSON ``NULL`` all round-trip."""
    engine = simulation_engine(disposable_database.superuser_url)
    session_factory = create_session_factory(engine)
    try:
        # Tests may build the models' tables; runtime code never does.
        referenced_metadata.create_all(engine)
        service_metadata.create_all(engine)
        household = referenced_tables.ReferencedHousehold(id=uuid4())
        version = referenced_tables.ReferencedTaxBenefitModelVersion(id=uuid4())
        with session_factory.begin() as session:
            session.add_all([household, version])
            # No relationship orders these inserts, so the keys' targets go first.
            session.flush()
            job = HouseholdJob(
                country="us",
                request_data={"people": {}},
                status=HouseholdJobStatus.PENDING,
                household_id=household.id,
                year=2026,
                tax_benefit_model_version_id=version.id,
                country_package_version="1.0.0",
                policyengine_version="2.0.0",
                result=None,
            )
            session.add(job)
        with session_factory() as session:
            stored_job = session.get(HouseholdJob, job.id)
            result_is_sql_null = session.scalar(
                select(HouseholdJob.result.is_(None)).where(HouseholdJob.id == job.id)
            )
    finally:
        engine.dispose()

    assert isinstance(job.id, UUID)
    # Read back at the insert, and still loaded after the session closed.
    assert job.created_at is not None
    assert stored_job is not None
    assert stored_job.status is HouseholdJobStatus.PENDING
    assert stored_job.request_data == {"people": {}}
    assert stored_job.result is None
    assert result_is_sql_null is True


def _insert_simulation_run(session_factory) -> UUID:
    """Insert a simulation run and the rows its keys name; return its ``id``."""
    household = referenced_tables.ReferencedHousehold(id=uuid4())
    version = referenced_tables.ReferencedTaxBenefitModelVersion(id=uuid4())
    simulation = Simulation(
        simulation_type=SimulationType.HOUSEHOLD,
        household_id=household.id,
        tax_benefit_model_version_id=version.id,
    )
    with session_factory.begin() as session:
        session.add_all([household, version])
        session.flush()
        session.add(simulation)
        session.flush()
        run = SimulationRun(
            simulation_id=simulation.id,
            year=2026,
            tax_benefit_model_version_id=version.id,
            country_package_version="1.0.0",
            policyengine_version="2.0.0",
            status=SimulationStatus.PENDING,
        )
        session.add(run)
    return run.id


@pytest.mark.postgres
def test_a_written_result_stays_readable_after_its_session_closes(
    disposable_database,
) -> None:
    """A role with API v1's grants on a result table reads its defaults back."""
    owner_engine = simulation_engine(disposable_database.superuser_url)
    writer_engine = simulation_engine(disposable_database.service_url)
    try:
        # Tests may build the models' tables; runtime code never does.
        referenced_metadata.create_all(owner_engine)
        service_metadata.create_all(owner_engine)
        result_metadata.create_all(owner_engine)
        run_id = _insert_simulation_run(create_session_factory(owner_engine))
        with owner_engine.begin() as connection:
            connection.execute(
                text(f"GRANT SELECT, INSERT ON public.poverty TO {SERVICE_TEST_ROLE}")
            )
        result = Poverty(simulation_run_id=run_id, poverty_type="spm", entity="person")

        with create_session_factory(writer_engine).begin() as session:
            session.add(result)
    finally:
        owner_engine.dispose()
        writer_engine.dispose()

    assert isinstance(result.id, UUID)
    assert result.created_at is not None


@pytest.mark.public_api_schema
def test_the_input_classes_are_identified_as_api_v1s_tables_are(
    public_api_database_url,
) -> None:
    """A mapped class needs a primary key, which must be the table's real one.

    The comparison of the read-only models looks only at their columns, so it
    wouldn't notice an identity that isn't unique in API v1's table,
    and a session would merge the rows that shared it.
    """
    engine = create_engine(
        make_url(public_api_database_url).set(drivername="postgresql+psycopg")
    )
    try:
        with engine.connect() as connection:
            real = describe_tables(connection, list(INPUT_CLASSES))
    finally:
        engine.dispose()

    for table_name in INPUT_CLASSES:
        assert "PRIMARY KEY (id)" in real.tables[table_name].constraints.values(), (
            table_name
        )
