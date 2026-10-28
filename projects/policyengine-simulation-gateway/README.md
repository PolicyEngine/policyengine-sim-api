# policyengine-simulation-gateway

The stable Modal gateway for the simulation service: routes simulation
requests to versioned executor apps (`policyengine-simulation-py{X}`) via
the routing state in `modal.Dict`, and serves the public API contract.

## Dataset selection

The economy comparison and budget-window endpoints use the certified dataset
from the selected PolicyEngine.py release. They reject request fields `data`
and `data_version` with HTTP 422, including when either field is null. The
response still reports the dataset and data version used. Use `region` or
`region_group` to select a geographic subset; the worker resolves the
appropriate dataset for that region from its release bundle.

## Country-version routing

A request that names a country model `version` resolves to the app the
routing state lists for it. When the registry ties that app to exactly one
wrapper bundle and the bundle's manifest states a *different* model version
for the country, the gateway refuses the request with a 400 instead of
routing it.

That is a deliberate change from the pre-canonical gateway, which served
the route and returned a self-contradictory body — `version` from the stale
route, `policyengine_bundle.model_version` from the live manifest. It can
happen without anyone doing anything wrong: publishing only ever *adds*
country routes and overwrites the bundle manifest for the wrapper it
deploys, and nothing prunes the old country route, so re-publishing one
wrapper with an upgraded country model leaves the previous country version
pointing at an app that no longer serves it. Callers pinning an old
`version` (the `policyengine-apis-integ` suite pins one) should move to a
version the current bundle states, or pass `policyengine_version`
explicitly.

A manifest that states *no* model version for the country — entry absent,
or the value absent, non-string, empty or whitespace — contradicts nothing
and still routes.

## Job stage reporting

`GET /jobs/{job_id}` adds optional fields to annual economy jobs, so a
client can show real progress instead of a fixed duration estimate. Clients
that read only `status` see no change.

| Field | Meaning |
|---|---|
| `stage` | `queued`, `loading_data`, `baseline`, `reform`, `simulating` or `outputs`. Running jobs report their current stage; failed jobs report the stage they stopped in; complete jobs omit it. Later versions may add stages, so clients must accept a value they do not know. |
| `progress` | Coarse percent, 0–100, at the start of the current stage. It changes only at stage boundaries, stays below 100 while running, and is 100 when complete. |
| `progress_max` | The highest percent a running job reaches before its next stage starts. |
| `submitted_at`, `started_at`, `stage_started_at`, `finished_at` | UTC timestamps. `started_at - submitted_at` is the queue time. |
| `segments` | `{completed, total}` region segments of a segmented US national run, for captions. |
| `stage_durations` | Seconds spent in each stage that has ended. |

A monolithic run moves through `loading_data`, `baseline`, `reform` and
`outputs`. A segmented US national run computes baseline and reform together
in 20 region segments, so it moves through `loading_data`, `simulating` and
`outputs`.

The `run_simulation` worker writes a stage record to the Modal Dict
`simulation-api-job-progress`, keyed by `job_id`. If Modal runs the same call
again after a preemption, the preempted attempt leaves its record running
rather than finishing it, and the new attempt continues that record and keeps
the reported stage until it reaches a stage at least as far along, so
`progress` never moves backwards, however many times the call is preempted. Budget-window children are ordinary
`run_simulation` calls and write their own records, which
`/budget-window-jobs` does not read yet. The gateway reports
`queued` for a job without a record only when the worker app's bundle
manifest declares `job_progress`; apps deployed before stage reporting never
write a record, so their jobs get no stage fields. A failed read or a
malformed record drops the stage fields; it never changes the status, the
result or the HTTP status code.

