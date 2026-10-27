# Type Checking

Read this before changing a package's Pyright settings, its Pyright baseline or
the pinned Pyright version, when the Pyright check fails, or when adding a
package.

## Rules

Every uv package is type-checked with Pyright on every pull request, by the
`pyright` job in `.github/workflows/pr.yml`:

- `sim.quality.pyright-libraries-and-entry`, `sim.quality.pyright-gateway`,
  `sim.quality.pyright-executor` and `sim.quality.pyright-apis-integ`: the
  contract, observability, simulation persistence, Stage 12 persistence and
  fastapi libraries, the simulation entry, the gateway, the executor and the
  generated-client integration tests have no Pyright errors.
- `sim.quality.every-package-type-checked`: a Pyright rule lists every uv
  package under `libs/` and `projects/`. When you add a package, give it a
  `[tool.pyright] include`, add it to a Pyright rule's `enforcer.projects`, and
  add it to the `pyright` job's matrix.

## Running the check

From the repository root:

```bash
make check                                   # CI's ruff, then Pyright in every package
python -m scripts.rules pyright --sync projects/policyengine-simulation-gateway
python -m scripts.rules pyright --list-packages
```

`--sync` first runs `uv sync --frozen --all-extras` in the package; leave it
out to check the environment as it is. apis-integ installs the generated API
client, so run `./scripts/generate-clients.sh` before checking it (`make check`
does).

The check:

- runs one pinned Pyright version, `PYRIGHT_VERSION` in
  `scripts/rules/pyright.py`, through `uv tool run`;
- points it at the interpreter of the package's locked environment
  (`<package>/.venv/bin/python`), so imports resolve against the package's own
  dependencies;
- checks the paths in the package's `[tool.pyright] include`. Without an
  `include`, Pyright would check the whole directory, tests included. The rules
  guard fails when a package a Pyright rule lists has none, has a
  `pyrightconfig.json` (which Pyright would read instead), is listed by another
  Pyright rule, or is missing from the `pyright` job's matrix.

Other `[tool.pyright]` settings can weaken the check without failing anything,
for example `ignore`, a lower `typeCheckingMode` or a rule set to `"none"`.
`pyproject.toml` files aren't code-owned, so review a change to them as you
would a rule change.

### Why one pinned version

Each package's `build` extra also installs Pyright, for editors, and its lock
file pin can differ from package to package (1.1.409 to 1.1.414 when the check
was added). CI ignores those pins:

- Pyright's messages change between releases. For the same executor error,
  1.1.409 prints `Series | Unknown | DataFrame` and 1.1.414 prints
  `Series | Unknown | Self@DataFrame`, and baseline entries quote messages.
- The daily dependency update relocks every package with `--upgrade`, so the
  locked versions move whenever Pyright releases. A dependency update shouldn't
  change type-check results, just as a ruff release can't (ruff is pinned in
  `pr.yml`).

To upgrade Pyright, change `PYRIGHT_VERSION` in its own pull request, and fix
the errors the new version reports.

## Baselines

No package has a Pyright baseline now: every Pyright rule is enforced.

### When you can't fix Pyright errors

Weakening a Pyright rule is a decision for a person, not an agent. If Pyright
reports errors you can't fix in your pull request, including after a Pyright
upgrade or in a new package, stop and report them: list the errors and say
what you tried. Don't:

- change a Pyright rule's `status` from `enforced`, or add a
  `[rule.downgrade]` section;
- create a file under `rules/baselines/`, or add entries to one;
- move a package out of an enforced rule's `enforcer.projects`, or into a new
  rule;
- weaken a package's `[tool.pyright]` settings (see above).

Fixing a ratcheted package's errors and shrinking its baseline is fine.

### How a baseline works

A ratcheted rule's baseline, `rules/baselines/pyright-<package>.txt`, lists the
errors the package may still have, one line per error, sorted:

```text
<path> :: <Pyright rule> :: <first line of the message>
```

Entries carry no line numbers, so editing elsewhere in a file doesn't change
them. The check fails on an error the baseline doesn't list, and on an entry
for an error that no longer occurs, so each fix removes its entry in the same
commit and the baseline only shrinks.

