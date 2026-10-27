# Migration provisioning

During the one-time migration, the Public API exports the v1 reports that
account rows refer to. This package reads that export and creates each
report's simulations in `simulation.simulations` and the report in
`simulation.reports`, with no run. The first time a migrated report is read,
the Public API calculates it on the current catalog version.

It is temporary: item 5.04 removes this package.

## Input file: report definitions, schema version 1

One JSON object. The models in `definitions.py` are the format's definition;
this section describes them.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | `1` | The format's version. Any other value is rejected. |
| `reports` | array | One definition per v1 report. Each `legacy_report_id` appears once. |

Each report definition:

| Field | Type | Meaning |
| --- | --- | --- |
| `kind` | `"economy"` or `"household"` | The report's kind, and its simulations' kind. |
| `legacy_report_id` | positive integer, at most 2^63 − 1 | The v1 report ID. |
| `country` | two lowercase letters | The report's country, and its simulations'. |
| `year` | integer, 1900 to 2200 | The year the report calculates. |
| `options` | JSON object | The report's options, stored as given. |
| `baseline` | simulation | The baseline simulation. |
| `reform` | simulation, or `null` for a household report | The reform simulation. An economy report must have one. |

Each simulation of an economy report:

| Field | Type | Meaning |
| --- | --- | --- |
| `legacy_simulation_id` | positive integer, at most 2^63 − 1 | The v1 simulation ID. |
| `policy_id` | UUID or `null` | The v2 policy that `legacy_policy_mappings` maps the v1 policy to, or `null` for current law. |
| `region_code` | text, no whitespace | The v2 region code the Public API's region reference files map the v1 region to: `us`, `state/ca`, `country/northern_ireland`, `constituency/E14001063`. |
| `dataset_name` | text, no whitespace, or `null` | A catalog dataset name, or `null` for the region's default dataset in each catalog version. |

Each simulation of a household report:

| Field | Type | Meaning |
| --- | --- | --- |
| `legacy_simulation_id` | positive integer, at most 2^63 − 1 | The v1 simulation ID. |
| `policy_id` | UUID or `null` | As for an economy simulation. |
| `household_id` | UUID | The v2 household that `legacy_household_mappings` maps the v1 household to. |

Rules the file must follow, checked before anything is written:

- Every field is required, including those that may be `null`, and no other
  field is allowed.
- Numbers are JSON numbers and IDs are JSON strings; nothing is converted from
  text.
- Nothing holds a value the database cannot store: no text, including the
  options' keys and values, contains a NUL character, and the options hold no
  NaN or Infinity.
- An economy report's two simulations have the same `region_code` and
  `dataset_name`; a household report's have the same `household_id`.
- A v1 simulation that several reports use is described the same way in each.
- A v1 economy report with a single simulation, which v1 calculates as that
  policy over itself, names the same simulation as `baseline` and `reform`.

The export resolves every v2 reference itself. This package reads no Public API
table and no region reference file, and the runtime role it connects as has no
privilege on the Public API's tables.

Example:

```json
{
  "schema_version": 1,
  "reports": [
    {
      "kind": "economy",
      "legacy_report_id": 4512,
      "country": "us",
      "year": 2026,
      "options": {},
      "baseline": {
        "legacy_simulation_id": 9001,
        "policy_id": null,
        "region_code": "state/ca",
        "dataset_name": null
      },
      "reform": {
        "legacy_simulation_id": 9002,
        "policy_id": "0b6f1c9e-3f43-4d6a-9d51-6f3a2c1b8e07",
        "region_code": "state/ca",
        "dataset_name": null
      }
    },
    {
      "kind": "household",
      "legacy_report_id": 4513,
      "country": "uk",
      "year": 2025,
      "options": {},
      "baseline": {
        "legacy_simulation_id": 9003,
        "policy_id": null,
        "household_id": "5d0c3a48-2b8e-4f1e-a7c2-9e4b6d1f0a33"
      },
      "reform": null
    }
  ]
}
```

## Running it

An operator runs it from a checkout of `main`, once per environment, after
the Public API's migration command has written the export. It is not an HTTP
route and runs nowhere else.

