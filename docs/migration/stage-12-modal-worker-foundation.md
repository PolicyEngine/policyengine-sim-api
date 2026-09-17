# Stage 12 Modal worker foundation

Companion change identifier: `stage-12-modal-worker-foundation`.

This change implements the simulation-service portion of the Stage 12 plan
defined in `PolicyEngine/policyengine-api`. It does not change the public
simulation submission or polling contracts.

## Service ownership

- The Cloud Run Simulation Entrypoint keeps its current production adapter,
  which forwards submissions and polls to the existing Modal HTTP routing
  service. Responses from that path remain authoritative.
- A separate evaluation adapter reads only a separately stored v2 version
  manifest and invokes separately named v2 Modal functions directly.
- Invocation context carries both the logical deployment environment used for
  records and artifact paths (`staging` or `production`) and the Modal
  environment used for function lookup (`staging` or `main`). Production child
  simulations therefore resolve in Modal `main` without relabeling durable
  records as `main`.
- One v2 report-coordinator invocation starts baseline and reform as distinct
  single-simulation calls before awaiting either result.
- The v2 functions write canonical private artifacts and temporary evaluation
  state. They do not create production simulations, reports, report runs, or
  user associations.

The existing Modal routing service, its versioned executor applications, and
its v1 routing manifest remain deployed and unchanged throughout Stage 12.

## Cross-repository contract

`PolicyEngine/policyengine-api` is the only schema authority for the temporary
evaluation tables and the canonical source for the Stage 12 request, artifact,
and persistence contracts. This repository consumes the generated contract
identified by:

```text
https://policyengine.org/contracts/stage-12-worker-v1.json
```

Compatibility tests must compare this repository's adapters with that
generated document. This repository must not define SQLModel tables or an
Alembic migration for the evaluation records, and its runtime must not execute
DDL.

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
- V2 worker object access uses the separate
  `stage12-evaluation-gcp-credentials` Modal secret. The worker application
  does not receive the existing general GCP credential secret.
- Dual execution is configured as enabled or disabled per flow and environment,
  defaults to disabled, and never selects only a percentage of eligible jobs.
- The Cloud Run revision can contain the Stage 12 adapter while the separate
  `simulation-api-v2-dual-execution` control document remains disabled. The
  entry service reads that document for each accepted job and fails closed if
  it is missing or invalid, so operators can stop new evaluation dispatch and
  v2 manifest selection without redeploying the entry service or changing the
  production forwarding configuration.

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
| Configure the v2 manifest reader and binary flow settings | Cloud Run deployment workflow |
| Create the private artifact namespace and its retention policy | Infrastructure configuration |
| Grant private-object access only to the Modal runtime and evaluation-row DML to the entry and Modal runtimes | Infrastructure configuration |
| Attach named runtime secrets without exposing their values | Simulation deployment workflow |
| Disable or restore v2 manifest selection and dual execution | Deployment rollback automation |
| Recreate any Stage 12 service or storage resource after loss | The same deployment and infrastructure automation |

When a deployment requests the Stage 12 resources, the ordinary integration
and promotion jobs wait for storage configuration, v2 worker validation, and
separate v2 manifest publication to succeed. A failed Stage 12 deployment
therefore prevents promotion of a Cloud Run revision configured to use it.

Every merge to `main` requests the Stage 12 resources in both the `beta` and
`prod` jobs. Each job validates its pre-provisioned environment-specific
resources, deploys and validates the separate v2 Modal application, publishes
the separate v2 manifest, and deploys a Stage 12-configured Cloud Run revision.
The `prod` job runs only after `beta` succeeds. Each environment's independent
dual-execution control remains disabled during deployment, so deploying Stage
12 does not copy production-path submissions until an operator enables the
approved flow for that environment. Production prerequisites must therefore be
provisioned and verified before merging; missing production prerequisites are
a deployment failure, not a reason to omit Stage 12 from the `prod` job.

This automatic production deployment creates or updates a dormant Stage 12
runtime: the separate Modal application, v2 manifest, runtime configuration,
and temporary authenticated direct endpoint are present for later updates and
qualification. It does not send ordinary production calculations to the Stage
12 report coordinator or single-simulation workers. The promoted Cloud Run
revision continues forwarding those calculations to the existing Modal HTTP
routing service and existing executors while both production control settings
remain false.

Existing environment, domain, and production-service resources are inputs to
Stage 12 rather than resources this change provisions. If implementation later
reveals an operation that is genuinely required only once, work must pause
until its bounded disposable script, target checks, secret references, and
verification output have been reviewed. The script is removed only after the
created state is independently verified.

## Initial flow

