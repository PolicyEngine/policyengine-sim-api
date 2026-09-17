"""Export the strict Stage 12 worker bundle selected by PolicyEngine.py."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from policyengine_simulation_executor.stage12_bundle import (
    assert_expected_bundle_values,
    load_stage12_bundle,
)


def _expectations(values: list[str]) -> dict[str, str]:
    expectations: dict[str, str] = {}
    for value in values:
        name, separator, expected = value.partition("=")
        if not separator or not name or not expected:
            raise ValueError("--expect values must use non-empty NAME=VALUE syntax")
        if name in expectations:
            raise ValueError(f"Duplicate Stage 12 bundle assertion {name!r}")
        expectations[name] = expected
    return expectations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export the packaged PolicyEngine.py bundle for Stage 12",
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Assert a bundle-derived value without selecting or overriding it",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the normalized bundle document to this explicit path",
    )
    args = parser.parse_args()

    resolved = load_stage12_bundle()
    try:
        expected = _expectations(args.expect)
    except ValueError as error:
        parser.error(str(error))
    assert_expected_bundle_values(resolved.bundle, expected)
    rendered = (
        json.dumps(
            resolved.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"bundle_manifest_sha256={resolved.bundle_manifest_sha256}")


if __name__ == "__main__":
    main()
