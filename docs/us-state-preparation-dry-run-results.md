# US state preparation: first-checkpoint results

Date: 2026-10-08. Status: the small-data checkpoint passed. The full ACS download,
partition, and benchmark have **not** started. Production integration remains
unimplemented and requires the later checkpoint.

## Implemented prototypes

Both repositories have pushed feature branches named
`prototype/us-state-year-precompute`:

- [policyengine.py](https://github.com/PolicyEngine/policyengine.py/tree/prototype/us-state-year-precompute), tested commit `0fb291b`:
  typed source-derivative contracts; one validated partition into 50 states and
  DC; state/year preparation using the same year-extraction helper as national
  preparation; hash, entity-membership, and output readback checks.
- [Simulation API](https://github.com/PolicyEngine/policyengine-sim-api/tree/prototype/us-state-year-precompute), tested commit `f6644ca`:
  bounded parallel dispatch, independent memory sampling, failure reporting,
  and an opt-in testing-only benchmark. This branch is stacked on the existing
  regional-dataset branch at `cd5fe62`; it does not change that branch.

The partition preserves all six native entity tables, stable identifiers,
entity relationships, input values, weights, periods, and producer metadata.
Each derivative records its own SHA-256 separately from the source SHA-256.
Workers exchange typed artifact metadata, not DataFrames. The dispatcher permits
at most ten calls in flight and does not automatically retry failed calls.

No API v1 code, deployment workflows, production caches, dataset selection,
database permissions, credentials, or GitHub variables changed. No new scheduled
process exists. The existing national-only production precompute is unchanged.

## Successful Modal dry run

The benchmark ran in Modal's `testing` environment, not staging or production:
[application ap-Jh2jrKKOfhkvqwEV29xCqn](https://modal.com/apps/policyengine/testing/ap-Jh2jrKKOfhkvqwEV29xCqn).
Its resource names end in `l1744b`.

The input was a synthetic native-format HDF file containing one household and
one member of every entity per state/DC: 51 rows in each of six entity tables.
The input was 1,099,000 bytes, with SHA-256
`06539c82b5de8306e12593fd6eb946360daede93ea825d45026c7cf991d0d95d`.
It is **not** the certified ACS dataset. The test executed the actual partition
and country-model year-preparation functions, rather than mocks.

| Operation | Operation time | Sampled peak process RSS | Sampled peak container memory | Output bytes |
| --- | ---: | ---: | ---: | ---: |
| Partition all 51 states/DC | 5.207 s | 1.107 GiB | 1.121 GiB | 55,593,672 across 51 files |
| California, 2025 | 1.559 s | 1.150 GiB | 1.165 GiB | 37,296 |
| Utah, 2025 | 2.704 s | 1.154 GiB | 1.167 GiB | 37,296 |

GiB means bytes divided by 1,073,741,824. Operation time uses the named partition
or preparation phase. The partition's overall measurement was 5.208 s with
4.600 CPU-seconds. Preparation CPU time was 1.150 s for California and 1.370 s
for Utah. RSS measures the principal worker process; the separate Linux cgroup
counter measures container memory, including the sampler. These are sampled
peaks, not guaranteed continuous maxima.

The preparation workers overlapped for 15.839 s after their samplers started.
This interval deliberately includes readiness synchronization and a five-second
hold. It proves that two independent containers can run concurrently, **not**
that the small computations achieved a particular speedup. California's overall
measurement was 19.514 s, including 12.950 s of synchronization; Utah's was
15.839 s, including 8.129 s of synchronization. Container startup and image build
are outside these operation timings.

The probe requested one CPU core and 2 GiB, and the partition/preparation
functions requested two cores and 4 GiB each. These functions used hard memory
and execution limits, single-use containers, no retries, and a maximum of two
concurrent preparation containers.

## Measurement verification

- A separate process sampled worker RSS and the container memory counter at a
  nominal 100 ms interval. The 405 preserved samples had mean spacing 104 ms and
  maximum spacing 109 ms.
- Touching and holding a known 64 MiB allocation increased sampled RSS by
  67,117,056 bytes. The 600 ms hold measured 617 ms overall.
- An intentional `ValueError` produced a failed measurement with seven samples
  and the error recorded. It did not masquerade as a completed operation.
- All five measurement records matched their JSONL sample counts and peak RSS;
  all five could read `/sys/fs/cgroup/memory/memory.usage_in_bytes`. Process
  high-water readings provided an additional RSS cross-check.
- Records contain source/code identities, actual installed versions, requested
  resources, phase/CPU/elapsed times, input/output bytes, and failure status.
  Installed versions were `policyengine=6.2.2`, `policyengine-us=2.2.1`,
  `policyengine-core=3.32.10`, and `spm-calculator=1.0.0`, derived from the runtime.

Handled exceptions preserve a final report. Abrupt container termination can
leave only partial logs or samples; the prototype does not claim such a run
completed or guarantee artifact persistence after a forced termination.

## Earlier attempts and corrections

1. `e9xu3e`: both preparations completed, but fresh-container startup differed
   enough that their measured intervals did not overlap. The benchmark failed
   its concurrency assertion. Added bounded readiness synchronization after
   both samplers start and recorded that wait separately from preparation.
2. `cst882`: readiness checks every 200 ms exceeded Modal's volume-list request
   rate and raised `ResourceExhaustedError`. Reduced polling to once per ten
   seconds per worker. Preserved the failed-run measurement evidence.
3. `l1744b`: all first-checkpoint checks passed using those corrections.

## Focused automated checks

70 focused tests passed; the full executor suite was not run:

- 58 analysis-package tests: eight partition tests, six state/year tests, six
  existing persistence tests, 29 native-weight tests, seven alignment tests,
  and two targeted existing dataset-create/ensure integration tests.
- 12 simulation tests: three instrumentation tests, seven bounded-dispatch
  tests, and two readiness-synchronization tests.

The state/year tests compare every entity table for California and Utah in
2025, 2026, and 2027 against national preparation followed by filtering. They
also cover legacy-input renames, hash tampering, overwrite rejection, failed
write cleanup, and preservation of unrelated files. Ruff, focused mypy, and
focused Pyright checks passed. This establishes small-fixture equivalence;
full-source equivalence for all 153 combinations is still unverified.

## Evidence and cleanup

Raw JSON/JSONL evidence for all three attempts and the synthetic source are
stored outside the repositories:

```text
/Users/admin/Documents/benchmark-results/us-state-preparation-2026-10-08/
  synthetic-source.h5
  pe-state-preparation-first-attempt-e9xu3e/
  pe-state-preparation-evidence-cst882/
  pe-state-preparation-evidence-l1744b/
```

The successful directory includes `dry-run-summary.json`,
`partition-manifest.json`, `prepared-results.json`, and each operation's
`measurement.json`, `phases.json`, and `samples.jsonl`.

All three temporary benchmark apps stopped with zero running tasks. After
preserving the evidence, removed only the three testing volumes named
`policyengine-state-preparation-data-e9xu3e`,
`policyengine-state-preparation-data-cst882`, and
`policyengine-state-preparation-data-l1744b`. Their generated synthetic HDF
derivatives were deleted and can be regenerated from the retained source.
Unrelated testing apps, staging, and production were untouched.

## Approval needed before the full-source benchmark

The small fixture cannot establish the memory requirements, runtime, output
size, or total cost of the approximately 9.8 GB ACS source. Do not extrapolate
linearly from its timings or HDF file sizes: interpreter/model startup and HDF
metadata dominate this tiny test.

Proposed initial full-data pilot, subject to explicit approval:

1. Download and hash-verify the certified source once in a new uniquely named
   testing application/volume, then partition once using eight cores and 128 GiB.
2. Prepare California and Utah for 2025–2027 using eight cores and 64 GiB per
   worker, with at most two workers initially. Actively sample the same metrics.
3. Use those measurements to forecast the remaining 147 state/year preparations
   and the three sequential national reference builds before dispatching them.
   The eventual concurrency limit remains ten, not ten for the initial pilot.
4. Record full-content parity and actual output/storage sizes. Pause again before
   integrating caches and production deployment prerequisites.

An initial **$20 spending budget for the source partition and six pilot jobs**
is proposed, not approved. Stop and seek direction if projected remaining cost
would exceed it. This is not a provider-enforced billing ceiling, and there is
no automatic retry or automatic resource increase.

At [Modal's published rates](https://modal.com/pricing), checked 2026-10-08,
eight cores plus 128 GiB cost approximately $1.400 per billed container-hour;
eight cores plus 64 GiB cost approximately $0.889. These are requested-resource
rate calculations, not measured invoices. Billing includes application load
and billable idle time, and may increase with actual resource usage. Volume
storage is listed at $0.09/GiB/month after included usage; this workspace's
remaining included allocation was not checked. Full-data duration and storage
requirements remain unknown until measured.
