# policyengine-simulation-entry

The permanent Cloud Run entrypoint for PolicyEngine simulation submission and
polling.

During migration Stage 5 this service is a contract-compatible control-plane
proxy backed by the existing Modal-hosted simulation gateway. It authenticates
callers, uses its own machine identity for the Modal hop, and preserves existing
job identifiers. It does not yet own job persistence, version routing, or
compute dispatch.

## Local development

    uv sync --extra test
    uv run pytest tests/ -v

The production app is policyengine_simulation_entry.app:app. Required runtime
configuration is documented in policyengine_simulation_entry.config.Settings.
The service does not require its own public URL as runtime configuration.
Cloud Run's generated revision name is returned in a response header so
deployment checks can prove which revision a tagged or stable URL serves.

Live caller/backend authentication checks are isolated in `authenticated_tests`.
They qualify tagged Cloud Run candidates before an environment's stable traffic
changes and verify its stable endpoint immediately after promotion, in both
staging and production.

## Caller authentication and permissions

Protected routes accept Auth0 RS256 access tokens issued by
`SIMULATION_ENTRYPOINT_AUTH_ISSUER` for `SIMULATION_ENTRYPOINT_AUTH_AUDIENCE`.
A missing or invalid token returns 403. Each protected route also requires one
Auth0 permission:

| Route | Permission |
|---|---|
| `POST /simulate/economy/comparison` | `simulation:submit` |
| `POST /simulate/economy/budget-window` | `simulation:submit` |
| `GET /jobs/{job_id}` | `simulation:read` |
| `GET /budget-window-jobs/{batch_job_id}` | `simulation:read` |
| `POST /internal/stage12/reports` | `stage12:operate` |
| `GET /internal/stage12/reports/{evaluation_id}` | `stage12:operate` |
| `POST /household-calculations` | `simulation:submit` |
| `GET /household-calculations/{household_job_id}` | `simulation:read` |

`GET /health`, `GET /ready`, `GET /versions`, `GET /versions/{kind}` and
`POST /ping` stay public. The entrypoint answers `GET /health` and `POST /ping`
itself, without calling the old gateway: `/ping` returns the request's `value`
plus one as `incremented`, the same answer the old gateway gives. Their
responses carry no `X-PolicyEngine-Simulation-Backend` header and record no
old-gateway telemetry events.

A caller holds a permission when it appears in the token's `permissions` claim
(Auth0 RBAC). For a client-credentials (machine) token, an entry in its
space-delimited `scope` claim also counts. A user token's `scope` never does,
because without RBAC Auth0 puts any requested API scope there.
`SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS` decides what happens when the
permission is missing:

- `0`, or unset: log only. The request proceeds, and the service emits a
  `simulation_entry_permission_missing` event with the `permission`,
  `enforced=false`, and the token's `azp` as `client_id`. Tokens and other
  claims are never recorded.
- `1`: the request is rejected with 403, and the same event is emitted with
  `enforced=true`.

Any other value stops the service from starting. When caller authentication is
disabled (`SIMULATION_ENTRYPOINT_AUTH_REQUIRED=0`, which production refuses),
there is no caller identity and every protected route is allowed. The Auth0
setup required before enforcement is described in `DEPLOYMENT.md`.

## Household calculations

`POST /household-calculations` names a stored household, and optionally a
stored policy, of the Public API: `country`, `household_id`, `policy_id`,
`add_missing_variables`, `axes` (within the household contract's limits)
and `spm` (SPM settings as an object; omit it to use the household's saved
selection, since null is refused). The service reads the rows itself,
calculates the household's `default_year` on the v2 version manifest's
default worker, creates a `household_jobs` row and starts the household
function's call with `spawn`, answering 202 with the job's `job_id` and
`status`. Each submission creates a new job.
`GET /household-calculations/{household_job_id}` returns the job's status,
the household function's `result` document once it has one, and its
`error_message`.

A submission is refused before anything is written: with 400 and a `code`
when the household, policy or catalog version for the default worker's .py
version is missing, or the default worker has no household function for the
country; with 422, one reason per field, when the stored household has no
default year, the policy sets parameters that catalog version lacks, or the
household contract refuses the stored data. A job naming a row deleted
since the reads is refused by the database's foreign keys, also with 400.
The routes answer 503 with `Retry-After` when the manifest or the database
can't be read. When Modal refuses the call, or doesn't confirm it within ten
seconds, the job is failed at once and the 503 (`modal_call_refused`) names
it; a job the household function claimed meanwhile is answered with 202 as
it stands, and a job whose state couldn't then be recorded or read gets a
503 `job_state_unknown` naming it, to be read before submitting again. A
request the service builds and the household contract refuses is an
internal error, 500. `DEPLOYMENT.md` describes the deployment settings and the
staging check.

