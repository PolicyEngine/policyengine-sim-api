# File Length

Read this before splitting a long Python module, or when the `file-length`
quality guard fails.

## The rule

Rule `sim.quality.python-file-length`. A tracked Python file has at most 800
lines. Test code has at most 1,500: a file under a `tests` or `fixtures`
directory, or a `conftest.py` anywhere. The `file-length` guard in
`scripts/run_quality_guards.py` counts every physical line, blank lines,
comments and docstrings included, of each Python file that `git ls-files`
lists.

Ruff has no file-length rule (it doesn't implement pylint's `C0302`), and the
complexity, branch, statement and argument rules each measure one function, so
a module can keep growing while every function in it passes. A long module
usually holds several responsibilities, and is hard to review.

## The baseline

While the rule is ratcheted, `rules/baselines/python-file-length.txt` lists the
files that were already over their limit when the rule was added. The guard
fails on a file over its limit that the baseline doesn't list, and on a listed
file that is now within its limit or no longer tracked. The pull request that
splits a listed file therefore removes its entry and lowers the rule's baseline
`count`.

Never add an entry, or move a file out of the rule's scope: that weakens the
rule, which only a person may decide (see
[the registry's baselines](rules-registry.md#baselines)). Split the file
instead.

## Splitting a module

- **Group by responsibility.** Each new module holds one concern, such as
  request models, storage, or the command-line interface, and is named after
  it. Aim for about 400 lines; never split a file at an arbitrary point to
  reach a number.
- **Move code without changing behaviour.** Logic, messages, outputs and the
  order of operations stay the same.
- **Keep every import path working.** Code elsewhere, tests and workflows
  (`python -m ...`) import names from the original module, so it imports and
  re-exports them. List the re-exported names in `__all__`, so ruff knows the
  imports are used.
- **Don't import a private name across modules.** When a moved name with a
  leading underscore is needed in another module, rename it public.
- **Patch the module that looks the name up.** `monkeypatch.setattr` and
  `mock.patch` replace a name in one module only. After a move, a patch that
  still targets the original module no longer reaches the moved code, and the
  test keeps running with the real dependency. Target the module whose code
  reads the name now.
- **Keep tests intact.** A split test file keeps every test's name and body,
  apart from imports, and the number of tests stays the same. Shared helpers
  move to a support module next to the tests that use them.
