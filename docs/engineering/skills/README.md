# Engineering Skills

This directory is the canonical source for AI-facing engineering rules.

Tool-specific instruction files such as `AGENTS.md`, `CLAUDE.md`, and
`.github/copilot-instructions.md` should point here instead of duplicating
implementation-specific guidance. When a rule changes, update the skill here
first, then keep adapters thin.

Current skills:

- `ci-workflows.md`: one line of code per workflow `run:` block, tested
  workflow scripts, and shellcheck.
- `database-access.md`: SQLAlchemy-only database connections, schema changes
  only through Alembic migrations, and the guard that checks both.
- `github-prs.md`: same-repository draft PR workflow, issue linkage, PR head
  verification, and title conventions.
- `testing.md`: test placement, dependency boundaries, and expected validation
  commands.
- `microdata-weights.md`: weights, values, labels and counts in simulation
  microdata, and the guards that keep weights from being aggregated as values.
- `observability.md`: identifier ownership, HTTP and Modal transport,
  persistence, registered runtime stages, trace boundaries, and failure
  isolation.
- `rules-registry.md`: how rules are registered in `rules/registry.toml`,
  enforced, proven, and rendered into `docs/generated/rules.md`.
- `typing-only-imports.md`: which imports ruff moves under `TYPE_CHECKING`, how
  it decides a Pydantic model is runtime-evaluated, and how to list model bases
  so models keep the imports their annotations need.
