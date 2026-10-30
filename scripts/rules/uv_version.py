"""Check that every uv install pins the one uv release in ``.uv-version``.

The repository's uv release is written once, in ``.uv-version`` at the root.
The guard reads every tracked file that can install uv (workflows, composite
actions, shell scripts, Makefiles, Dockerfiles, Compose files and Python
modules that define Modal images) and fails when an install pins no version or
pins another one:

- an ``astral-sh/setup-uv`` step must set ``with: version:``; without it the
  action installs the newest uv release;
- a reference to the uv container image (``ghcr.io/astral-sh/uv`` or
  ``astral/uv``), such as a Dockerfile's ``COPY --from=``, must carry the tag
  ``<version>`` or ``<version>-<variant>``;
- a Modal ``uv_sync`` or ``uv_pip_install`` call must pass
  ``uv_version="<version>"``; without it Modal copies uv from the image's
  ``latest`` tag;
- the uv install script and uv release downloads must name the version in
  their URL;
- ``pip``, ``pipx`` or ``uv tool`` installs of the ``uv`` package must pin
  ``uv==<version>``, and ``uv self update`` must name the version.

A workflow input can't be read from a file, and Modal imports its image
definitions inside containers that don't hold the repository root, so each
install writes the version itself and this guard keeps every copy equal to
``.uv-version``. See docs/engineering/skills/dependencies.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.rules.locked_installs import file_kind, tracked_files
from scripts.rules.model import Violation

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_NAME = "uv-version"
RULE_ID = "sim.dependencies.uv-version-pinned"
VERSION_FILE = ".uv-version"

# The kinds of uv install, each with how to pin it.
SETUP_UV_STEP = "astral-sh/setup-uv step"
UV_IMAGE = "uv container image"
MODAL_UV = "Modal uv install"
DOWNLOAD_URL = "uv download URL"
PACKAGE_INSTALL = "install of the uv package"
SELF_UPDATE = "uv self update"

_HOW_TO_PIN = {
    SETUP_UV_STEP: 'set with: version: "{version}" on the step',
    UV_IMAGE: "use the tag ghcr.io/astral-sh/uv:{version}",
    MODAL_UV: 'pass uv_version="{version}"',
    DOWNLOAD_URL: "name {version} in the URL (https://astral.sh/uv/{version}/install.sh)",
    PACKAGE_INSTALL: "install uv=={version}",
    SELF_UPDATE: "run uv self update {version}",
}

_RELEASE = re.compile(r"\d+\.\d+\.\d+")
_KEY = re.compile(
    r"^(?P<lead>\s*)(?P<dash>-\s+)?(?P<key>[A-Za-z0-9_-]+):\s*(?P<value>.*)$"
)
_LIST_ITEM = re.compile(r"^\s*-\s+")
_SETUP_UV = re.compile(
    r"^(?P<lead>\s*)(?P<dash>-\s+)?uses:\s*[\"']?astral-sh/setup-uv@[^\s\"']+"
)
_FLOW_VERSION = re.compile(r"(?:^|[{,\s])version:\s*(?P<version>[^,}\s]+)")
_COMMENT = re.compile(r"(?:^|\s)#.*$")
# The uv image on GitHub's registry or Docker Hub, with its tag if it has one.
_IMAGE = re.compile(
    r"(?:ghcr\.io/astral-sh/uv|(?<![\w.-])astral/uv)(?![\w-])"
    r"(?::(?P<version>[^\s\"'@,)]+))?"
)
_DOWNLOADS = (
    re.compile(r"astral\.sh/uv/(?:(?P<version>[^/\s\"']+)/)?install\.(?:sh|ps1)"),
    re.compile(
        r"(?:astral-sh|astral\.sh/github)/uv/releases/"
        r"(?:download/(?P<version>[^/\s\"']+)|latest)/"
    ),
)
_PACKAGE_INSTALL = re.compile(
    r"(?<![\w-])(?:pip3?|python3?\s+-m\s+pip|pipx|uv\s+tool|uv\s+pip)\s+install\s+"
    r"(?:[^\s;&|]+\s+)*?uv(?:==(?P<version>[^\s,;&|]+))?(?=[\s,);&|]|$)"
)
_SELF_UPDATE = re.compile(r"\buv\s+self\s+update\b(?:\s+(?P<version>\d[\w.]*))?")
# Modal image methods that install uv, and those whose arguments can hold
# shell commands, image references or package names.
_MODAL_UV_METHODS = ("uv_sync", "uv_pip_install")
_MODAL_TEXT_METHODS = ("run_commands", "dockerfile_commands", "from_registry")
_MODAL_PACKAGE_METHODS = ("pip_install", "uv_pip_install")
_MODAL_CALL = re.compile(
    r"\.("
    + "|".join(
        sorted({*_MODAL_UV_METHODS, *_MODAL_TEXT_METHODS, *_MODAL_PACKAGE_METHODS})
    )
    + r")\("
)
_UV_VERSION_ARGUMENT = re.compile(r"\buv_version\s*=\s*(?P<value>[^,)]+)")
_PACKAGE_ARGUMENT = re.compile(r"(?<![\w.-])uv(?:==(?P<version>[\w.+-]+))?(?![\w.=-])")
_QUOTE_PAIR_LENGTH = 2


@dataclass(frozen=True)
class UvInstall:
    """One place that installs uv, and the version it pins (None when unpinned)."""

    path: str
    line: int
    kind: str
    version: str | None
    # What the report calls the install, when it is more specific than its kind.
    label: str = ""

    @property
    def site(self) -> str:
        """Format as ``path:line: what it is``."""
        return f"{self.path}:{self.line}: {self.label or self.kind}"

    def fix(self, pinned: str) -> str:
        """Say how to make this install pin ``pinned``."""
        how = _HOW_TO_PIN[self.kind].format(version=pinned)
        return f"{how}, the release in {VERSION_FILE}"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= _QUOTE_PAIR_LENGTH and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _opens_step(line: str, key_column: int) -> bool:
    """Whether ``line`` is the ``- `` item whose keys start at ``key_column``."""
    item = _LIST_ITEM.match(line)
    return item is not None and len(item.group(0)) == key_column


def _step_lines(lines: list[str], index: int, key_column: int) -> list[str]:
    """Return the lines of the step whose ``uses:`` key is ``lines[index]``."""
    start = index
    while start > 0 and not _opens_step(lines[start], key_column):
        previous = lines[start - 1]
        if (
            previous.strip()
            and _indent(previous) < key_column
            and not _opens_step(previous, key_column)
        ):
            break
        start -= 1
    end = index + 1
    while end < len(lines) and (
        not lines[end].strip() or _indent(lines[end]) >= key_column
    ):
        end += 1
    return lines[start:end]


def _with_version(step: list[str], key_column: int) -> str | None:
    """Return the step's ``with: version:`` input, or None if it sets none."""
    for position, line in enumerate(step):
        entry = _KEY.match(line)
        if entry is None or entry["key"] != "with":
            continue
        if len(entry["lead"]) + len(entry["dash"] or "") != key_column:
            continue
        if entry["value"].startswith("{"):
            inline = _FLOW_VERSION.search(entry["value"])
            return _unquote(inline["version"]) if inline else None
        for child in step[position + 1 :]:
            if child.strip() and _indent(child) <= key_column:
                break
            argument = _KEY.match(child)
            if argument is not None and argument["key"] == "version":
                return _unquote(argument["value"])
    return None


