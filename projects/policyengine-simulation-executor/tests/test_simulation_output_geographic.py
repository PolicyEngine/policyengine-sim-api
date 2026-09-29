"""Unit tests for required UK geography metadata handling."""

from __future__ import annotations

from contextlib import nullcontext

import pytest
from policyengine.data.uk_geography_assets import CONSTITUENCY_ASSET_SPEC

from policyengine_simulation_executor import simulation_output_geographic


def _complete_constituency_record() -> dict[str, object]:
    return {
        "constituency_code": "E14001063",
        "constituency_name": "Aldershot",
        "x": 56,
        "y": -40,
        "average_household_income_change": -150.95,
        "relative_household_income_change": -0.0026,
        "population": 40_530.65,
    }


def test_required_uk_lookup_uses_gcp_credentials(monkeypatch) -> None:
    observed: list[str] = []

    def credentials():
        observed.append("credentials-entered")
        return nullcontext()

    def resolve(spec, **kwargs):
        assert spec is CONSTITUENCY_ASSET_SPEC
        assert kwargs == {"download_missing_assets": True}
        observed.append("lookup-resolved")
        return "/tmp/constituencies_2024.csv"

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.setup_gcp_credentials",
        credentials,
    )
    monkeypatch.setattr(
        "policyengine.outputs.uk_geography_impact."
        "resolve_uk_geography_lookup_csv_path",
        resolve,
    )

    result = simulation_output_geographic._required_uk_geography_lookup_csv_path(
        CONSTITUENCY_ASSET_SPEC
    )

    assert result == "/tmp/constituencies_2024.csv"
    assert observed == ["credentials-entered", "lookup-resolved"]


def test_required_uk_lookup_rejects_missing_asset(monkeypatch) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.setup_gcp_credentials",
        nullcontext,
    )
    monkeypatch.setattr(
        "policyengine.outputs.uk_geography_impact."
        "resolve_uk_geography_lookup_csv_path",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(FileNotFoundError, match="constituency lookup CSV"):
        simulation_output_geographic._required_uk_geography_lookup_csv_path(
            CONSTITUENCY_ASSET_SPEC
        )


def test_complete_uk_geography_output_accepts_lookup_metadata() -> None:
    result = simulation_output_geographic._complete_uk_geography_output(
        [_complete_constituency_record()],
        code_field="constituency_code",
        name_field="constituency_name",
    )

    record = result.root[0].model_dump(mode="python")
    assert record["constituency_name"] == "Aldershot"
    assert record["x"] == 56
    assert record["y"] == -40


def test_complete_uk_geography_output_rejects_empty_result() -> None:
    with pytest.raises(ValueError, match="did not contain result records"):
        simulation_output_geographic._complete_uk_geography_output(
            [],
            code_field="constituency_code",
            name_field="constituency_name",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("constituency_name", "E14001063", "missing a lookup name"),
        ("constituency_name", "", "missing a lookup name"),
        ("x", None, "missing lookup coordinates"),
        ("y", None, "missing lookup coordinates"),
    ],
)
def test_complete_uk_geography_output_rejects_fallback_metadata(
    field: str,
    value: object,
    message: str,
) -> None:
    record = _complete_constituency_record()
    record[field] = value

    with pytest.raises(ValueError, match=message):
        simulation_output_geographic._complete_uk_geography_output(
            [record],
            code_field="constituency_code",
            name_field="constituency_name",
        )
