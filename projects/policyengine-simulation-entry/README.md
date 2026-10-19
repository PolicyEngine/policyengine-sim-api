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
They qualify tagged Cloud Run candidates before production traffic changes and
verify the stable production endpoint immediately after promotion.

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
| `GET /simulation-jobs/{job_reference}` | `simulation:read` |

`POST /internal/simulation-jobs/{job_id}/collect` and
`POST /internal/simulation-jobs/sweep` do not use Auth0: they accept only the
Google OIDC token Cloud Tasks and Cloud Scheduler send (see `DEPLOYMENT.md`).

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
