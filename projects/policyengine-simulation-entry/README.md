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
staging and production. The staging household test there runs only in its own
job after a staging promotion (`DEPLOYMENT.md`, "Staging household test").

## Household calculations

`POST /v2/household-calculations` records a household calculation, one
stored household under one stored policy or current law, and starts its
Modal call; `GET /v2/household-calculations/{calculation_id}` returns its
status and, once it succeeded, the calculated household with its warnings,
the SPM selection it applied (`spm_config`), its SPM receipt
(`spm_provenance`) and its bundle, or, once it failed, its error. The Public
API sends the IDs of its household and policy and the PolicyEngine.py
version; this service reads, from the Public API's tables in the same
database, the country's catalog version for that PolicyEngine.py version,
the household (its document, saved SPM choice and `default_year`, the year
calculated) and the policy's parameter values, before writing anything.
A household, policy or catalog version the Public API does not have for the
country, a policy changing a parameter the catalog version lacks, and a
household without a default year or that the household functions cannot
take are refused with 400 and the reason. The runtime role needs `SELECT`
on those tables (`libs/policyengine-simulation-persistence`, "Household
calculations").

Each calculation is a simulation run of a household simulation (household,
policy and options such as axes), so the same calculation submitted again
reuses its run while it is unfinished or for 28 days after it succeeded,
unless `force` is set; after that, it runs again under a new calculation ID.
The household function comes from the v2 version manifest for the
calculation's PolicyEngine.py version, in the v2 application's Modal
environment (`SIMULATION_CALL_REPORT_MODAL_ENVIRONMENT`). The function
reports when its call ends, and the outcome is recorded then
(`DEPLOYMENT.md`, "Recording run outcomes"). Without the simulation database
or that environment, the routes answer 503.

A household report compares the household under a baseline and an optional
reform. `POST /v2/household-reports` creates it and starts its first run;
`POST /v2/household-reports/{report_id}/runs` starts another run of it on a
PolicyEngine.py version, reading the report's own household and policies
again; `GET /v2/household-reports/{report_id}`
returns it with its newest run and that run's two calculations. Each report
run joins a run of each simulation, reused like any calculation's, and one
Modal call computes the runs that still need computing, so a new report's
two households are calculated together. A report run has failed when either
calculation failed and succeeded when both did.

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
| `POST /v2/household-calculations` | `simulation:submit` |
| `GET /v2/household-calculations/{calculation_id}` | `simulation:read` |
| `POST /v2/household-reports` | `simulation:submit` |
| `POST /v2/household-reports/{report_id}/runs` | `simulation:submit` |
| `GET /v2/household-reports/{report_id}` | `simulation:read` |
| `POST /internal/stage12/reports` | `stage12:operate` |
| `GET /internal/stage12/reports/{evaluation_id}` | `stage12:operate` |

`GET /health`, `GET /ready`, `GET /versions`, `GET /versions/{kind}` and
`POST /ping` stay public.

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
