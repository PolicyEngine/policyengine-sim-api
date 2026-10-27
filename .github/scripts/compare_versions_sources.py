"""Compare the old gateway's ``/versions`` answers with the v2 manifest's.

The Simulation Entrypoint forwards ``GET /versions`` and
``GET /versions/{kind}`` to the old gateway, which answers from v1's routing
state. It serves the answers the v2 version manifest gives at
``GET /internal/v2/versions`` and ``GET /internal/v2/versions/{kind}``. A
staging deploy runs this script against its candidate revision and records
every difference between the two in the job summary, before ``/versions``
stops forwarding.

The report is for information only. The script exits 0 whatever it finds,
including when a route cannot be read, so it never fails a deploy. It exits 2
only when it is run without a base URL.

Application names are not compared directly: the old gateway names each
version's v1 application (``policyengine-simulation-py…``) and the manifest
its v2 application (``policyengine-simulation-v2-py…``). Each route is
compared instead by the PolicyEngine version whose ``policyengine`` route, in
the same answer, names the same application.

Usage:
  compare_versions_sources.py BASE_URL

Environment:
  GITHUB_STEP_SUMMARY  optional; a file the report is appended to (set by the
                       GitHub Actions runner)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Callable

KINDS = ("policyengine", "us", "uk")
# A kind neither source supports; both must reject it the same way.
UNKNOWN_KIND = "unknown-kind"
LATEST_KEY = "latest"
HTTP_OK = 200
EXPECTED_ARGUMENT_COUNT = 2
USAGE_ERROR = 2
REQUEST_TIMEOUT_SECONDS = 30
MAX_CELL_LENGTH = 160


class _Absent:
    """Marks a key that one answer has and the other does not."""

    def __repr__(self) -> str:
        return "(absent)"


ABSENT = _Absent()


@dataclass(frozen=True)
class Answer:
    """One route's HTTP status and JSON body, or why there is none."""

    status: int | None
    body: object = None
    error: str | None = None

    def describe(self) -> str:
        """Return the status, or the error that prevented one."""
        if self.status is None:
            return f"no answer ({self.error})"
        return f"HTTP {self.status}"


@dataclass(frozen=True)
class Difference:
    """One place where the old gateway's answer and the manifest's differ."""

    where: str
    key: str
    gateway: object
    manifest: object


type Fetch = Callable[[str], Answer]


def fetch(url: str) -> Answer:
    """GET ``url`` and return its status and parsed JSON body."""
    request = Request(url, headers={"Accept": "application/json"})  # noqa: S310  # the URL is the deploy's own candidate revision, from its argument
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310  # the request built above
            status, content = response.status, response.read()
    except HTTPError as error:
        status, content = error.code, error.read()
    except OSError as error:
        return Answer(status=None, error=type(error).__name__)
    try:
        body = json.loads(content)
    except ValueError:
        body = None
    return Answer(status=status, body=body)


def _version_sort_key(key: str) -> tuple[bool, tuple[int, ...], str]:
    parts = key.split(".")
    if all(part.isascii() and part.isdigit() for part in parts):
        return (True, tuple(int(part) for part in parts), key)
    return (False, (), key)


def application_versions(answer: object) -> dict[str, str]:
    """Map each application to the PolicyEngine version whose route names it."""
    routes = answer.get("policyengine") if isinstance(answer, dict) else None
    if not isinstance(routes, dict):
        return {}
    versions: dict[str, str] = {}
    for version, application in sorted(
        routes.items(), key=lambda entry: _version_sort_key(str(entry[0]))
    ):
        if version != LATEST_KEY and isinstance(application, str):
            versions.setdefault(application, str(version))
    return versions


def _served_version(application: object, versions: dict[str, str]) -> str:
    if isinstance(application, str) and application in versions:
        return f"serves policyengine {versions[application]}"
    return f"application with no policyengine route: {application!r}"


def compare_routes(
    where: str,
    gateway_routes: object,
    manifest_routes: object,
    *,
    gateway_versions: dict[str, str],
    manifest_versions: dict[str, str],
) -> list[Difference]:
    """Compare two route maps by the PolicyEngine version each route serves."""

    def normalized(routes: object, versions: dict[str, str]) -> dict[str, object]:
        if not isinstance(routes, dict):
            return {}
        return {
            str(key): value if key == LATEST_KEY else _served_version(value, versions)
            for key, value in routes.items()
        }

    gateway = normalized(gateway_routes, gateway_versions)
    manifest = normalized(manifest_routes, manifest_versions)
    return [
        Difference(where, key, gateway.get(key, ABSENT), manifest.get(key, ABSENT))
        for key in sorted(set(gateway) | set(manifest), key=_version_sort_key)
        if gateway.get(key, ABSENT) != manifest.get(key, ABSENT)
    ]


def compare_values(
    where: str, path: tuple[str, ...], gateway: object, manifest: object
) -> list[Difference]:
    """Compare two JSON values, descending into objects key by key."""
    if isinstance(gateway, dict) and isinstance(manifest, dict):
        differences: list[Difference] = []
        for key in sorted(set(gateway) | set(manifest), key=_version_sort_key):
            differences.extend(
                compare_values(
                    where,
                    (*path, str(key)),
                    gateway.get(key, ABSENT),
                    manifest.get(key, ABSENT),
                )
            )
        return differences
    if gateway == manifest:
        return []
    return [Difference(where, ".".join(path), gateway, manifest)]


