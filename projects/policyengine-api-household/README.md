# policyengine-api-household

Experimental resurrection of the household calculation service from PR #49
(commit `019b808`), rebuilt on the monorepo's current conventions
(uv + hatchling, `libs/policyengine-fastapi`, layout mirroring
`projects/policyengine-api-simulation`).

Goal: byte-level parity with the v1 household API
(`policyengine-household-api`) for `POST /{country_id}/calculate`, so a
golden corpus can be diffed against the real household API.

## Endpoints

- `POST /{country_id}/calculate` — `country_id` is `us` or `uk`. Body:
  `{"household": {...v1 entity structure...}}` where a `null` variable
  value means "compute this". Returns the v1 envelope:
  `{"status", "message", "result", "policyengine_bundle"}`;
  `policyengine_bundle.model_version` is the country model package
  version, for pinning parity diffs.
- `GET /ping/started`, `GET /ping/alive`, `POST /ping` — the repo's
  standard health routes from `policyengine-fastapi`.

## Engine

Country model packages are used directly
(`policyengine_us` / `policyengine_uk` via
`<package>.Simulation(situation=...)`), the same engine as the v1
household API and PR #49. Versions are pinned to match
`projects/policyengine-api-simulation` (`policyengine-us==1.729.0`,
`policyengine-uk==2.89.2`, `policyengine-core==3.28.0`).

## Run locally

From this directory (`projects/policyengine-api-household`):

```bash
# Install (country model packages are large; the first sync takes a while)
uv sync --extra test

# Run tests (first calculate test loads the US model, ~1-2 min)
uv run pytest

# Start the service on port 8080
uv run uvicorn policyengine_api_household.main:app --port 8080
```

Smoke test:

```bash
curl -s -X POST http://localhost:8080/us/calculate \
  -H "Content-Type: application/json" \
  -d '{
    "household": {
      "people": {"you": {"age": {"2026": 30},
                          "employment_income": {"2026": 30000}}},
      "tax_units": {"your tax unit": {"members": ["you"],
                                       "income_tax": {"2026": null}}},
      "households": {"your household": {"members": ["you"],
                                         "state_name": {"2026": "TX"}}}
    }
  }'
```

## Scope / caveats

- Local-run experiment only: no Modal, deployment, auth, or telemetry
  wiring yet.
- Only `us` and `uk` are supported (PR #49 also loaded ca/ng/il).
- Reform (`policy`) payloads are plumbed through `country.calculate`
  (recovered from PR #49) but not yet exposed on the endpoint; for the
  UK they would raise (see next point).
- policyengine-uk >= 2.x changed `Simulation.__init__` (no
  `tax_benefit_system` keyword), so the UK path constructs
  `Simulation(situation=...)` directly and cannot take a cloned,
  reform-modified system the way PR #49 did.
- The v1 service layers extra validation/warnings (variable validation,
  deprecated-input filtering, period validation, axes limits) that this
  experiment does not replicate; expect parity gaps on error paths and
  on the optional `warnings` response key.
