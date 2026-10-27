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
| `legacy_report_id` | positive integer | The v1 report ID. |
| `country` | two lowercase letters | The report's country, and its simulations'. |
| `year` | integer, 1900 to 2200 | The year the report calculates. |
| `options` | JSON object | The report's options, stored as given. |
| `baseline` | simulation | The baseline simulation. |
| `reform` | simulation, or `null` for a household report | The reform simulation. An economy report must have one. |

Each simulation of an economy report:

| Field | Type | Meaning |
| --- | --- | --- |
| `legacy_simulation_id` | positive integer | The v1 simulation ID. |
| `policy_id` | UUID or `null` | The v2 policy that `legacy_policy_mappings` maps the v1 policy to, or `null` for current law. |
| `region_code` | text, no whitespace | The v2 region code the Public API's region reference files map the v1 region to: `us`, `state/ca`, `country/northern_ireland`, `constituency/E14001063`. |
| `dataset_name` | text, no whitespace, or `null` | A catalog dataset name, or `null` for the region's default dataset in each catalog version. |

Each simulation of a household report:

| Field | Type | Meaning |
| --- | --- | --- |
| `legacy_simulation_id` | positive integer | The v1 simulation ID. |
| `policy_id` | UUID or `null` | As for an economy simulation. |
| `household_id` | UUID | The v2 household that `legacy_household_mappings` maps the v1 household to. |

Rules the file must follow, checked before anything is written:

- Every field is required, including those that may be `null`, and no other
  field is allowed.
- Numbers are JSON numbers and IDs are JSON strings; nothing is converted from
  text.
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
