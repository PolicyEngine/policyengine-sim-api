# Dependency Locking

Read this before adding or changing anything that installs Python packages or
runs tests: a workflow step, a `.github/scripts/` or `scripts/` script, a
Makefile target, a Dockerfile, a Compose file or a Modal image definition.

Every project directory (`projects/*`, `libs/*`) is its own uv project with its
own `uv.lock`, and so is `.github/towncrier`, which only locks the Towncrier the
release runs to build `CHANGELOG.md` (`build-changelog.sh` runs
`uv run --frozen --project .github/towncrier towncrier build`). The repository
root has no project. Four rules keep every
environment on the locked versions. The `locked-installs` quality guard
(`scripts/rules/locked_installs.py`) checks all four on every pull request. A
fifth rule, [checked by the `uv-version` guard](#we-pin-one-uv-version), makes
every install use the same uv release.

## We use uv.lock

`sim.dependencies.lock-committed`: every directory with a `pyproject.toml`
commits a `uv.lock` that matches it. `.github/scripts/check-uv-locks.sh` runs
`uv lock --check --project <dir>` for every tracked `pyproject.toml`, so a new
project is checked without editing the workflow. It runs in the PR
`test-integration` job, after the API clients are generated, because
`policyengine-apis-integ` depends on the generated client by path.

After editing a `pyproject.toml`, run `uv lock` in that directory and commit
the `uv.lock`.

## We install from uv.lock

`sim.dependencies.install-from-lock`: every install installs exactly the
locked versions:

- `uv sync --frozen` (or `--locked`);
- `uv run --frozen` (or `--locked`), without `--with` or `--no-project`;
- in a Modal image, `uv_sync(..., frozen=True)`.

The guard reports any other installer: `pip install`, `uv pip`, `uv sync` or
`uv run` without `--frozen`, `uv run --with`, `uvx`, `uv tool`, `pipx`,
`uv add`, `poetry install`, `conda install`, and the Modal methods
`pip_install*`, `uv_pip_install` and `run_commands` that run one of these.
`uv lock` is not an install. Modal's `add_local_python_source` and
`add_local_dir` copy this repository's source files into the image and install
no packages, so they are not reported.

## We test from uv.lock

`sim.dependencies.test-from-lock`: every test command (`pytest`,
`python -m pytest`, `coverage run`, `python -m unittest`) runs through
`uv run --frozen`, in the project's locked environment.

## We relock automatically

`sim.dependencies.scheduled-relock`: `.github/workflows/update-dependencies.yml`
runs every day. Its `make update` step runs `uv lock --upgrade` in every
`libs/*` and `projects/*` directory with a `pyproject.toml` and in
`.github/towncrier`, and its scripts open or update the dependency-update pull
request, with its changelog fragment. The guard checks this
configuration statically: a `schedule:` workflow whose job, through the
scripts and Make targets it runs, relocks with upgrades, covers every project
directory and runs `gh pr create`. It does not check that the scheduled runs
succeed.

## What the guard reads

Every tracked workflow (its `run:` blocks), composite action, shell script,
Makefile, Dockerfile (its `RUN` instructions), Compose file, and Python module
outside tests (for Modal image calls). Before matching, the guard:

- skips comment lines;
- joins backslash continuations;
- reads each `&&`, `||`, `;`, `|` or backtick segment as its own command;
- removes every single and double quote, so a `match` is written without the
  file's quotes (`pip install shellcheck-py==0.11.0.1` for
  `pip install "shellcheck-py==0.11.0.1"`);
- skips commands whose first program only prints or edits text: `echo`,
  `printf`, `sed`, `grep` and `awk`, after any `NAME=`/`NAME :=` assignment,
  `$(` or `$(shell`, and the shell words `if`, `then`, `do`, `else` and `!`.
`TestRepository.test_every_install_capable_file_is_scanned` fails if a file of
one of these kinds is not scanned. Installs that Python code starts through
`subprocess` are not read.

## Adding an exception

Only where installing from the lock is not possible. Add a
`[[rule.exception]]` to the rule in `rules/registry.toml`:

```toml
[[rule.exception]]
path = ".github/workflows/example.yml"   # the file
match = "pip install example"            # a substring of the one command or call
reason = "Why this site cannot install from uv.lock."
removal = "The condition under which the exception is removed."
# or, instead of removal: expires = 2026-12-31 and issue = <tracking issue>
```

A tool installed outside any project and pinned to an exact version
(`uv tool install ruff==0.15.22`, `pip install "shellcheck-py==0.11.0.1"`,
`uv tool run ruff@$(RUFF_VERSION)`) may use `category = "pinned-tool"` instead
of a removal condition. The guard checks that `match` contains the pin:
`name==version` or `name@version`, where the version is a release number or a
variable that holds one, never a moving name such as `latest`.

The guard fails when an exception has no `match`, has neither a removal
condition nor both a review date (`expires`) and a tracking issue (`issue`), or
matches no site (a stale exception). The
current exceptions are listed in `docs/generated/rules.md` under "Exceptions
and review dates".

## We pin one uv version

`sim.dependencies.uv-version-pinned`: every place that installs uv installs the
one release written in `.uv-version` at the repository root, so lock checks,
installs, the daily relock and the images all run the same uv, whichever
release is newest that day:

- each `astral-sh/setup-uv` step sets `with: version: "<release>"`; without it
  the action installs the newest release;
- each Dockerfile copies uv from `ghcr.io/astral-sh/uv:<release>`, never
  `:latest`; a tag may add a variant, as in `<release>-python3.13-bookworm`;
- each Modal `uv_sync` or `uv_pip_install` call passes
  `uv_version="<release>"`; without it Modal copies uv from the image's
  `latest` tag;
- a uv install script or release download names the release in its URL
  (`https://astral.sh/uv/<release>/install.sh`), a `pip`, `pipx` or `uv tool`
  install of uv pins `uv==<release>`, and `uv self update` names the release.

A workflow's `with:` input can't be read from a file, and Modal imports its
image definitions inside containers that don't hold the repository root, so
each install writes the release itself. The `uv-version` quality guard
(`scripts/rules/uv_version.py`) keeps every copy equal to `.uv-version`. It
reads every workflow, composite action, shell script, Makefile, Dockerfile and
Compose file, skipping comment lines, and the Modal image calls in Python
modules outside tests and fixtures, and fails when an install pins no release
or another one.

Use the same release on your own machine, so that `uv lock` writes what CI
checks; the guard can't check the uv you run locally.

To upgrade uv:

1. Change the release in `.uv-version`.
2. Run `python scripts/run_quality_guards.py`. The `uv-version` guard lists
   every install that still pins the old release; change each one.
3. Run `.github/scripts/check-uv-locks.sh` with the new uv. If a lock fails,
   relock that project with the new uv and commit the lock in the same pull
   request.

Changing `uv_version` changes the Modal images' `uv_sync` layer, so Modal
rebuilds each image from that layer on the next time it is built.
