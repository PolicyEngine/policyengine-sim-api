"""Simulation schema ownership: one chain, no executor dependency, none in Modal."""

from __future__ import annotations

import pytest
from migration_support import REPOSITORY_ROOT
from ownership_checks import (
    declares_persistence_dependency,
    migration_chain_violations,
    modal_credential_violations,
    modal_source_violations,
    persistence_dependency_violations,
)

FIXTURES = REPOSITORY_ROOT / "rules" / "fixtures"
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
WORKFLOW_SCRIPTS = REPOSITORY_ROOT / ".github" / "scripts"
MODAL_PROJECTS = (
    REPOSITORY_ROOT / "projects" / "policyengine-simulation-executor",
    REPOSITORY_ROOT / "projects" / "policyengine-simulation-gateway",
)


def _texts(directory, pattern):
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob(pattern))
    }


def test_repository_has_one_simulation_migration_chain() -> None:
    assert migration_chain_violations(REPOSITORY_ROOT) == []


def test_rule_proof_one_migration_chain() -> None:
    """Proof for rule sim.persistence.one-migration-chain (rules/registry.toml)."""
    fixtures = FIXTURES / "sim.persistence.one-migration-chain"

    assert migration_chain_violations(fixtures / "bad_repository") != []
    assert migration_chain_violations(fixtures / "good_repository") == []


def test_modal_projects_never_depend_on_the_persistence_library() -> None:
    for project_root in MODAL_PROJECTS:
        assert persistence_dependency_violations(project_root) == []


def test_rule_proof_executor_independent() -> None:
    """Proof for rule sim.persistence.executor-independent (rules/registry.toml)."""
    fixtures = FIXTURES / "sim.persistence.executor-independent"

    assert declares_persistence_dependency(
        (fixtures / "bad_pyproject.toml").read_text(encoding="utf-8")
    )
    assert not declares_persistence_dependency(
        (fixtures / "good_pyproject.toml").read_text(encoding="utf-8")
    )


def test_modal_receives_no_database_url_but_the_comparison_paths() -> None:
    assert (
        modal_credential_violations(
            _texts(WORKFLOWS, "*.yml"),
            _texts(WORKFLOW_SCRIPTS, "*.sh"),
        )
        == []
    )
    for project_root in MODAL_PROJECTS:
        sources = {
            str(path): path.read_text(encoding="utf-8")
            for path in sorted((project_root / "src").rglob("*.py"))
        }
        assert modal_source_violations(sources) == []


def test_rule_proof_no_modal_database_credentials() -> None:
    """Proof for rule sim.persistence.no-modal-database-credentials."""
    fixtures = FIXTURES / "sim.persistence.no-modal-database-credentials"
    no_scripts: dict[str, str] = {}

    assert modal_credential_violations(
        {"bad.yml": (fixtures / "bad_workflow.yml").read_text(encoding="utf-8")},
        no_scripts,
    )
    assert not modal_credential_violations(
        {"good.yml": (fixtures / "good_workflow.yml").read_text(encoding="utf-8")},
        no_scripts,
    )


def _modal_secret_workflow(*, step_env: str = "", job_env: str = "") -> str:
    return (
        "jobs:\n"
        "  deploy:\n"
        f"{job_env}"
        "    steps:\n"
        "      - name: Synchronize Modal secrets\n"
        f"{step_env}"
        "        run: uv run modal secret create runtime --from-dotenv .env\n"
    )


@pytest.mark.parametrize(
    ("step_env", "job_env"),
    [
        ("        env:\n          REPORTS_DATABASE_URL: x\n", ""),
        ("        env:\n          DATABASE_URL: x\n", ""),
        ("        env:\n          SIMULATION_DATABASE_URL: x\n", ""),
        ("", "    env:\n      SIMULATION_MIGRATION_DATABASE_URL: x\n"),
        ("        env:\n          DSN: postgresql://user:secret@db/postgres\n", ""),
    ],
    ids=[
        "any-database-url",
        "bare-database-url",
        "simulation-url",
        "job-level-env",
        "url-literal",
    ],
)
def test_modal_steps_may_carry_only_the_comparison_paths_database_url(
    step_env, job_env
) -> None:
    workflow = _modal_secret_workflow(step_env=step_env, job_env=job_env)

    assert modal_credential_violations({"deploy.yml": workflow}, {}) != []


def test_the_comparison_paths_database_url_may_reach_modal_until_it_is_removed() -> (
    None
):
    workflow = _modal_secret_workflow(
        step_env=(
            "        env:\n"
            "          STAGE12_DATABASE_URL_SECRET_NAME: stage12-database-url\n"
        )
    )
    script = "jq -n '{STAGE12_DATABASE_URL: $url}'\nuv run modal secret create x\n"

    assert modal_credential_violations({"deploy.yml": workflow}, {}) == []
    assert modal_credential_violations({}, {"stage12-sync.sh": script}) == []


def test_a_modal_script_may_not_carry_another_database_url() -> None:
    script = (
        "uv run modal secret create x REPORTS_DATABASE_URL=${REPORTS_DATABASE_URL}\n"
    )

    assert modal_credential_violations({}, {"sync.sh": script}) == [
        "sync.sh gives Modal a database credential other than the comparison "
        "path's: REPORTS_DATABASE_URL"
    ]


def test_modal_sources_may_name_only_the_comparison_paths_database_url() -> None:
    assert (
        modal_source_violations({"worker.py": "os.environ['STAGE12_DATABASE_URL']"})
        == []
    )
    assert modal_source_violations(
        {"worker.py": "os.environ['SIMULATION_DATABASE_URL']"}
    ) == ["worker.py names SIMULATION_DATABASE_URL"]
    assert modal_source_violations({"worker.py": "os.getenv('DATABASE_URL')"}) == [
        "worker.py names DATABASE_URL"
    ]


def test_a_temporary_modal_app_may_not_receive_another_database_url() -> None:
    workflow = (
        "jobs:\n"
        "  precompute:\n"
        "    steps:\n"
        "      - name: Precompute\n"
        "        env:\n"
        "          SIMULATION_DATABASE_URL: x\n"
        "        run: uv run --frozen modal run src/modal/precompute_app.py\n"
    )

    assert modal_credential_violations({"precompute.yml": workflow}, {}) != []
