# Microdata Weights Skill

Read this before changing code that aggregates simulation microdata: the
`simulation_output_*.py` builders, `segmented_national_reduce` and
`stage12_runtime/aggregation`, or anything else that reads
`output_dataset.data` or builds a `MicroDataFrame`.

## Weights aren't values

A microdata column is one of four kinds, and each allows different operations
(rule `sim.microdata.weights-not-values`):

| Kind | Examples | Allowed | Never |
| --- | --- | --- | --- |
| Weight | `household_weight`, `person_weight` | A plain (unweighted) sum, to count entities; use as `weights=` | Aggregation as a value through a weighted frame or aggregator |
| Value | `household_net_income`, `household_tax` | Weighted aggregation, with its own entity's weights | Aggregation after the weights are stripped |
| Label | `household_income_decile`, `age`, `is_male`, `race` | Grouping and filtering | Summing or averaging |
| Count | `households`, people per decile | Derived from weights | |

The executor's entity frames are microdf `MicroDataFrame`s weighted by
`<entity>_weight`: policyengine builds them that way, and so do
`segmented_national_reduce._us_dataset_from_frames` and
`stage12_runtime/aggregation._dataset_from_frames` when they rebuild a dataset.
So `.sum()`, `.mean()` and `.count()` on any column are already weighted.

In #766 (fixed by #767), `build_budgetary_impact` passed `"household_weight"`
to `_sum_output_variable`, the helper it uses for ordinary values. That
weighted the weight column by itself, giving the sum of squared weights, and
`budget.households` came out about 10^4 times too high on every run. It
escaped because the builder test used plain `pd.DataFrame`s, which carry no
weights. Count entities with `_count_output_entities` instead.

## Rules

- `sim.microdata.weights-not-aggregated-as-values`: a weight is never the
  variable of a weighted aggregator (`_sum_output_variable`,
  `_change_output_variable`, policyengine's `Aggregate` and `ChangeAggregate`)
  or `<frame>[<weight>]` followed by a weighted reduction (`sum`, `mean`,
  `median`, `quantile`, `std`, `count`, `groupby`, `cumsum`).
- `sim.microdata.weight-stripping-confined`: raw values are read from a
  weighted frame or column (`np.asarray`, `np.array`, `.values`, `.to_numpy()`,
  `.tolist()`, `pd.DataFrame(...)`, `pd.Series(...)`) only in
  `simulation_output_common.py`, which holds `_count_output_entities`. Summing
  stripped values gives an unweighted sample total, the mirror image of #767.
  Within that module, counting entities is the one permanent read; every other
  read is a recorded exception, and the list of exceptions may only shrink.
- `sim.microdata.builder-tests-use-weighted-frames`: the output-builder tests
  (`tests/test_simulation_output_*.py`) build entity frames only through
  `fixtures/weighted_frames.weighted_entity_frame`, which returns a
  `MicroDataFrame` with non-uniform weights, never as a plain `pd.DataFrame`.

The guards live in
`projects/policyengine-simulation-executor/tests/test_microdata_weight_boundaries.py`
and run in the executor's `Test` job.

## How the guard recognises a weight

The guards read the source, so they recognise names, not types:

- a weight is a string or f-string ending in `_weight` or `_weights` (including
  `f"{entity}_weight"`), a name assigned from one, or a `.weights` attribute;
- a weighted frame is `<simulation>.output_dataset.data.<entity>` (or
  `getattr(<simulation>.output_dataset.data, ...)`), `_entity_data(...)`, a
  `MicroDataFrame` or `MicroSeries` built with `weights=`, a name assigned from
  one, and anything indexed, filtered or computed from those.

They don't follow data through function calls or containers. A weight passed
through a helper escapes them, and so do whole-frame readers that iterate
`output_dataset.data.entity_data` (`simulation_microdata`, the Stage 12
simulation worker and the precompute determinism check).

## Handling a finding

Each finding needs a decision, recorded in the PR:

- **Fix it.** Count entities with `_count_output_entities`, and aggregate values
  through the weighted frame or the aggregators.
- **Move it, as a recorded exception.** A strip that can't be fixed yet moves
  into `simulation_output_common.py` as a named helper, with a docstring saying
  why and what would remove it.

Recorded exceptions are listed in
`rules/baselines/sim-microdata-weight-stripping.txt`. That list may only
shrink, so adding one is a deliberate change to the rule's baseline. Today it
is empty: the last one, `_unweighted_household_frame`, went when the UK
constituency and local-authority breakdowns moved to policyengine's
disclosure-controlled geography output, which takes the simulations.
