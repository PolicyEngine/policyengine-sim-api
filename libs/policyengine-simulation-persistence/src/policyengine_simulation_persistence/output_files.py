"""Links to the files a run stored, each belonging to one run.

A simulation run may store its microdata and a report run its comparison
files in the artifact bucket, which deletes them after 30 days. A link is
offered for 30 days after its file was written; the result rows stay for
good.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    insert,
    select,
)

from policyengine_simulation_persistence.runs import report_runs, simulation_runs
from policyengine_simulation_persistence.schema import simulation_metadata

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import Connection

OUTPUT_FILE_RETENTION = timedelta(days=30)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
URI_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://\S+")
ROLE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")

output_files = Table(
    "output_files",
    simulation_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("simulation_run_id", Uuid(as_uuid=True), nullable=True),
    Column("report_run_id", Uuid(as_uuid=True), nullable=True),
    Column("role", Text, nullable=False),
    Column("uri", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("written_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "(simulation_run_id IS NULL) <> (report_run_id IS NULL)",
        name="one_run",
    ),
    CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
    CheckConstraint("uri ~ '^[a-z][a-z0-9+.-]*://\\S+$'", name="uri"),
    CheckConstraint("role ~ '^[a-z][a-z0-9_]{0,63}$'", name="role"),
    UniqueConstraint("simulation_run_id", "uri"),
    UniqueConstraint("report_run_id", "uri"),
    ForeignKeyConstraint(
        ["simulation_run_id"],
        [simulation_runs.c.id],
        name="fk_output_files_simulation_run",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["report_run_id"],
        [report_runs.c.id],
        name="fk_output_files_report_run",
        ondelete="RESTRICT",
    ),
)


@dataclass(frozen=True)
class OutputFile:
    """A stored file: what it holds (its role), where it is and its SHA-256."""

    role: str
    uri: str
    sha256: str

    def __post_init__(self) -> None:
        """Reject a malformed link without touching the database."""
        for field_name, pattern in (
            ("role", ROLE_PATTERN),
            ("uri", URI_PATTERN),
            ("sha256", SHA256_PATTERN),
        ):
            if not pattern.fullmatch(getattr(self, field_name)):
                msg = f"{field_name} is malformed"
                raise ValueError(msg)


@dataclass(frozen=True)
class OutputFileRecord:
    """One ``simulation.output_files`` row."""

    id: UUID
    simulation_run_id: UUID | None
    report_run_id: UUID | None
    role: str
    uri: str
    sha256: str
    written_at: datetime

    def as_output_file(self) -> OutputFile:
        """Return the link without its row's identity."""
        return OutputFile(role=self.role, uri=self.uri, sha256=self.sha256)


def _owner(
    simulation_run_id: UUID | None, report_run_id: UUID | None
) -> dict[str, UUID | None]:
    if (simulation_run_id is None) == (report_run_id is None):
        msg = "a file belongs to exactly one run: a simulation run or a report run"
        raise ValueError(msg)
    return {"simulation_run_id": simulation_run_id, "report_run_id": report_run_id}


def record_output_files(
    connection: Connection,
    files: Sequence[OutputFile],
    *,
    written_at: datetime,
    simulation_run_id: UUID | None = None,
    report_run_id: UUID | None = None,
) -> None:
    """Record links to files one run stored, in the caller's transaction."""
    owner = _owner(simulation_run_id, report_run_id)
    if files:
        connection.execute(
            insert(output_files),
            [
                {
                    "id": uuid4(),
                    **owner,
                    "role": file.role,
                    "uri": file.uri,
                    "sha256": file.sha256,
                    "written_at": written_at,
                }
                for file in files
            ],
        )


def available_output_files(
    connection: Connection,
    *,
    now: datetime,
    simulation_run_id: UUID | None = None,
    report_run_id: UUID | None = None,
) -> list[OutputFileRecord]:
    """Return one run's files written less than 30 days before ``now``."""
    owner = _owner(simulation_run_id, report_run_id)
    owner_column = (
        output_files.c.simulation_run_id
        if owner["simulation_run_id"] is not None
        else output_files.c.report_run_id
    )
    owner_id = owner["simulation_run_id"] or owner["report_run_id"]
    rows = connection.execute(
        select(output_files)
        .where(
            owner_column == owner_id,
            output_files.c.written_at > now - OUTPUT_FILE_RETENTION,
        )
        .order_by(output_files.c.written_at, output_files.c.role, output_files.c.uri)
    ).mappings()
    return [OutputFileRecord(**row) for row in rows]