1. Install the library with its `provisioning` extra:

   ```bash
   cd libs/policyengine-simulation-persistence
   uv sync --frozen --extra provisioning
   ```

2. Put the runtime role's URL in `SIMULATION_DATABASE_URL`, the variable the
   Simulation Entrypoint reads. The deploy mounts the newest enabled version
   of the Secret Manager secret that the `SIMULATION_DATABASE_URL_SECRET_NAME`
   variable of sim's GitHub environment names (`beta` for staging, `prod` for
   production): a secret name in the project named by
   `SIMULATION_ENTRYPOINT_GCP_PROJECT_ID`, or a full
   `projects/<project>/secrets/<name>` reference. Only the entrypoint's runtime
   service account can read it today, so the operator needs read access to it
   first. `latest` below names the newest version; if that version is
   disabled, give the newest enabled version's number instead.

   ```bash
   SIMULATION_DATABASE_URL="$(gcloud secrets versions access latest \
     --secret="$SECRET_NAME" --project="$PROJECT_ID")"
   export SIMULATION_DATABASE_URL
   ```

3. Dry run, then review its counts and failures:

   ```bash
   uv run --frozen --extra provisioning \
     python -m policyengine_simulation_persistence.migration_provisioning \
     --definitions report-definitions.json --dry-run
   ```

4. Run, writing the report mapping to a new file:

   ```bash
   uv run --frozen --extra provisioning \
     python -m policyengine_simulation_persistence.migration_provisioning \
     --definitions report-definitions.json --output report-mapping.json
   ```

Before it reads or writes a row, the command checks:

- the definitions file, listing up to 20 errors;
- for a run, that the output file does not exist yet and its directory does;
- that `SIMULATION_DATABASE_URL` is set, uses `postgresql://` and logs in as
  the runtime role, `simulation_runtime`, never the migrator;
- that the database is at the schema revision this code expects.

For each definition it creates the simulations, keyed by
`legacy_simulation_id`, and then the report, keyed by `legacy_report_id`,
through the stores' `create_migrated` methods. A row that already exists is
reused if it matches the definition, so running the same export again creates
nothing and maps the same reports. A row that exists with a different
definition, or a definition the database rejects (a policy or household the
Public API does not have, one of another country, or a value it cannot store,
such as text holding a NUL character), fails that report alone; the other
reports continue.

Each row is committed on its own. If a report fails after some of its
simulations were created, they stay, keyed by their v1 IDs, and a later run
reuses them.

The dry run only reads. It counts the simulations and reports a run would
create and those that already exist, and reports conflicts with existing rows.
The runtime role cannot read the Public API's tables, so a dry run does not
find a policy or household the database would reject; the run reports those.

Both print a JSON summary: `dry_run`, `new_simulations`,
`existing_simulations`, `new_reports`, `existing_reports` (in a dry run, the
`new_` counts are what a run would create) and `failures`.

| Exit status | Meaning |
| --- | --- |
| 0 | Every report was provisioned, or in a dry run would be. |
| 1 | Some reports failed, or in a dry run would fail. The summary lists them, and so does a run's output file; the other reports are provisioned. |
| 2 | The command refused to start. Nothing was written. |
| 3 | The database stopped the command partway, or a run's output file could not be written. A run's rows created before the stop are kept, and running the command again, with a new output file, reuses them. A dry run writes nothing. |

## Output file: report mapping, schema version 1

A run writes it at the end; `mapping.py` defines it. The Public API's migration
command reads it and sets each account row's v2 report ID.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | `1` | The format's version. |
| `reports` | array | Each provisioned report: `legacy_report_id`, its v2 `report_id`, and `created` (`false` when an earlier run had created it). |
| `failures` | array | Each failed report: `legacy_report_id` and a one-line `reason`. |

```json
{
  "schema_version": 1,
  "reports": [
    {
      "legacy_report_id": 4512,
      "report_id": "6c1d7f0a-8f5e-4a3b-9c2d-1e4f7a9b3c55",
      "created": true
    }
  ],
  "failures": [
    {
      "legacy_report_id": 4513,
      "reason": "the database rejected v1 simulation 9003: insert or update on table \"simulations\" violates foreign key constraint \"fk_simulations_household_country\""
    }
  ]
}
```