def setup_uv_steps(path: str, text: str) -> list[UvInstall]:
    """Return each ``astral-sh/setup-uv`` step in a workflow or action file."""
    lines = [_COMMENT.sub("", line).rstrip() for line in text.splitlines()]
    installs = []
    for index, line in enumerate(lines):
        match = _SETUP_UV.match(line)
        if match is None:
            continue
        key_column = len(match["lead"]) + len(match["dash"] or "")
        step = _step_lines(lines, index, key_column)
        installs.append(
            UvInstall(path, index + 1, SETUP_UV_STEP, _with_version(step, key_column))
        )
    return installs


def text_installs(path: str, line_number: int, text: str) -> list[UvInstall]:
    """Return the uv images, downloads and package installs that ``text`` names."""
    unquoted = text.replace('"', " ").replace("'", " ")
    found = [
        (match.start(), UV_IMAGE, match["version"]) for match in _IMAGE.finditer(text)
    ]
    found.extend(
        (match.start(), DOWNLOAD_URL, match["version"])
        for pattern in _DOWNLOADS
        for match in pattern.finditer(text)
    )
    found.extend(
        (match.start(), PACKAGE_INSTALL, match["version"])
        for match in _PACKAGE_INSTALL.finditer(unquoted)
    )
    found.extend(
        (match.start(), SELF_UPDATE, match["version"])
        for match in _SELF_UPDATE.finditer(unquoted)
    )
    return [
        UvInstall(path, line_number, kind, version)
        for _start, kind, version in sorted(found, key=lambda item: item[0])
    ]


