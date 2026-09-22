# Stage 12 Modal worker foundation

Companion change identifier: `stage-12-modal-worker-foundation`.

This change implements the simulation-service portion of the Stage 12 plan
defined in `PolicyEngine/policyengine-api`. It does not change the public
simulation submission or polling contracts.

## Service ownership

- The Cloud Run Simulation Entrypoint keeps its current production adapter,
  which forwards submissions and polls to the existing Modal HTTP routing
  service. Responses from that path remain authoritative.
- A separate comparison-run adapter reads only a separately stored v2 version
  manifest and invokes separately named v2 Modal functions directly.
- Invocation context carries both the logical deployment environment used for
  records and artifact paths (`staging` or `production`) and the Modal
  environment used for function lookup (`staging` or `main`). Production child
  simulations therefore resolve in Modal `main` without relabeling durable
  records as `main`.
- One v2 report-coordinator invocation starts baseline and reform as distinct
  single-simulation calls before awaiting either result.
- Each country-specific single-simulation function and the report-coordinator
  function has a Modal `max_containers` value of 10. This is a per-function
  limit; the functions do not share one application-wide container quota.
- The v2 functions write canonical private artifacts and use a shared
  SQLAlchemy Core package to persist temporary comparison state directly.
  They do not create production simulations, reports, report runs, or user
  associations.

The existing Modal routing service, its versioned executor applications, and
its v1 routing manifest remain deployed and unchanged throughout Stage 12.

## Cross-repository ownership

`PolicyEngine/policyengine-api` is the only schema authority for the temporary
comparison tables. Their physical `stage12_evaluation_*` names and
`evaluation_id` column remain unchanged until Stage 14. This repository owns
the strict runtime request, artifact, and data-transfer models used by the
Stage 12 workers. Changes that affect both repositories require coordinated review;
no generated cross-repository contract file is checked in or consumed at
runtime. This repository must not define SQLModel tables or an Alembic
migration for the comparison records. It contains synchronized SQLAlchemy Core
table mappings and parameterized DML only; raw SQL, SQLAlchemy textual SQL,
DBAPI cursor execution, runtime DDL, metadata creation, and a competing
migration chain are prohibited. Deployment validation compares those mappings
with the live API-owned schema.

## Release and deployment constraints

- The selected PolicyEngine.py release's packaged `.py` bundle manifest is the
  sole source of country-package names and exact versions, data-package names
  and versions, default dataset identities and URIs, artifact revisions, and
  supported alternatives.
- Environment variables, workflow inputs, command-line values, and installed
  distribution inspection may only assert expected bundle-derived values.
- V2 application names, manifest storage, publication configuration, reader
  configuration, and in-process cached state must be distinct from v1.
- Publishing or rejecting either manifest must leave the other manifest and
  its selected applications unchanged.
- V2 worker object access uses the already-provisioned separate
  `stage12-evaluation-gcp-credentials` Modal secret. The worker application
  does not receive the existing general GCP credential secret.
- `STAGE12_DATABASE_URL` is delivered from the environment-specific Secret
  Manager resource named by `STAGE12_DATABASE_URL_SECRET_NAME`. It authenticates
  as the existing shared `policyengine_v2_runtime` account; Stage 12 does not
  create or require a separate PostgreSQL role. The stored URL uses the
  canonical `postgresql://` scheme, and the persistence package selects the
  psycopg 3 driver internally.
- `STAGE12_ENABLED` is the only Stage 12 execution setting. Missing or `0`
  disables automatic runs and rejects new direct Stage 12 submissions. `1`
  enables every supported newly accepted annual society-wide report and the
  authenticated direct submission route. Any other value prevents service
  startup. Changing the setting requires a Cloud Run deployment. There is no
  percentage, request-bucket, shared runtime-control document, or
  partial-dataset mode.
- The separately named Modal application and v2 manifest remain deployed when
  `STAGE12_ENABLED=0`. The authenticated status route remains available for
  inspecting existing runs whenever the Stage 12 resources are configured.

Any genuinely one-time provisioning sequence must be implemented in a bounded
disposable script before execution and removed after the resulting state is
verified. Operations required for repeatable deployment, recreation, rollback,
or disaster recovery remain in maintained automation.

### Provisioning classification

Stage 12 currently requires no genuinely one-time provisioning operation. The
following operations are repeatable and therefore belong in maintained
automation or infrastructure configuration:

| Operation | Maintained owner |
| --- | --- |
| Build and deploy each versioned v2 Modal application | Simulation deployment workflow |
| Create or update the separate v2 manifest storage object | V2 manifest publisher |
| Configure the v2 manifest reader and `STAGE12_ENABLED` | Cloud Run deployment workflow |
| Create the private artifact namespace and its retention policy | Infrastructure configuration |
| Ensure the existing API v2 runtime identity has comparison-row database access | API infrastructure configuration |
| Deliver the existing shared runtime database secret to Cloud Run and Modal | Simulation deployment workflow |
| Grant private-object access only to the Modal runtime | Infrastructure configuration |
| Attach named runtime secrets without exposing their values | Simulation deployment workflow |
| Disable or restore automatic Stage 12 invocation | Cloud Run deployment workflow |
| Recreate any Stage 12 service or storage resource after loss | The same deployment and infrastructure automation |

