# Certified ACS state preparation: partial pilot results

Date: 2026-10-08. **The full-source partition passed; year preparation is
blocked by missing WIC take-up inputs in the certified source.** Two 2025 jobs
failed, and the remaining four pilot jobs were not submitted. Do not treat this
as a completed six-job pilot or use failed-job times to forecast preparation.

## Scope and identities

The user approved an initial $20 budget for one full-source partition and
California/Utah preparation for 2025–2027, with at most two preparation workers.
No remaining-state jobs, national reference builds, production integration,
database work, or request routing were authorized or performed.

- Analysis prototype: `prototype/us-state-year-precompute`, commit `0fb291b`.
- Simulation prototype: same branch name, executed commit `74ac226`.
- [Modal testing application](https://modal.com/apps/policyengine/testing/ap-OYrp9Yh4IwqjfPeh27I7br):
  `policyengine-acs-preparation-pilot-x3zpjs`.
- Retained testing volume: `policyengine-acs-preparation-data-x3zpjs`.
- Dataset: `populace_us_2024_acs_local`, resolved by name through the bundled
  manifest, not an unmanaged URI.
- Source release: `populace-us-2024-buildo-acs-local-767312d60-20260923T074941Z`.
- Source SHA-256: `769756c31f3ca646d12c272511744dec04c0e68870c6946dd945fbba65b6a7ec`.
- Runtime-derived installed versions: `policyengine=6.2.2`,
  `policyengine-us=2.2.1`, `policyengine-core=3.32.10`, `spm-calculator=1.0.0`.

Hugging Face's anonymous-access metadata request confirmed the file size and
matching hash before the run. No Hugging Face secret was supplied or changed.
The source was downloaded once in Modal and retained there. Only the smaller
Utah derivative was subsequently downloaded locally for read-only diagnosis.

## Full-source partition measurements

| Measurement | Result |
| --- | ---: |
| Source download and hash verification | 206.888 s |
| Hash verification, native-table load, partition writes and readback | 314.830 s |
| Combined profiled duration | 521.725 s |
| Process CPU time | 329.010 CPU-seconds |
| Sampled peak process RSS | 18.667 GiB |
| Process high-water RSS | 18.667 GiB |
| Sampled peak container memory | 18.663 GiB |
| Source bytes | 9,821,668,812 (9.147 GiB) |
| 51 derivative files, combined bytes | 10,128,659,971 (9.433 GiB) |
| Independent memory samples | 4,875 |

The function requested eight physical cores and 128 GiB, with a one-hour
execution timeout and 128-GiB hard memory limit. CPU used an explicit soft limit
matching its request. The measurement excludes image build, initial container
imports, and final volume commit. The complete retained native tables contain
1,588,854 households and 3,589,209 people, with 2,177,474 tax units, 1,591,514 SPM
units, 1,596,820 families, and 2,869,051 marital units.

All 51 files passed exact readback comparisons. Membership validation and the
typed manifest verified complete, disjoint state/DC coverage and preservation
of entity counts. Values, identifiers, weights, and source periods were not
changed. The source hash and each derivative's hash are separate identities.

## Attempted preparations

| Task | Status | Time until failure | Sampled peak container memory | Native input size | Prepared output size |
| --- | --- | ---: | ---: | ---: | --- |
| California, 2025 | Failed input validation | 21.954 s | 7.413 GiB | 1,096,720,867 bytes | Not produced |
| Utah, 2025 | Failed input validation | 3.367 s | 1.735 GiB | 112,967,144 bytes | Not produced |
| California/Utah, 2026–2027 | Not submitted | Not measured | Not measured | Same state inputs | Not produced |

Both calls requested eight cores and 64 GiB, with one-hour timeouts and hard
64-GiB limits. No artificial overlap holds or synchronization were used. The
dispatcher stopped new submissions on the first failure and awaited the other
already-started call. No calls were retried and no resources were increased.

These failed-job memory values cover only loading and validation, not complete
year preparation. They cannot establish successful runtime, memory, annual
artifact size, or the cost of the remaining 147 state/year preparations.

## Cause of the failure

Both worker reports contain:

```text
Cannot map stored 'would_claim_wic' onto 'takes_up_wic_if_eligible'
for 2024: the stored column has missing values.
```

The source's person table stores the legacy field `would_claim_wic`. The current
country model instead defines the monthly boolean input
`takes_up_wic_if_eligible`. The analysis package's existing compatibility helper
maps complete stored values into that current input and refuses missing values.
It does not invent values or silently drop the old column.

Read-only inspection of the verified Utah derivative found:

- 39,679 people; legacy field dtype `object`.
- 36,778 missing values (92.7%).
- 2,733 `False` and 168 `True` values.
- No `takes_up_wic_if_eligible` column.

The local derivative SHA-256 matched the partition manifest:
`67f6a3e071e89e8d785407a01fb346459d86cca2482004d0facb4ab2893a0595`.
The partition readback check establishes that it preserved the source values;
partitioning did not introduce these missing entries. California failed the
same check; its precise missing-value count has not been measured.

[Analysis commit d4bf360](https://github.com/PolicyEngine/policyengine.py/commit/d4bf360fbd0dca8b837ed8f7db37d0ec44610945),
dated September 25, introduced this mapping for stored WIC take-up draws. The
certified ACS source predates it (September 23). That commit documents why
dropping the old field was incorrect: the new country input defaults to `True`,
causing every eligible person to take up WIC when the stored draw is ignored.
The commit added the same helper to national `create_datasets`, managed
microsimulation, and simulation execution, not only this prototype.

National `create_datasets` calls that helper before extracting years, as does
the prototype. Consequently this source/model combination has the same
validation problem in that national path. This is a code-path inference, not a
claim that a full national build was run. The test has not established whether
the missing values originate specifically from ACS records, how they should be
imputed, or which source-build operation omitted them.

**No WIC values, null handling, country defaults, certified hashes, or source
releases were changed.** Continuing requires an explicit decision about fixing
the source inputs or their documented interpretation; it is not a memory or
parallel-dispatch fix. No blanket null-to-boolean conversion is acceptable as
an unreviewed way to complete this benchmark.

## Evidence recovery and automated checks

All three measurement records were checked against 5,115 raw JSONL samples.
Sample counts, peak RSS, and container peaks matched. Mean sample spacing was
107 ms and maximum spacing 360 ms, for a nominal 100-ms sampler. Peaks are
sampled, not guaranteed continuous container maxima.

The automatic evidence export hit a Modal `ServiceError: stream timeout`
during volume listing after the application stopped. An independent read-only
export recovered every JSON/JSONL file without restarting compute. The pilot
command now records the primary execution error separately and preserves it if
evidence retrieval also fails; three focused tests cover that behavior.

Nine new planning/budget/source-identity tests passed, as did twelve existing
benchmark checks across focused runs. One existing local allocation test timed
out while waiting for its sampler on the first run, then passed separately
without changing its timeout. Ruff and focused Pyright passed. No full executor
suite was run. The paid pilot is recorded as failed despite those unit checks.

Evidence is preserved outside the repositories:

```text
/Users/admin/Documents/benchmark-results/us-state-preparation-2026-10-08/
  pe-acs-pilot-evidence-x3zpjs/
    partition-result.json
    partition-manifest.json
    resources.json
    wic-diagnosis.json
    evidence-verification.json
    partition-measurements/{measurement.json,phases.json,samples.jsonl}
    measurements-ca-2025/{measurement.json,phases.json,samples.jsonl}
    measurements-ut-2025/{measurement.json,phases.json,samples.jsonl}
  state-ut-x3zpjs.h5
```

## Cost, retained storage, and next checkpoint

At [Modal's published requested-resource rates](https://modal.com/pricing),
checked 2026-10-08, the measured function-body intervals imply approximately
$0.203 for source materialization/partition and $0.006 for the two failed calls,
combined $0.209. This is **not the actual invoice**: it excludes build, startup,
commit, idle, network, and storage charges, and actual metered usage may differ.
The initial full-timeout resource estimate was $7.294, not a charge incurred.

The retained source and 51 extracts total approximately 18.580 GiB. At the
listed $0.09/GiB/month, that is about $1.67/month if all of it exceeds included
workspace storage. Remaining included storage was not checked. The California
and Utah **native** files are 1.021 and 0.105 GiB respectively; per-year prepared
file sizes for 2025–2027 remain unknown.

The testing application is stopped with zero running tasks. Its uniquely named
volume remains to avoid another national download/partition after the WIC
issue is resolved. No staging/production service or unrelated testing resource
was modified. Production cache integration and the remaining benchmark work
are still unimplemented and must wait for successful preparation and the
planned approval checkpoints.
