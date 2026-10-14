r"""Numerical release sentinel: the deployed API computes what the bundle computes.

``expected`` runs each sentinel request in this process through
``run_simulation_impl``, the pure implementation the Modal simulation worker
runs, with the packages this project locks (the bundle being deployed), and
writes the values. ``check`` submits the same requests to a deployed
entrypoint pinned to that policyengine.py version, waits for the jobs and
compares: the served bundle identity must be the deployed one and every value
must agree within a tolerance that allows only floating-point reordering.

UK national requests are not segmented, so the served worker runs this
implementation on the payload the gateway forwards, which is what
``worker_payload`` builds. A difference therefore means the deployed stack is
not serving the bundle that was built and tested: wrong app, wrong data,
stale cache or a broken route.

Usage::

    python -m src.modal.utils.release_sentinel expected --output sentinel.json
    python -m src.modal.utils.release_sentinel check --base-url URL \
        --expected sentinel.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from importlib import metadata
from pathlib import Path
from typing import Any

SCHEMA = "policyengine-sim-api.release-sentinel/v1"

#: Relative and absolute tolerance. The same code on the same data differs
#: only by floating-point reduction order across CPUs.
REL_TOL = 1e-9
ABS_TOL = 0.01

#: The requests the sentinel runs and the result values it compares. The
#: reform takes effect on 1 January so it changes the 2026 result under every
#: released convention for dated UK reforms (about GBP 6.7bn on the 6.2.3
#: bundle); a value that is zero on both sides would prove nothing.
SENTINEL_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "uk_basic_rate_21p_2026",
        "request": {
            "country": "uk",
            "scope": "macro",
            "time_period": "2026",
            "reform": {
                "gov.hmrc.income_tax.rates.uk[0].rate": {"2026-01-01.2100-12-31": 0.21}
            },
        },
        "values": (
            "budget.budgetary_impact",
            "budget.tax_revenue_impact",
            "budget.benefit_spending_impact",
            "budget.baseline_net_income",
        ),
    },
)

#: Job poll statuses that mean the job has not finished yet: poll again.
_PENDING_JOB_STATUSES = frozenset(
    {HTTPStatus.ACCEPTED, HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.SERVICE_UNAVAILABLE}
)

_AUTH_VARS = (
    "SIMULATION_TEST_AUTH_ISSUER",
    "SIMULATION_TEST_AUTH_AUDIENCE",
    "SIMULATION_TEST_AUTH_CLIENT_ID",
    "SIMULATION_TEST_AUTH_CLIENT_SECRET",
)


class SentinelError(RuntimeError):
    """The deployed stack did not reproduce the bundle's numbers."""