When a deployment requests the Stage 12 resources, the ordinary integration
and promotion jobs wait for storage configuration, v2 worker validation, and
separate v2 manifest publication to succeed. A failed Stage 12 deployment
therefore prevents promotion of a Cloud Run revision configured to use it.

Every merge to `main` requests the Stage 12 resources in both the `beta` and
`prod` jobs. Each job validates its pre-provisioned environment-specific
resources, deploys and validates the separate v2 Modal application, publishes
the separate v2 manifest, and deploys a Stage 12-configured Cloud Run revision.
The `prod` job runs only after `beta` succeeds. The `beta` and `prod` GitHub
environments each provide `STAGE12_ENABLED`; both values are initially `0`, so
deploying Stage 12 does not copy production-path submissions until an operator
changes the applicable value to `1` and deploys Cloud Run. Production
prerequisites must therefore be provisioned and verified before merging;
missing production prerequisites are a deployment failure, not a reason to
omit Stage 12 from the `prod` job.

This automatic production deployment creates or updates a dormant Stage 12
runtime: the separate Modal application, v2 manifest, runtime configuration,
and temporary authenticated direct endpoint are present for later updates and
qualification. With `STAGE12_ENABLED=0`, it does not send ordinary production
calculations to the Stage 12 report coordinator or single-simulation workers,
and it rejects new direct Stage 12 submissions.
The promoted Cloud Run revision continues forwarding those calculations to the
existing Modal HTTP routing service and existing executors.

Existing environment, domain, and production-service resources are inputs to
Stage 12 rather than resources this change provisions. If implementation later
reveals an operation that is genuinely required only once, work must pause
until its bounded disposable script, target checks, secret references, and
verification output have been reviewed. The script is removed only after the
created state is independently verified.

## Initial flow

The initial comparison flow is `POST /simulate/economy/comparison` for reviewed
annual society-wide comparison inputs. The production request is forwarded
unchanged. Only a newly accepted supported production job may create one
corresponding comparison run; polls, cached results, and repeated submissions
that resolve the same production job do not create another comparison.

The Simulation Entrypoint, Modal report coordinator, and single-simulation
functions use each environment's existing `policyengine_v2_runtime` PostgreSQL
credential. They share one SQLAlchemy Core persistence package for the two
temporary tables. `policyengine-api` remains the sole owner of the canonical
SQLModel definitions and Alembic chain; the simulation repository owns no DDL
and introduces no persistence HTTP routes.

Before deploying, the simulation workflow obtains the shared runtime URL from
Secret Manager, verifies the expected `policyengine_v2_runtime` identity and
permissions, inspects the complete temporary schema against the declared
SQLAlchemy mappings, and runs a rolled-back insert/read/update/delete canary. It
also verifies required Secret Manager access and performs a create/read/delete
canary in the private artifact bucket. The storage canary is deleted in the
normal path and by exit cleanup after a failure.

The current Public API omits `data` for the certified default dataset. The v2
adapter therefore resolves an absent `data` field to the exact default dataset
in the selected `.py` bundle. An explicit `data` value is eligible only when it
identifies an artifact declared by that same bundle. The request model converts
the Public API's `_telemetry` field to `telemetry`; the adapter accepts that
correlation metadata but does not include it in either simulation input.

## Temporary direct runner endpoint

> **Temporary Stage 12 interface:** The authenticated routes in this section
> exist only for controlled direct invocation and inspection while the v2
> computation path is non-serving. Remove them during Stage 14 when
> authoritative v2 report submission and polling use the production
> `simulations`, `reports`, and `report_runs` records. They are intentionally
> excluded from the generated OpenAPI document and clients.

The existing Cloud Run Simulation Entrypoint exposes these operator-only routes:

- `POST /internal/stage12/reports` creates one direct Stage 12 report execution;
- `GET /internal/stage12/reports/{evaluation_id}` reads its temporary parent and
  child execution metadata.

Both routes enforce the Simulation Entrypoint's existing bearer authentication.
`POST /internal/stage12/reports` accepts new work only when
`STAGE12_ENABLED=1`; with a missing or zero value it returns HTTP 503 without
creating a parent record or invoking Modal. The status route remains available
whenever the Stage 12 resources are configured so authorized operators can
inspect runs that were already submitted.

The direct submission does not call the existing production computation and
does not keep a Cloud Run request open while calculations run:

```text
authenticated caller
        |
        | POST /internal/stage12/reports
        v
Cloud Run Simulation Entrypoint
        | validate request
        | resolve separate v2 manifest
        | prepare parent metadata in memory
        | request a Modal coordinator invocation
        | wait at most five seconds for Modal's acknowledgement
        |
        +------> return the temporary evaluation_id
        |
        v
Modal report coordinator
        | atomically create or resolve the temporary parent row
        | stop if another invocation already owns the same logical run
        |
        +------> baseline single-simulation call
        |
        +------> reform single-simulation call
        |
        +------> write aggregate artifact and durable state

authenticated caller
        |
        | GET /internal/stage12/reports/{evaluation_id}
        v
Cloud Run Simulation Entrypoint
        |
        | typed SQLAlchemy Core read
        v
API-owned temporary PostgreSQL tables
```

