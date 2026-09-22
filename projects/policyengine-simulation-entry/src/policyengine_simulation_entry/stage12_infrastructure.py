"""Validate the shared API v2 runtime credential and Stage 12 schema."""

from __future__ import annotations

import argparse
from pathlib import Path

from policyengine_stage12_persistence import (
    Stage12RuntimeAccessError,
    create_stage12_engine,
    validate_runtime_database,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--environment", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    database_url = args.database_url_file.read_text(encoding="utf-8").strip()
    try:
        validate_runtime_database(
            create_stage12_engine(database_url),
            expected_role=args.expected_role,
            environment=args.environment,
        )
    except (Stage12RuntimeAccessError, ValueError) as error:
        raise SystemExit(str(error)) from None
    print(
        "Shared API v2 runtime database access verified for Stage 12: "
        f"environment={args.environment}, role={args.expected_role}"
    )


if __name__ == "__main__":
    main()