def worker_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build the payload the gateway forwards to the worker for ``request``."""
    from policyengine_simulation_contract.gateway_models import (  # noqa: PLC0415  # the contract models load only when a payload is built
        SimulationRequest,
    )

    return SimulationRequest.model_validate(dict(request)).model_dump(
        exclude={"version", "policyengine_version", "telemetry"},
        mode="json",
        exclude_none=True,
    )


def value_at(result: Mapping[str, Any], path: str) -> float:
    """Return the finite number at the dotted ``path`` in ``result``."""
    node: Any = result
    for key in path.split("."):
        if not isinstance(node, Mapping) or key not in node:
            msg = f"the result has no {path}"
            raise SentinelError(msg)
        node = node[key]
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        msg = f"{path} is {node!r}, not a number"
        raise SentinelError(msg)
    if not math.isfinite(node):
        msg = f"{path} is {node}, not a finite number"
        raise SentinelError(msg)
    return float(node)


def agree(expected: float, actual: float) -> bool:
    """Equal up to floating-point reduction order (symmetric)."""
    return abs(expected - actual) <= max(
        ABS_TOL, REL_TOL * max(abs(expected), abs(actual))
    )


def _bundle_identity() -> dict[str, Any]:
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # resolved at call time, as the tests' module patches rely on
        get_country_release_bundle,
    )

    uk = get_country_release_bundle("uk")
    return {
        "policyengine_version": metadata.version("policyengine"),
        "uk": {
            "model_version": uk.model_version,
            "dataset": uk.default_dataset,
            "dataset_sha256": uk.default_dataset_sha256,
        },
    }


def _local_runner() -> Callable[[dict[str, Any]], Mapping[str, Any]]:
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # loads the policyengine runtime only for the expected command; the tests patch it
        run_simulation_impl,
    )
    from policyengine_simulation_executor.stage12_qualification import (  # noqa: PLC0415  # loads the policyengine runtime only for the expected command; the tests patch it
        _local_qualification_runtime,
    )

    runtime = _local_qualification_runtime()
    return lambda payload: run_simulation_impl(payload, runtime=runtime)


def compute_expected(
    *,
    runner: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    identity: Mapping[str, Any] | None = None,
    cases: tuple[dict[str, Any], ...] = SENTINEL_CASES,
) -> dict[str, Any]:
    """Run every case in this process and record its values."""
    runner = runner or _local_runner()
    identity = dict(identity or _bundle_identity())
    if not identity["uk"].get("dataset_sha256"):
        msg = "the installed bundle records no UK dataset SHA-256"
        raise SentinelError(msg)
    recorded = []
    for case in cases:
        started = time.monotonic()
        result = runner(worker_payload(case["request"]))
        recorded.append(
            {
                "case_id": case["case_id"],
                "request": case["request"],
                "values": {path: value_at(result, path) for path in case["values"]},
                "seconds": round(time.monotonic() - started, 1),
            }
        )
    return {"schema": SCHEMA, **identity, "cases": recorded}


# --- HTTP --------------------------------------------------------------------


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _http(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Mapping[str, Any] | None = None,
    timeout: float = 60,
) -> tuple[int, Mapping[str, str], Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        status, headers, raw = error.code, error.headers, error.read()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode("utf-8", "replace")
    return status, dict(headers or {}), parsed


def access_token(environ: Mapping[str, str] = os.environ) -> str | None:
    """Return a client-credentials token when the test auth variables are configured."""
    present = [name for name in _AUTH_VARS if environ.get(name)]
    if not present:
        if _truthy(environ.get("SIMULATION_TEST_AUTH_REQUIRED")):
            msg = "SIMULATION_TEST_AUTH_REQUIRED is set but no auth vars"
            raise SentinelError(msg)
        return None
    if len(present) != len(_AUTH_VARS):
        missing = sorted(set(_AUTH_VARS) - set(present))
        msg = f"partial auth configuration; missing {missing}"
        raise SentinelError(msg)
    status, _, payload = _http(
        "POST",
        environ["SIMULATION_TEST_AUTH_ISSUER"].rstrip("/") + "/oauth/token",
        body={
            "client_id": environ["SIMULATION_TEST_AUTH_CLIENT_ID"],
            "client_secret": environ["SIMULATION_TEST_AUTH_CLIENT_SECRET"],
            "audience": environ["SIMULATION_TEST_AUTH_AUDIENCE"],
            "grant_type": "client_credentials",
        },
    )
    token = payload.get("access_token") if isinstance(payload, Mapping) else None
    if status != HTTPStatus.OK or not token:
        msg = f"token request failed with HTTP {status}"
        raise SentinelError(msg)
    return str(token)


def _retry_after(headers: Mapping[str, str], fallback: float) -> float:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(0.0, float(value))
            except ValueError:
                return fallback
    return fallback


HttpCall = Callable[..., tuple[int, Mapping[str, str], Any]]


@dataclass(frozen=True)
class JobPolling:
    """How long to wait for each job, and the clock and sleep that measure it.

    ``poll_seconds`` is the wait between polls when the API sends no usable
    ``Retry-After`` header.
    """

    timeout_seconds: float = 1800
    poll_seconds: float = 10
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


@dataclass
class _JobOutcome:
    """What polling one job found: its problems, its result and whether it completed."""

    problems: list[str] = field(default_factory=list)
    result: object = None
    completed: bool = False


@dataclass(frozen=True)
class _Deployment:
    """The deployment under check: its URL, how to call it and how to poll it."""

    base_url: str
    token: str | None
    http: HttpCall
    polling: JobPolling

    def check_case(
        self, case: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> list[str]:
        """Submit one case and return its identity, job and value problems."""
        label = case["case_id"]
        version = expected["policyengine_version"]
        body = {**case["request"], "policyengine_version": version}
        status, _, submitted = self.http(
            "POST",
            f"{self.base_url}/simulate/economy/comparison",
            token=self.token,
            body=body,
        )
        if status != HTTPStatus.OK or not isinstance(submitted, Mapping):
            return [f"{label}: submit returned HTTP {status}: {submitted}"]
        problems = _identity_problems(
            label,
            submitted.get("policyengine_bundle") or {},
            version=version,
            country_identity=expected[case["request"]["country"]],
        )
        job = self._await_job(label, submitted["job_id"])
        problems += job.problems
        if not isinstance(job.result, Mapping):
            # A complete job must carry an object result; anything else
            # (missing, null, a list or a string) compares nothing.
            if job.completed:
                problems.append(
                    f"{label}: complete job returned a "
                    f"{type(job.result).__name__} result, not an object"
                )
            return problems
        return problems + _value_problems(label, case["values"], job.result)

    def _await_job(self, label: str, job_id: str) -> _JobOutcome:
        """Poll the job until it finishes, fails or runs out of time."""
        deadline = self.polling.clock() + self.polling.timeout_seconds
        while True:
            status, headers, job = self.http(
                "GET", f"{self.base_url}/jobs/{job_id}", token=self.token
            )
            if status == HTTPStatus.OK and isinstance(job, Mapping):
                return _finished_job(label, job)
            if status not in _PENDING_JOB_STATUSES:
                return _JobOutcome([f"{label}: job returned HTTP {status}: {job}"])
            if self.polling.clock() >= deadline:
                return _JobOutcome(
                    [f"{label}: no result within {self.polling.timeout_seconds:.0f}s"]
                )
            self.polling.sleep(_retry_after(headers, self.polling.poll_seconds))


def _finished_job(label: str, job: Mapping[str, Any]) -> _JobOutcome:
    """Describe a finished job; one that did not complete is a problem."""
    completed = job.get("status") == "complete"
    problems = [] if completed else [f"{label}: job ended {job.get('status')!r}"]
    return _JobOutcome(problems, job.get("result"), completed)


def _identity_problems(
    label: str,
    served: Mapping[str, Any],
    *,
    version: str,
    country_identity: Mapping[str, Any],
) -> list[str]:
    """Compare the served wrapper version, country model and dataset digest."""
    return [
        f"{label}: served {name} {served.get(name)!r}, expected {wanted!r}"
        for name, wanted in (
            ("policyengine_version", version),
            ("model_version", country_identity["model_version"]),
            ("dataset_sha256", country_identity["dataset_sha256"]),
        )
        if served.get(name) != wanted
    ]


def _value_problems(
    label: str, values: Mapping[str, float], result: Mapping[str, Any]
) -> list[str]:
    """Compare each computed value with the one the deployment served."""
    problems = []
    for path, wanted in values.items():
        try:
            actual = value_at(result, path)
        except SentinelError as exc:
            problems.append(f"{label}: {exc}")
            continue
        if not agree(wanted, actual):
            problems.append(f"{label}: {path} served {actual!r}, computed {wanted!r}")
    return problems


def check_deployment(
    base_url: str,
    expected: Mapping[str, Any],
    *,
    token: str | None = None,
    http: HttpCall | None = None,
    polling: JobPolling | None = None,
) -> list[str]:
    """Submit each expected case to ``base_url`` and return what disagrees."""
    if expected.get("schema") != SCHEMA:
        msg = f"expected file schema is {expected.get('schema')!r}"
        raise SentinelError(msg)
    deployment = _Deployment(
        base_url=base_url.rstrip("/"),
        token=token,
        http=http or _http,
        polling=polling or JobPolling(),
    )
    problems: list[str] = []
    for case in expected["cases"]:
        problems += deployment.check_case(case, expected)
    return problems


def _report(line: str, *, error: bool = False) -> None:
    """Print one line of the command's report, to stderr for a failure."""
    print(line, file=sys.stderr if error else sys.stdout)


