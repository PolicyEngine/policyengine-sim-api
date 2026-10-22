"""The check behind sim.persistence.migrate-before-entrypoint's workflow part.

It takes a deploy workflow's jobs, so the rule test runs it on the reusable
deploy workflow and the proof test on the rule's fixtures.
"""

from __future__ import annotations

import json

MIGRATION_JOB = "migrate_simulation_database"
MIGRATION_STEP = "Apply simulation schema migrations"
MIGRATION_SECRET = "SIMULATION_MIGRATION_DATABASE_URL"
ENTRYPOINT_JOB = "deploy_entrypoint"
MIGRATION_MODE = "${{ github.ref == 'refs/heads/main' && 'upgrade' || 'verify' }}"


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _secret_carriers(jobs: dict) -> list[tuple[str, str]]:
    carriers = []
    for job_name, job in jobs.items():
        if MIGRATION_SECRET in json.dumps(job.get("env", {})):
            carriers.append((job_name, "job env"))
        carriers.extend(
            (job_name, str(step.get("name")))
            for step in job.get("steps", [])
            if MIGRATION_SECRET in json.dumps(step)
        )
    return carriers


def migration_wiring_violations(jobs: dict) -> list[str]:
    """Return how a deploy workflow's jobs depart from the migration wiring."""
    migration = jobs.get(MIGRATION_JOB)
    if migration is None:
        return [f"no {MIGRATION_JOB} job"]
    violations = []
    entrypoint = jobs.get(ENTRYPOINT_JOB, {})
    if MIGRATION_JOB not in _needs(entrypoint) or (
        f"needs.{MIGRATION_JOB}.result == 'success'" not in entrypoint.get("if", "")
    ):
        violations.append(
            f"{ENTRYPOINT_JOB} does not wait for a successful {MIGRATION_JOB}"
        )
    carriers = _secret_carriers(jobs)
    if carriers != [(MIGRATION_JOB, MIGRATION_STEP)]:
        violations.append(f"{MIGRATION_SECRET} reaches {carriers}")
    steps = {step.get("name"): step for step in migration.get("steps", [])}
    mode = steps.get(MIGRATION_STEP, {}).get("env", {}).get("SIMULATION_MIGRATION_MODE")
    if mode != MIGRATION_MODE:
        violations.append(f"{MIGRATION_STEP} applies revisions from any branch")
    return violations