The `progress` value at each stage start (`STAGE_PROGRESS_FLOOR` in
`policyengine_simulation_contract.job_progress`) depends on the execution
path, which the worker records as `monolithic` or `segmented`.
Completed segments do not move `progress`: the regional children finish at
about the same time and California finishes last, so a segment count jumps
late and then waits. For a smooth bar, a client moves its own time-based
estimate between `progress` and `progress_max` and never moves the bar
backwards. While a job is `queued`, its path is unknown, so `progress_max`
is the highest limit of any path. The values
come from one week of production telemetry (6–13 October 2026): queue time
is about 39% of a UK national run, while the segmented calculation is about
96% of a US national run. They stay provisional until per-stage runtime
statistics exist for each country and region type (#780, phase 2).

The worker also adds `job_stage_seconds_<stage>` attributes to its
`run_simulation` operation, so telemetry can split the baseline from the
reform calculation.

## UK local-area breakdowns

A UK request to `POST /simulate/economy/comparison` may set
`breakdown: "local_areas"`. The job then computes only the constituency and
local-authority impacts, on the local-area dataset that the policyengine.py
bundle routes those regions to (`microcosm_uk_2024_25_dense` in .py 6.10.0),
filtered to the request's region. Its result is a `LocalAreaBreakdownOutput`
instead of a `SingleYearMacroOutput`:

| Field | Meaning |
|---|---|
| `kind` | Always `local_areas`. |
| `model_version`, `policyengine_version` | The country model and the policyengine.py release that computed it. |
| `dataset`, `data_version` | The local-area dataset's name and release revision. |
| `constituency_impact`, `local_authority_impact` | The published areas of each level. Disclosure control leaves withheld areas out, so a list may be empty; each record carries a `disclosure_tier` (`reported` or `approximate`) and 90% interval bounds. |
| `constituency_withheld_codes`, `local_authority_withheld_codes` | The ONS codes, sorted, of each level's areas that the run estimated but disclosure control did not publish. An area the run never estimated, such as one with no households in the dataset, is in neither list, so an area absent from both the records and this list has no estimate. An empty list means that no area was withheld. Results from older executors don't report them, and a result that doesn't report them leaves both keys out rather than sending null. |

Rules:

- The request must be for the UK, with `region` absent, `uk`,
  `country/england`, `country/scotland`, `country/wales` or
  `country/northern_ireland` (the nations policyengine.py registers). Any
  other region, including a constituency, a local authority, a bare name or
  `country/ni`, and `include_cliffs: true`, `segmented: true` or a
  `region_group`, is refused with 422, and so is any other `breakdown`
  value. `include_cliffs: false`
  is accepted, because the Public API always sends it. Budget-window
  requests do not take the field.
- The gateway sends a breakdown only to a worker app whose routing bundle
  manifest declares `local_area_breakdown: 1`. The routing publisher writes
  it for apps whose bundle routes both UK area region types to one
  local-area release dataset (`regional_release_kind` `local_area`). Any
  other route, including one pinned to an older version and one from the
  legacy version dicts, gets 400 before a job is spawned, and the message
  names the policyengine.py version. Older apps ignore fields
  they do not know, so they would otherwise return a national result.
- On those bundles, a UK-wide or nation-level request without `breakdown`
  computes no area results: `constituency_impact` and
  `local_authority_impact` are null, and the status body omits them.
  Constituency and local-authority requests are unchanged.
- The job reports the usual monolithic stages: `loading_data` while the
  local-area dataset loads, then `baseline`, `reform` and `outputs`.
- The entry starts no Stage 12 comparison for a breakdown request (skip
  reason `unsupported_options`).

## Image dependencies

The Modal image installs with `uv_sync(frozen=True)` from this project's
`uv.lock`, so the image environment is exactly what CI unit tests run
against — packages can only change through a relock (see issue #602 for
what fresh build-time resolution caused). Local packages (this package,
the contract/observability libs, policyengine-fastapi) are dev-group path
dependencies and ship into the image as mounted source; the image build
passes `--no-default-groups` so the dev group never installs in Modal's
build context.

## Common tasks

```bash
uv sync --extra test && uv run pytest          # unit tests (parity env)
uv run modal deploy --env=staging src/policyengine_simulation_gateway/app.py
uv run modal run --env=staging src/policyengine_simulation_gateway/smoke_app.py  # in-image import smoke
uv run python -m policyengine_simulation_gateway.generate_openapi
../../scripts/generate-clients.sh              # regen client for apis-integ
```

PRs touching image inputs run the smoke automatically
(`.github/workflows/pr-image-smoke.yml`).

The generated client package name stays `policyengine_api_simulation_client`
(external consumers depend on it; see `openapi-python-client.yaml`).