def main(argv: list[str] | None = None) -> int:
    """Run the ``expected`` or ``check`` command and return its exit status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    expected_parser = commands.add_parser("expected", help="compute in-process values")
    expected_parser.add_argument("--output", required=True)
    check_parser = commands.add_parser("check", help="compare a deployment")
    check_parser.add_argument("--base-url", required=True)
    check_parser.add_argument("--expected", required=True)
    check_parser.add_argument("--timeout-seconds", type=float, default=1800)
    args = parser.parse_args(argv)

    if args.command == "expected":
        expected = compute_expected()
        with Path(args.output).open("w") as handle:
            json.dump(expected, handle, indent=2, sort_keys=True)
        for case in expected["cases"]:
            _report(f"{case['case_id']} ({case['seconds']}s): {case['values']}")
        return 0

    with Path(args.expected).open() as handle:
        expected = json.load(handle)
    problems = check_deployment(
        args.base_url,
        expected,
        token=access_token(),
        polling=JobPolling(timeout_seconds=args.timeout_seconds),
    )
    if problems:
        _report("The deployed API does not reproduce the bundle's numbers:", error=True)
        for problem in problems:
            _report(f"  {problem}", error=True)
        return 1
    _report(
        f"{args.base_url} reproduces {len(expected['cases'])} sentinel case(s) for "
        f"policyengine {expected['policyengine_version']}, UK dataset "
        f"{expected['uk']['dataset_sha256']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