The initial evaluation flow is `POST /simulate/economy/comparison` for reviewed
annual society-wide comparison inputs. The production request is forwarded
unchanged. Only a newly accepted supported production job may create one
corresponding evaluation report; polls, cached results, and repeated submissions
that resolve the same production job do not create another evaluation.

Deployment keeps one environment-specific PostgreSQL runtime role for the
Simulation Entrypoint, report coordinator, single-simulation functions, and
retention cleanup. `policyengine-api` remains the source of truth for that
role: it has `SELECT`, `INSERT`, `UPDATE`, and `DELETE` only on the two temporary
Stage 12 tables, has no schema-migration authority, and participates in no role
membership. Before deploying, the simulation workflow connects with the actual
runtime secret, verifies the effective role and table access, exercises all
four operations with canary rows inside a rolled-back transaction, and rejects
access to other public tables. It also verifies required Secret Manager access
and performs a create/read/delete canary in the private artifact bucket using
the exact Modal service-account credential. The storage canary is deleted in
the normal path and by exit cleanup after a failure.

The current Public API omits `data` for the certified default dataset. The v2
adapter therefore resolves an absent `data` field to the exact default dataset
in the selected `.py` bundle. An explicit `data` value is eligible only when it
identifies an artifact declared by that same bundle.

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
They are available whenever the Stage 12 backend is configured. Manual direct
submissions do not consult the automatic dual-execution control, so disabling
automatic economy copies does not disable these authenticated routes.

The direct submission does not call the existing production computation and
does not keep a Cloud Run request open while calculations run:

```text
authenticated caller
        |
        | POST /internal/stage12/reports
        v
Cloud Run Simulation Entrypoint
        | validate request
        | create temporary parent row
        | resolve separate v2 manifest
        | call Modal spawn and retain its invocation ID
        |
        +------> return evaluation_id
        |
        v
Modal report coordinator
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
Cloud Run reads temporary PostgreSQL state only
```

The Modal report coordinator waits for the child Modal calls. Cloud Run does
not poll a Modal `FunctionCall`; the client polls Cloud Run, and Cloud Run reads
the durable PostgreSQL records.

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

An accepted request returns `202` without waiting for the calculations:

```json
{
  "evaluation_id": "00000000-0000-0000-0000-000000000001",
  "status": "running",
  "poll_url": "/internal/stage12/reports/00000000-0000-0000-0000-000000000001"
}
```

Each POST intentionally creates a new execution. After receiving the
`evaluation_id`, callers use the GET route for all subsequent status checks.
While the report is pending or running, GET returns `202` and `Retry-After: 5`;
completed, failed, incomplete, or skipped states return `200`. The response
contains the complete temporary parent and child execution metadata, including
private artifact references and digests, but never returns artifact contents.
Cloud Run therefore requires no additional object-storage permission for this
interface.

## Runtime enablement and rollback

`STAGE12_DUAL_EXECUTION_ECONOMY=1` installs the evaluation adapter and its
separate credentials in a Cloud Run revision; it does not by itself start any
evaluation work. The runtime decision comes from the separately stored
`simulation-api-v2-dual-execution` document. A missing, malformed, disabled, or
wrong-environment document stops automatic production-copy dispatch before
manifest lookup or database access. It does not disable the authenticated
temporary direct runner described above.

After staging qualification, enable complete economy comparisons with:

```bash
uv run python -m src.modal.utils.set_stage12_dual_execution \
  --environment staging \
  --manifest-selection-enabled \
  --economy-enabled
```

Stop new evaluation dispatch and v2 manifest selection without changing the
Cloud Run revision or the existing production forwarding path with:

```bash
uv run python -m src.modal.utils.set_stage12_dual_execution \
  --environment staging \
  --no-manifest-selection-enabled \
  --no-economy-enabled
```

The same commands require the explicit `production` environment for production
state. These controls are binary: there is no percentage, request bucket, or
partial-dataset mode.

Automatic copies use a fixed-size dispatcher in each Cloud Run instance. The
defaults allow 8 active dispatch attempts and 32 additional waiting requests.
Each attempt has a 15-second dispatch timeout. Deployments can set
`STAGE12_DISPATCH_MAX_IN_FLIGHT`, `STAGE12_DISPATCH_QUEUE_CAPACITY`, and
`STAGE12_DISPATCH_TIMEOUT_SECONDS` to reviewed values within the limits checked
at startup. When the waiting capacity is exhausted or an attempt times out, the
service records that Stage 12 failure and leaves the accepted v1 response
unchanged. Because Python cannot safely terminate a synchronous database or
Modal client call already running in a thread, a timed-out call retains its
fixed worker slot until it returns. This prevents abandoned calls from
accumulating while keeping memory, asynchronous tasks, and active dispatches
bounded independently of production request concurrency.

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
