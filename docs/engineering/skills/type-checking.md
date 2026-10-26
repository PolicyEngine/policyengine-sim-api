# Type Checking

Read this before changing a package's Pyright settings, its Pyright baseline or
the pinned Pyright version, when the Pyright check fails, or when adding a
package.

## Rules

Every uv package is type-checked with Pyright on every pull request, by the
`pyright` job in `.github/workflows/pr.yml`:

- `sim.quality.pyright-libraries-and-entry`, `sim.quality.pyright-gateway`,
  `sim.quality.pyright-executor` and `sim.quality.pyright-apis-integ`: the
  contract, observability, Stage 12 persistence and fastapi libraries, the
  simulation entry, the gateway, the executor and the generated-client
  integration tests have no Pyright errors.
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

What stops a baseline growing is the rules guard, which compares each existing
rule's status and baseline count with the base branch. It fails when the count
doesn't match the file, when the count is higher than on the base branch, and
when an enforced rule becomes ratcheted without a `[rule.downgrade] reason`.
It doesn't check:

- what the reason says: any non-empty reason passes;
- which entries changed: a change that fixes one error and adds another keeps
  the count and passes;
- which packages a rule lists: moving a package from an enforced rule into a
  new ratcheted rule passes without a reason, because a new rule has no status
  on the base branch to compare with.

Review is what catches these. `.github/CODEOWNERS` assigns `rules/` to the core
developers, but `sim.process.rule-machinery-code-owned` is doc-only: merging
doesn't require their approval. Whoever reviews a pull request that changes a
baseline, a rule's status or a rule's `enforcer.projects` checks it.

When a person decides to ratchet a package's rule, `--write-baseline` creates
its baseline: it refuses an error an existing baseline doesn't list, but
records every current error when the file doesn't exist yet.

Once a baseline is empty, delete it and make the rule `enforced`.

## Fixing errors

- Prefer narrowing a type (an `isinstance` check, an early return) or
  correcting an annotation to `cast`.
- Suppress an error only as a last resort, with the rule and the reason on the
  same line: `# pyright: ignore[reportAttributeAccessIssue]  # <reason>`.
- If an error shows a real bug, first write a test that demonstrates it, then
  fix it.
- Open observability spans and operations with `scopes.span(runtime, ...)` and
  `scopes.operation(runtime, ...)` from
  `policyengine_simulation_observability.scopes`, not `runtime.span(...)` or
  `runtime.operation(...)`. policyengine-observability types its scope
  managers as possibly suppressing exceptions, so after `with runtime.span(...)`
  Pyright reports each variable the block assigned as possibly unbound.
- Before reading a simulation's output frames, narrow
  `<simulation>.output_dataset.data` with the `EntityFrames` protocol in
  `policyengine_simulation_executor.output_frames`, and keep reading through
  that expression: the microdata weight checks recognise weighted frames by it.