def compare_whole_answers(gateway: dict, manifest: dict) -> list[Difference]:
    """Compare two ``GET /versions`` answers field by field."""
    gateway_versions = application_versions(gateway)
    manifest_versions = application_versions(manifest)
    differences: list[Difference] = []
    for field in sorted(set(gateway) | set(manifest)):
        where = f"GET /versions: {field}"
        if field in KINDS:
            differences.extend(
                compare_routes(
                    where,
                    gateway.get(field, ABSENT),
                    manifest.get(field, ABSENT),
                    gateway_versions=gateway_versions,
                    manifest_versions=manifest_versions,
                )
            )
        else:
            differences.extend(
                compare_values(
                    where, (), gateway.get(field, ABSENT), manifest.get(field, ABSENT)
                )
            )
    return differences


def _status_difference(where: str, gateway: Answer, manifest: Answer) -> Difference:
    return Difference(where, "status", gateway.describe(), manifest.describe())


def compare_sources(base_url: str, fetch_answer: Fetch = fetch) -> list[Difference]:
    """Fetch both sources' answers from ``base_url`` and list their differences."""
    differences: list[Difference] = []
    gateway_whole = fetch_answer(f"{base_url}/versions")
    manifest_whole = fetch_answer(f"{base_url}/internal/v2/versions")
    if (
        gateway_whole.status == manifest_whole.status == HTTP_OK
        and isinstance(gateway_whole.body, dict)
        and isinstance(manifest_whole.body, dict)
    ):
        differences.extend(
            compare_whole_answers(gateway_whole.body, manifest_whole.body)
        )
    else:
        differences.append(
            _status_difference("GET /versions", gateway_whole, manifest_whole)
        )

    kind_answers = {
        kind: (
            fetch_answer(f"{base_url}/versions/{kind}"),
            fetch_answer(f"{base_url}/internal/v2/versions/{kind}"),
        )
        for kind in KINDS
    }
    # Each source's own policyengine routes name the version each of its
    # applications serves, even when its whole answer is unavailable.
    gateway_policyengine, manifest_policyengine = kind_answers["policyengine"]
    gateway_versions = application_versions({"policyengine": gateway_policyengine.body})
    manifest_versions = application_versions(
        {"policyengine": manifest_policyengine.body}
    )
    for kind, (gateway_kind, manifest_kind) in kind_answers.items():
        where = f"GET /versions/{kind}"
        if gateway_kind.status == manifest_kind.status == HTTP_OK:
            differences.extend(
                compare_routes(
                    where,
                    gateway_kind.body,
                    manifest_kind.body,
                    gateway_versions=gateway_versions,
                    manifest_versions=manifest_versions,
                )
            )
        else:
            differences.append(_status_difference(where, gateway_kind, manifest_kind))

    where = f"GET /versions/{UNKNOWN_KIND}"
    gateway_unknown = fetch_answer(f"{base_url}/versions/{UNKNOWN_KIND}")
    manifest_unknown = fetch_answer(f"{base_url}/internal/v2/versions/{UNKNOWN_KIND}")
    if (
        gateway_unknown.status != manifest_unknown.status
        or gateway_unknown.status is None
    ):
        # Two routes that both gave no answer were not compared, so they are
        # reported too.
        differences.append(_status_difference(where, gateway_unknown, manifest_unknown))
    else:
        differences.extend(
            compare_values(
                where, ("body",), gateway_unknown.body, manifest_unknown.body
            )
        )
    return differences


def _cell(value: object) -> str:
    if value is ABSENT:
        text = repr(ABSENT)
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True)
    if len(text) > MAX_CELL_LENGTH:
        text = text[: MAX_CELL_LENGTH - 1] + "…"
    return text.replace("|", "\\|").replace("\n", " ")


_REPORT_HEADING = "## `/versions`: the old gateway compared with the v2 manifest"
_REPORT_NOTE = (
    "This comparison is for information only and does not fail the deploy. "
    "Routes are compared by the PolicyEngine version each application serves, "
    "because the old gateway names v1 applications and the manifest names v2 "
    "applications."
)


def render_report(base_url: str, differences: list[Difference]) -> str:
    """Return the Markdown report of ``differences`` found at ``base_url``."""
    lines = [
        _REPORT_HEADING,
        "",
        f"Compared at `{base_url}`. {_REPORT_NOTE}",
        "",
    ]
    if not differences:
        lines.append("**The answers match.**")
        return "\n".join(lines) + "\n"
    count = len(differences)
    lines.extend(
        [
            f"**{count} difference{'s' if count != 1 else ''}.**",
            "",
            "| Where | Key | Old gateway | v2 manifest |",
            "| --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| {_cell(difference.where)} | {_cell(difference.key)} | "
        f"{_cell(difference.gateway)} | {_cell(difference.manifest)} |"
        for difference in differences
    )
    return "\n".join(lines) + "\n"


def render_failure(base_url: str, error: BaseException) -> str:
    """Return the Markdown report for a comparison that could not finish."""
    return (
        f"{_REPORT_HEADING}\n\nCompared at `{base_url}`. {_REPORT_NOTE}\n\n"
        f"**The comparison could not finish** ({type(error).__name__}).\n"
    )


def write_summary(report: str) -> None:
    """Append ``report`` to the job summary, when the runner names one."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as summary:  # noqa: PTH123  # the runner names this file by path
            summary.write(report)
    except OSError as error:
        print(f"Could not write the job summary: {type(error).__name__}")


def main(argv: list[str]) -> int:
    """Compare the sources at the base URL in ``argv`` and report; return 0."""
    if len(argv) != EXPECTED_ARGUMENT_COUNT or not argv[1].strip():
        print("Usage: compare_versions_sources.py BASE_URL", file=sys.stderr)
        return USAGE_ERROR
    base_url = argv[1].strip().rstrip("/")
    try:
        report = render_report(base_url, compare_sources(base_url))
    # The report is for information only and must never fail the deploy.
    except Exception as error:  # noqa: BLE001  # any failure is reported, not raised
        report = render_failure(base_url, error)
    print(report)
    write_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
