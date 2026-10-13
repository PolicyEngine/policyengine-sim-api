"""Run a .github/scripts/ shell script with fake commands first on PATH.

Each fake command records its arguments, then runs an optional bash body, so a
test can control what the command prints and returns and assert on every call
the script made, in order.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPOSITORY_ROOT / ".github" / "scripts"
WORKFLOWS_DIR = REPOSITORY_ROOT / ".github" / "workflows"

# Separates the recorded arguments of one call; arguments may contain spaces.
_FIELD = "\x1f"


@dataclass
class FakeCommands:
    """A directory of fake commands that record their calls to one log."""

    directory: Path
    log: Path

    @classmethod
    def create(cls, tmp_path: Path) -> FakeCommands:
        directory = tmp_path / "fake-bin"
        directory.mkdir()
        return cls(directory=directory, log=tmp_path / "fake-calls.log")

    def add(self, name: str, body: str = "") -> None:
        """Install ``name``: record ``name`` and its arguments, then run ``body``."""
        path = self.directory / name
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'printf \'%s{_FIELD}\' {name} "$@" >> "$FAKE_CALLS_LOG"\n'
            "printf '\\n' >> \"$FAKE_CALLS_LOG\"\n" + textwrap.dedent(body),
            encoding="utf-8",
        )
        path.chmod(0o755)

    def calls(self, name: str | None = None) -> list[list[str]]:
        """Return each recorded call as ``[command, *arguments]``.

        Given ``name``, return only that command's calls.
        """
        if not self.log.exists():
            return []
        recorded = [
            line.split(_FIELD)[:-1]
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]
        return [call for call in recorded if name is None or call[0] == name]

    def env(self, **overrides: str) -> dict[str, str]:
        """Return the environment to run a script in, with the fakes first on PATH."""
        return {
            **os.environ,
            "PATH": f"{self.directory}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CALLS_LOG": str(self.log),
            **overrides,
        }


def run_script(
    name: str,
    *args: str,
    env: dict[str, str],
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``.github/scripts/<name>`` directly, as a workflow step does."""
    return subprocess.run(
        [str(SCRIPTS_DIR / name), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=cwd,
    )


def github_output(path: Path) -> dict[str, str]:
    """Return the ``name=value`` pairs a script appended to ``$GITHUB_OUTPUT``."""
    if not path.exists():
        return {}
    pairs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        pairs[key] = value
    return pairs
