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
"""  # noqa: INP001  # a script run as python -m src.modal.utils.release_sentinel; utils is not a regular package

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
from importlib import metadata
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
    if status != 200 or not token:
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


def check_deployment(
    base_url: str,
    expected: Mapping[str, Any],
    *,
    token: str | None = None,
    timeout_seconds: float = 1800,
    poll_seconds: float = 10,
    http: Callable[..., tuple[int, Mapping[str, str], Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Submit each expected case to ``base_url`` and return what disagrees."""
    if expected.get("schema") != SCHEMA:
        msg = f"expected file schema is {expected.get('schema')!r}"
        raise SentinelError(msg)
    http = http or _http
    base_url = base_url.rstrip("/")
    version = expected["policyengine_version"]
    problems: list[str] = []
    for case in expected["cases"]:
        label = case["case_id"]
        body = {**case["request"], "policyengine_version": version}
        status, _, submitted = http(
            "POST", f"{base_url}/simulate/economy/comparison", token=token, body=body
        )
        if status != 200 or not isinstance(submitted, Mapping):
            problems.append(f"{label}: submit returned HTTP {status}: {submitted}")
            continue
        served = submitted.get("policyengine_bundle") or {}
        want = expected[case["request"]["country"]]
        for field, wanted in (
            ("policyengine_version", version),
            ("model_version", want["model_version"]),
            ("dataset_sha256", want["dataset_sha256"]),
        ):
            if served.get(field) != wanted:
                problems.append(
                    f"{label}: served {field} {served.get(field)!r}, "
                    f"expected {wanted!r}"
                )
        deadline = clock() + timeout_seconds
        result = None
        completed = False
        while True:
            status, headers, job = http(
                "GET", f"{base_url}/jobs/{submitted['job_id']}", token=token
            )
            if status == 200 and isinstance(job, Mapping):
                if job.get("status") != "complete":
                    problems.append(f"{label}: job ended {job.get('status')!r}")
                else:
                    completed = True
                result = job.get("result")
                break
            if status not in (202, 429, 503):
                problems.append(f"{label}: job returned HTTP {status}: {job}")
                break
            if clock() >= deadline:
                problems.append(f"{label}: no result within {timeout_seconds:.0f}s")
                break
            sleep(_retry_after(headers, poll_seconds))
        if not isinstance(result, Mapping):
            # A complete job must carry an object result; anything else
            # (missing, null, a list or a string) compares nothing.
            if completed:
                problems.append(
                    f"{label}: complete job returned a {type(result).__name__} "
                    "result, not an object"
                )
            continue
        for path, wanted in case["values"].items():
            try:
                actual = value_at(result, path)
            except SentinelError as exc:
                problems.append(f"{label}: {exc}")
                continue
            if not agree(wanted, actual):
                problems.append(
                    f"{label}: {path} served {actual!r}, computed {wanted!r}"
                )
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
        with open(args.output, "w") as handle:
            json.dump(expected, handle, indent=2, sort_keys=True)
        for case in expected["cases"]:
            _report(f"{case['case_id']} ({case['seconds']}s): {case['values']}")
        return 0

    with open(args.expected) as handle:
        expected = json.load(handle)
    problems = check_deployment(
        args.base_url,
        expected,
        token=access_token(),
        timeout_seconds=args.timeout_seconds,
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
