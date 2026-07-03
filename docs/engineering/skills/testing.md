# Testing Skill

Use this skill whenever adding, moving, or reviewing tests.

## Canonical Layout

- Service unit tests live under each service's `tests/` directory, for example
  `projects/policyengine-simulation-executor/tests/`.
- Generated-client integration tests live under
  `projects/policyengine-apis-integ/tests/`.
- Put reusable test helpers in local fixture modules or support modules near
  the tests that use them.
- Avoid importing helpers across unrelated test lanes. Move shared helpers to a
  neutral support module when needed.

## Dependency Boundaries

- Unit tests should not require real network credentials, Modal, Hugging Face,
  GCP, or deployed services. Mock those seams.
- Integration tests may require services, generated clients, or deployed
  environments, but should be explicit about those requirements and skip or mark
  cleanly when unavailable.
- When changing public API schemas, regenerate clients and run the relevant
  generated-client integration tests.

## Common Commands

Run the narrowest meaningful checks during development, then broader checks
before opening or updating a PR when feasible:

```bash
make format
make check
make test
make test-complete
```

Simulation-service focused checks:

```bash
cd projects/policyengine-simulation-executor
uv sync --extra test
uv run pytest tests/ -v
```

Integration checks:

```bash
./scripts/generate-clients.sh
cd projects/policyengine-apis-integ
uv sync --extra test
uv run pytest tests/ -v -m "not requires_gcp and not beta_only"
```