The Modal report coordinator waits for the child Modal calls. Cloud Run does
not poll a Modal `FunctionCall`; the client polls Cloud Run, and Cloud Run reads
the durable state directly from the API-owned temporary tables through the
shared typed persistence package.

Example submission:

```bash
curl -X POST "${SIMULATION_ENTRYPOINT_URL}/internal/stage12/reports" \
  -H "Authorization: Bearer ${SIMULATION_ENTRYPOINT_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-PolicyEngine-Request-ID: stage12-manual-example" \
  --data '{
    "country": "us",
    "scope": "macro",
    "region": "us",
    "time_period": "2026",
    "baseline": {},
    "reform": {}
  }'
```

An accepted request returns `202` and `Retry-After: 1` without waiting for the
calculations or for the coordinator's first database write:

```json
{
  "evaluation_id": "00000000-0000-0000-0000-000000000001",
  "status": "running",
  "poll_url": "/internal/stage12/reports/00000000-0000-0000-0000-000000000001"
}
```

Each POST intentionally creates a new execution. After receiving the
`evaluation_id`, callers use the GET route for all subsequent status checks.
An immediate first GET can return `404` with `Retry-After: 1` while the Modal
coordinator is starting and has not created the parent row; callers must bound
retries because the same status also represents an identifier that does not
exist.
While the report is pending or running, GET returns `202` and `Retry-After: 5`;
completed, failed, incomplete, or skipped states return `200`. The response
contains the complete temporary parent and child execution metadata, including
private artifact references and digests, but never returns artifact contents.
Cloud Run therefore requires no additional object-storage permission for this
interface.

## Automatic execution, result comparison, and rollback

The deployment workflow passes the environment-specific `STAGE12_ENABLED`
GitHub variable to Cloud Run. Set the `beta` value to `1` and deploy to enable
staging after qualification. Set it back to `0` and deploy to stop new
automatic and direct staging runs. Production uses the same explicit
deployment sequence with the `prod` variable. Changing this value does not
deploy or remove Modal workers, does not make existing run metadata
unavailable, and does not change the existing production forwarding
configuration.

When enabled, a successful production submission causes the Simulation
Entrypoint to derive a deterministic temporary evaluation identifier from the
production job and selected v2 release, prepare the parent metadata in memory,
and request an invocation of the already-deployed Modal report coordinator.
Cloud Run performs no Stage 12 persistence write on this submission path. It waits
only for Modal to acknowledge the invocation, for at most five seconds. There
is no process-local queue. An acknowledgement failure or timeout is logged, and
the unchanged production response is returned.

The Modal report coordinator asks the API's canonical service to atomically
create or resolve the parent record before starting child work. Repeated
production submissions can request more than one coordinator invocation, but
the deterministic evaluation identifier and database uniqueness constraint
make them one logical Stage 12 run. A later
coordinator that finds the run already active or complete exits without
starting child simulations. Unsupported automatic inputs are logged with a
bounded reason and are not persisted.

The report coordinator starts baseline and reform as independent Modal calls,
waits for them, and derives the Stage 12 aggregate. It then restores the
production Modal function call by the retained production job identifier and
waits up to 15 minutes for that production call to finish. It compares both
complete aggregate result objects exactly. The private
`reports/comparison.json` receipt contains:

- the SHA-256 digest of each complete result object;
- every differing scalar leaf, addressed by JSON Pointer;
- presence and both values for each difference; and
- absolute and relative deltas when both values are numeric.

The receipt does not duplicate either complete result object. Its URI, digest,
schema version, completion time, and status are written to the temporary parent
row. Comparison retrieval, calculation, artifact, or persistence failure cannot
change the already-successful Stage 12 aggregate and cannot affect the existing
production result. The comparison is attempted once for each Stage 12
execution. A failed comparison is final for that execution, and ordinary or
repeated production submissions do not retry it. Any future operator-initiated
comparison retry requires a separately reviewed entry point and state
transition. Direct Stage 12-only runs have no production result and keep
comparison status `not_requested`.

The artifact bucket deletes private Stage 12 objects after 30 days. This
application registers no periodic cleanup function and does not delete the
temporary PostgreSQL rows. Those rows remain restricted operational history
until Stage 14 removes the temporary schema.

Household computation, budget-window computation, public response fields,
public identifiers, and production persistence are outside this companion
change.

## Numerical qualification

The executor provides `src.modal.utils.qualify_stage12_parity` for a controlled
calculation. It accepts the existing combined request and the corresponding
canonical report input. The command runs the existing combined executor with
its private microdata export, starts the two v2 single-simulation calculations,
compares every entity, row identity, column, dtype, and value, rebuilds the v2
aggregate, and compares every aggregate field. Differences fail qualification
with only a bounded code and field path; calculation values are not included in
the diagnostic or receipt. Numerical tolerances default to zero and can be
introduced only for explicit field paths through a reviewed JSON file.