After fixing errors:

1. run `python -m scripts.rules pyright <package> --write-baseline` (without a
   package, it does every ratcheted package). It rewrites the baseline as the
   errors that occur now, and refuses when one of them isn't already listed;
2. set the rule's `[rule.baseline] count` in `rules/registry.toml` to the
   number of entries left.

What stops a baseline growing is the rules guard, which compares the registry
with the base branch. It fails when:

- a rule's baseline count doesn't match its file, or is higher than on the
  base branch;
- an enforced rule becomes ratcheted without a `[rule.downgrade]` section,
  which gives a `reason` and an `issue`, the number of the issue that tracks
  restoring the rule;
- a rule that the base branch doesn't have starts ratcheted with a baseline
  count above 0, without a `[rule.downgrade]` section;
- a package that an enforced or ratcheted Pyright rule lists on the base
  branch, and whose `pyproject.toml` still exists, is listed by no Pyright
  rule, or moves to a weaker one (from enforced to any other status, or from
  ratcheted to advisory), unless the rule that lists it now has a
  `[rule.downgrade]` section. Deleting the package, or moving it to a rule at
  least as strong, passes this check; a new ratcheted rule with a count above
  0 still needs the section, as above;
- a `[rule.downgrade]` section's `issue` isn't an open issue in this
  repository. The guard looks up every section's issue on every run, with
  `gh api` and the token in `GH_TOKEN` or `GITHUB_TOKEN`. In CI, a missing
  token or a failed lookup fails it, and so does an `issue_repository` at the
  top of the registry that isn't the repository the workflow runs in. Locally,
  without a token, it prints a note for each issue it didn't check; to check
  them, run it with `GH_TOKEN="$(gh auth token)"`.

It doesn't check:

- what the reason says, or whether the issue is about restoring this rule: any
  non-empty reason and any open issue pass;
- which entries changed: a change that fixes one error and adds another keeps
  the count and passes;
- whether a `[rule.downgrade]` section is new: a section already on the base
  branch also covers a further weakening of its rule, such as another package
  moving into it;
- whether a rule that stays weakened keeps its `[rule.downgrade]` section:
  removing the section from a ratcheted or advisory rule passes, and its
  tracking issue can then be closed without failing anything.

Review is what catches these. `.github/CODEOWNERS` assigns `rules/` to the core
developers, but `sim.process.rule-machinery-code-owned` is doc-only: merging
doesn't require their approval. Whoever reviews a pull request that changes a
baseline, a rule's status, a rule's `enforcer.projects` or a
`[rule.downgrade]` section checks it.

When a person decides to ratchet a package's rule, they add the
`[rule.downgrade]` section, and `--write-baseline` creates its baseline: it
refuses an error an existing baseline doesn't list, but records every current
error when the file doesn't exist yet.

Once a baseline is empty, delete it, make the rule `enforced` and remove its
`[rule.downgrade]` section, which an enforced rule can't have. That pull
request can then close the tracking issue. Closing the issue while the
section is still in the registry fails the guard on every pull request.

## Fixing errors

- Prefer narrowing a type (an `isinstance` check, an early return) or
  correcting an annotation to `cast`.
- Suppress an error only as a last resort, with the rule and the reason on the
  same line: `# pyright: ignore[reportAttributeAccessIssue]  # <reason>`.
- If an error shows a real bug, first write a test that demonstrates it, then
  fix it.
- Open observability spans and operations with `runtime.span(...)` and
  `runtime.operation(...)`. policyengine-observability 3.0.4 types them
  correctly: their `__exit__` and `__aexit__` return `Literal[False]`, so
  Pyright knows they never suppress an exception and doesn't report a variable
  the block assigns as possibly unbound after it. Every package that depends
  on policyengine-observability requires 3.0.4 or later, and
  `libs/policyengine-simulation-observability/tests/test_scope_exit_typing.py`
  fails if an older release is installed.
- Before reading a simulation's output frames, narrow
  `<simulation>.output_dataset.data` with the `EntityFrames` protocol in
  `policyengine_simulation_executor.output_frames`, and keep reading through
  that expression: the microdata weight checks recognise weighted frames by it.
