"""JSON value types shared by simulation API contracts."""

from __future__ import annotations

from pydantic import JsonValue


type JsonObject = dict[str, JsonValue]
