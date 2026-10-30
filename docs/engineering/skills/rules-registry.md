# Rules Registry

Read this before adding, changing, relaxing or removing a rule, a guard test, a
checker configuration (ruff, mypy, Pyright, Import Linter), or a CI job that
enforces one.

Every rule that governs this repository lives in `rules/registry.toml`. The
registry says what each rule requires, how mechanically it is enforced, which
CI job runs it, and whether its checker has been proven to reject a bad example.
`python -m scripts.rules render` turns it into `docs/generated/rules.md` and the
Rules block in `AGENTS.md`; the `rules-registry` quality guard fails CI when the
registry, the checkers and the rendered views disagree.

## Commands

```bash
python -m scripts.rules validate                    # schema and file checks only
python -m scripts.rules render                      # rewrite rules.md and the AGENTS.md block
python -m scripts.rules guard --base-ref origin/<default-branch>   # everything CI checks
python -m unittest discover -s scripts/rules/tests -t .            # the tool's own tests
```

All commands use only the standard library, so they run without installing the
project. When a rule has a `[rule.downgrade]` section, `guard` also runs
`gh api` to look up its tracking issue; see [Baselines](#baselines).

## Adding or changing a rule

A PR that adds, tightens or relaxes a rule contains all of these:

1. **The check**: the checker, config or test change.
2. **A registry entry** (fields below).
3. **Proof, appropriate to the tier**: see "Proof".
4. **Zero violations, or a baseline** where the baseline policy allows one.
5. **CI wiring**: a `[[rule.ci]]` reference to the job that runs the check.
6. **Doc update**: explanatory skills docs cite the rule id instead of
   restating the rule as unchecked prose.
7. **Regenerated outputs**: run `python -m scripts.rules render` and commit
   `docs/generated/rules.md` and `AGENTS.md`.

If a rule exists only in prose, register it with `status = "doc-only"` so the
gap is visible, rather than leaving it out.

## Fields

| Field | Meaning |
| --- | --- |
| `id` | Lowercase dotted id, e.g. `api.v2.services-no-sql`. Never reuse an id. |
| `category` | One of the `[[category]]` ids; controls grouping in the rendered views. |
| `statement` | One plain-English sentence an agent can follow. |
| `rationale` | Path (optionally `#anchor`) to the doc that explains the rule. |
| `scope` | Globs for the files the rule constrains; each must match a file. |
| `tier` | How mechanically the rule can be checked (see "Tiers"). |
| `status` | `enforced`, `ratcheted`, `advisory` or `doc-only`. |
| `autofix` | Whether the enforcer can fix violations itself (e.g. ruff format). |
| `review_by` | Optional date; the guard fails once it passes. |
| `[rule.enforcer]` | What checks the rule; `kind` plus kind-specific fields. |
| `[[rule.ci]]` | `workflow`, `job`, and an `invocation` string that must appear in that job's `run:` text (or its reusable-workflow `uses:`). |
| `[rule.proof]` | Optional; see "Proof". |
| `[rule.baseline]` | Only for `ratcheted` rules: `file` listing tolerated violations, and their `count`. |
| `[[rule.exception]]` | Temporary carve-out: `path`, `reason`, `expires`. |
| `[rule.downgrade]` | `reason`, and `issue`: the number of an open issue in the registry's `issue_repository` that tracks restoring the rule. Not allowed on an enforced rule. Required when a rule moves from enforced/ratcheted to advisory/doc-only, or from enforced to ratcheted; on a new rule that starts ratcheted with a baseline count above 0; and on a rule that now lists a package from a stronger rule on the base branch. |

`blocking` is derived, never declared: a rule blocks merges when its status is
`enforced` or `ratcheted` and its tier is not `judgment`.

### Enforcer kinds

| Kind | Fields | Resolved by the guard |
| --- | --- | --- |
| `pytest` | `nodes` (`path::test` or `path::Class::test`), optional `project` (sub-project directory the paths are relative to) | Each test exists, and the CI job's command collects it |
| `quality-guard` | `name`, optional `runner` | The name is in `GUARDS` in `scripts/run_quality_guards.py` |
| `ruff` | `mode` = `format`, `lint` (with `codes`) or `per-file-ignore` (with `pattern`, `codes`) | Codes are selected / the ignore entry exists and covers only [files allowed file-level exceptions](#file-level-exceptions) |
| `mypy` | `paths` | Each path is in `[tool.mypy].files` |
| `workflow-job` | optional `step` | The CI job exists (the job itself is the check) |
| `pyright` | `projects`: the package directories it checks | Each package exists, is listed by no other Pyright rule, has no `pyrightconfig.json`, lists what Pyright checks in its `pyproject.toml` `[tool.pyright] include`, and is in the CI job's matrix |
| `import-linter` | `config`, `contract` | The config file exists |
| `docs-guard` | `doc`, `marker` | The doc exists |
| `llm-review` | `prompt`, `model` | The prompt exists; judgment tier only |
| `none` | | Nothing; only for `doc-only` and `advisory` rules |

## Tiers

| Tier | Covers | Can block? | Proof |
| --- | --- | --- | --- |
| `mechanical` | A pattern is present or not | Yes | A bad fixture is rejected and a good fixture is accepted |
| `metric` | A number crosses a threshold | Yes | Fixtures just below and just above the threshold |
| `judgment` | Needs reading comprehension (LLM review plus human) | No | A labelled eval set with measured precision |

Judgment rules graduate: when the same judgment finding recurs, turn it into a
mechanical rule.

## Proof

A proof shows the checker actually rejects what the rule forbids. Put fixtures
under `rules/fixtures/<rule-id>/` and add a test named `test_rule_proof_<name>`
that runs the rule's checker on them.

| Tier | Proof fields |
| --- | --- |
| `mechanical` | `bad_fixture`, `good_fixture`, `test` (+ optional `project`) |
| `metric` | `below_fixture`, `above_fixture`, `threshold`, `test` |
| `judgment` | `eval_set`, `min_precision`, `measured_precision`, `measured_on` |

The proof test asserts that the bad fixture is rejected and the good one
accepted. The guard doesn't run it; it checks that the fixtures exist and that a
CI job collects the proof test:

- For a `pytest`-enforced rule, the proof test must be collected by the rule's
  own `[[rule.ci]]` job (for example, under the paths `make test` runs).
- For any other enforcer, name the job that runs the proof test with
  `proof.workflow`, `proof.job` and `proof.invocation` (all three or none).

Where possible, make the bad fixture a real past mistake from git history.
Blocking rules without proof are listed as "unproven" in `rules.md`; they don't
fail the guard yet.

## Baselines

- **Ruff is never ratcheted.** `ruff.toml` selects `ALL`, so every ruff rule
  applies except the four listed in `ignore` (two formatter conflicts and one
  of each mutually exclusive docstring pair) and the registered
  [file-level exceptions](#file-level-exceptions). When a ruff upgrade adds
  rules, the upgrade PR fixes every new violation. Don't use `--add-noqa`.
  Outside the files that allow file-level exceptions, a justified exception is
  an inline `# noqa: CODE  # reason`.
- Other enforcers (mypy, Pyright, Import Linter, custom guards) may start
  `ratcheted` with a baseline file of tolerated violations. A new rule that
  starts ratcheted with a count above 0 needs a `[rule.downgrade]` section,
  as a downgrade does. Baselines may only shrink: the guard compares counts
  against the base branch.
- Moving a rule from enforced/ratcheted to advisory/doc-only, or from enforced
  to ratcheted, needs a `[rule.downgrade]` section with a `reason` and an
  `issue`.
- A `[rule.downgrade]` section's `issue` is an open issue in
  `issue_repository`, set at the top of the registry, that tracks restoring
  the rule. The guard looks up every section's issue on every run, with
  `gh api` and the token in `GH_TOKEN` or `GITHUB_TOKEN`. On GitHub Actions, a
  missing token or a failed lookup fails it, and so does an
  `issue_repository` other than the repository the workflow runs in
  (`GITHUB_REPOSITORY`), so a pull request can't name an issue elsewhere.
  Elsewhere, without a token, it prints a note for each issue it didn't
  check. Closing the issue while the section is still in the registry fails
  the guard on every pull request, and an enforced rule can't have the
  section, so the pull request that restores the rule removes it. The guard
  accepts any non-empty reason and any open issue; whether the issue is about
  this rule, and whether a section was removed from a rule that stays
  weakened, is for the reviewer.
- A package in an enforced or ratcheted rule's `enforcer.projects` on the base
  branch (only `pyright` enforcers list packages) stays, while its
  `pyproject.toml` exists, in a rule of the same kind that is at least as
  strong: enforced for a package from an enforced rule, enforced or
  ratcheted for one from a ratcheted rule. If it moves to a weaker rule, that
  rule needs a `[rule.downgrade]` section. Deleting the package, or moving it
  to a rule at least as strong, passes this check; a new ratcheted rule with a
  count above 0 still needs the section.
- The guard compares rules by id. Removing a rule, or replacing it with a
  weaker rule under a new id, produces only the notes "rule ... was removed"
  and "rule ... is new", except that the checks above still cover Pyright
  packages and new ratcheted rules. Review catches the rest.
- **Weakening a rule is a decision for a person, not an agent.** Agents don't
  downgrade a rule, add a `[rule.downgrade]` section, create a baseline or add
  entries to one, or move a package or path out of a rule's scope. When a rule
  reports violations an agent can't fix in its pull request, the agent stops
  and reports them, with what it tried. Fixing violations and shrinking a
  baseline is fine.

## File-level exceptions

A ruff `per-file-ignores` entry switches rules off for whole files. It is
allowed only where that's the norm rather than an exception to it:

- **tests**: files under a `tests` or `authenticated_tests` directory;
- **CI/CD scripts**: files under `.github`;
- **command-line scripts**: files under a `scripts` directory, or modules that
  run as a program through `if __name__ == "__main__":`.

Everywhere else, keep a finding only with an inline suppression and its reason.

Each entry needs a `per-file-ignore` rule (see below). The guard expands the
entry's pattern like ruff does and fails when the pattern:

- covers any other file;
- is negated;
- matches nothing.

`scripts/rules/file_exceptions.py` holds these definitions.

## Hidden rules

The guard fails when something enforces a rule that the registry doesn't
describe:

- a `GUARDS` entry in `scripts/run_quality_guards.py`;
- a ruff format check in CI, an explicitly selected ruff code, or a ruff
  per-file ignore;
- a `[tool.mypy].files` entry;
- a test matching `[discovery].guard_test_globs` or listed in
  `extra_guard_tests`;
- a job in a `[discovery].workflows` file.

Register it, or list it under `discovery.ignore_jobs` / `discovery.ignore_tests`
with a reason.

## The tool in other repositories

policyengine-api has its own copy of `scripts/rules/`; this one started from it.
The copies may diverge: each repository owns its copy. Port a change by hand
when it's useful in both.