def _modal_calls(text: str) -> list[tuple[int, str, str]]:
    """Return each Modal image call as (line, method, argument text)."""
    calls = []
    for match in _MODAL_CALL.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if "#" in text[line_start : match.start()]:
            continue  # The call is written in a comment.
        depth = 0
        end = len(text)
        for index in range(match.end() - 1, len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        line_number = text.count("\n", 0, match.start()) + 1
        calls.append((line_number, match.group(1), text[match.end() : end]))
    return calls


def modal_installs(path: str, text: str) -> list[UvInstall]:
    """Return the uv installs in a Python module's Modal image calls."""
    installs = []
    for line_number, method, arguments in _modal_calls(text):
        if method in _MODAL_UV_METHODS:
            argument = _UV_VERSION_ARGUMENT.search(arguments)
            version = _unquote(argument["value"]) if argument else None
            label = f"Modal {method} call"
            installs.append(UvInstall(path, line_number, MODAL_UV, version, label))
        if method in _MODAL_TEXT_METHODS:
            installs.extend(text_installs(path, line_number, arguments))
        if method in _MODAL_PACKAGE_METHODS:
            without_options = _UV_VERSION_ARGUMENT.sub("", arguments)
            unquoted = without_options.replace('"', " ").replace("'", " ")
            installs.extend(
                UvInstall(path, line_number, PACKAGE_INSTALL, package["version"])
                for package in _PACKAGE_ARGUMENT.finditer(unquoted)
            )
    return installs


def scan(root: Path, files: Iterable[str] | None = None) -> list[UvInstall]:
    """Return every uv install in the install-capable files under ``root``."""
    installs = []
    for path in sorted(tracked_files(root) if files is None else files):
        kind = file_kind(path)
        if kind is None:
            continue
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        if kind == "python":
            installs.extend(modal_installs(path, text))
            continue
        if kind in {"workflow", "action"}:
            installs.extend(setup_uv_steps(path, text))
        for number, line in enumerate(text.splitlines(), start=1):
            code = _COMMENT.sub("", line)
            if code.strip():
                installs.extend(text_installs(path, number, code))
    return installs


def read_pinned_version(root: Path) -> tuple[str | None, list[Violation]]:
    """Return the release in ``.uv-version``, or why it holds no single release."""
    source = root / VERSION_FILE
    if not source.is_file():
        return None, [
            Violation(
                RULE_ID,
                GUARD_NAME,
                f"{VERSION_FILE} is missing",
                f"create {VERSION_FILE} holding the one uv release, such as 0.13.6",
            )
        ]
    values = [
        line.strip()
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(values) != 1 or not _RELEASE.fullmatch(values[0]):
        return None, [
            Violation(
                RULE_ID,
                GUARD_NAME,
                f"{VERSION_FILE} does not hold exactly one uv release number",
                f"write one release such as 0.13.6 in {VERSION_FILE}, without "
                "a range or a moving name such as latest",
            )
        ]
    return values[0], []


def _problem(install: UvInstall, pinned: str | None) -> str | None:
    """Say why ``install`` doesn't pin ``pinned``, or return None if it does."""
    version = install.version
    if version is None:
        return f"{install.site} does not pin a uv release"
    if not _RELEASE.match(version):
        return f"{install.site} does not pin a uv release (it names {version})"
    # An image tag may add a variant to the release, as in 0.13.6-python3.13-bookworm.
    image_variant = install.kind == UV_IMAGE and version.startswith(f"{pinned}-")
    if pinned is None or version == pinned or image_variant:
        return None
    return f"{install.site} pins uv {version}, not {pinned}"


def compare(installs: list[UvInstall], pinned: str | None) -> list[Violation]:
    """Report each install that pins no release, or not the pinned one."""
    violations = []
    for install in installs:
        problem = _problem(install, pinned)
        if problem is not None:
            fix = install.fix(pinned or "<version>")
            violations.append(Violation(RULE_ID, GUARD_NAME, problem, fix))
    return violations


def run_check(root: Path = REPO_ROOT) -> list[Violation]:
    """Return every uv install under ``root`` that doesn't pin ``.uv-version``."""
    pinned, violations = read_pinned_version(root)
    return [*violations, *compare(scan(root), pinned)]


def check() -> list[str]:
    """Adapter for scripts/run_quality_guards.py."""
    return [str(violation) for violation in run_check(REPO_ROOT)]
