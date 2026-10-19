"""Per-state blocks of a segmented US national run (issue #829).

The fixture is a small three-state national microdata set split over two
region-group children, as the segmented run splits it. A state's block must
equal the output that a separate row-filtered run for that state builds from
the same rows and weights.
"""

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from policyengine.core.scoping_strategy import RowFilterStrategy
from policyengine_simulation_executor import national_region_outputs as nro
from policyengine_simulation_executor import segmented_national_reduce as reduce_mod
from policyengine_simulation_executor import simulation_runtime as sr
from policyengine_simulation_executor.segmented_national_reduce import (
    PrecomputedSimulation,
    build_national_output,
)
from policyengine_simulation_executor.simulation_microdata import (
    extract_output_microdata,
)
from policyengine_simulation_executor.simulation_output_builder import (
    SimulationOutputBuilder,
)

YEAR = 2026
# state code -> (FIPS, households, congressional districts)
STATES = {"state/hi": (15, 30, 2), "state/ia": (19, 40, 4), "state/ca": (6, 60, 5)}
GROUPS = (("state/hi", "state/ia"), ("state/ca",))
GROUP_ENTITIES = ("marital_unit", "family", "spm_unit", "tax_unit", "household")


def _national_frames(*, reform: bool):
    """One person per household and one of each group unit per household."""
    rng = np.random.default_rng(829)
    fips, districts = [], []
    for state_fips, households, district_count in STATES.values():
        fips += [state_fips] * households
        districts += [
            state_fips * 100 + 1 + i % district_count for i in range(households)
        ]
    count = len(fips)
    ids = np.arange(1, count + 1, dtype=np.int64)
    market = rng.uniform(5_000, 250_000, count).astype(np.float32)
    tax = (market * np.float32(0.2)).astype(np.float32)
    benefits = rng.uniform(0, 8_000, count).astype(np.float32)
    if reform:
        benefits = (benefits + np.float32(1_000) * (ids % 3 == 0)).astype(np.float32)
    household = pd.DataFrame(
        {
            "household_id": ids,
            "household_weight": rng.uniform(50, 500, count).astype(np.float32),
            "household_count_people": np.ones(count, dtype=np.float32),
            "household_market_income": market,
            "household_tax": tax,
            "household_state_income_tax": (tax * np.float32(0.1)).astype(np.float32),
            "household_benefits": benefits,
            "household_net_income": (market - tax + benefits).astype(np.float32),
            "congressional_district_geoid": np.array(districts, dtype=np.int64),
        }
    )
    frames = {"household": household}
    person = pd.DataFrame(
        {
            "person_id": ids,
            "person_weight": household["household_weight"].to_numpy(),
            **{f"{entity}_id": ids for entity in GROUP_ENTITIES},
        }
    )
    frames["person"] = person
    for entity in GROUP_ENTITIES[:-1]:
        frames[entity] = pd.DataFrame(
            {
                f"{entity}_id": ids,
                f"{entity}_weight": household["household_weight"].to_numpy(),
            }
        )
    return frames, np.array(fips, dtype=np.int64)


def _rows_for(frames: dict[str, pd.DataFrame], mask: np.ndarray) -> dict:
    return {
        entity: frame[mask].reset_index(drop=True) for entity, frame in frames.items()
    }


def _child_simulation(output_frames: dict, national_input_household: pd.DataFrame):
    """Return a stand-in for one child's computed simulation.

    Like the real child, its input dataset is the unscoped national dataset
    (which carries ``state_fips``) and its output covers only its group.
    """
    return SimpleNamespace(
        output_dataset=SimpleNamespace(
            data=SimpleNamespace(entity_data=dict(output_frames))
        ),
        dataset=SimpleNamespace(
            data=SimpleNamespace(household=national_input_household)
        ),
    )


@pytest.fixture(scope="module")
def us_module():
    from policyengine.tax_benefit_models import us

    def analysis(baseline, reform, include_cliff_impacts=False):
        from policyengine.outputs.decile_impact import calculate_decile_impacts

        return SimpleNamespace(
            decile_impacts=calculate_decile_impacts(
                baseline_simulation=baseline,
                reform_simulation=reform,
                income_variable="household_net_income",
            )
        )

    return SimpleNamespace(model=us.model, economic_impact_analysis=analysis)


@pytest.fixture(scope="module")
def national():
    baseline, fips = _national_frames(reform=False)
    reform, _ = _national_frames(reform=True)
    input_household = pd.DataFrame(
        {"household_id": baseline["household"]["household_id"], "state_fips": fips}
    )
    children = []
    for group in GROUPS:
        mask = np.isin(fips, [STATES[code][0] for code in group])
        payload = extract_output_microdata(
            _child_simulation(_rows_for(baseline, mask), input_household),
            _child_simulation(_rows_for(reform, mask), input_household),
        )
        children.append(
            {"model_version": "1.0.0", "data_version": "data-1", "_microdata": payload}
        )
    return SimpleNamespace(
        baseline=baseline, reform=reform, fips=fips, children=children
    )


def _strategies(codes=STATES):
    return {
        code: RowFilterStrategy(
            variable_name="state_fips", variable_value=STATES[code][0]
        )
        for code in codes
    }


def _separate_state_run(national, code, us_module):
    """Build the output a separate row-filtered run for ``code`` would build.

    The rows are selected directly with pandas, independent of the filter
    the reduce uses.
    """
    mask = national.fips == STATES[code][0]

    def simulation(frames):
        dataset = reduce_mod._us_dataset_from_frames(_rows_for(frames, mask), year=YEAR)
        stand_in = PrecomputedSimulation(
            dataset=dataset, tax_benefit_model_version=us_module.model, policy=None
        )
        stand_in.output_dataset = dataset
        return stand_in

    baseline = simulation(national.baseline)
    return SimulationOutputBuilder(
        country="us",
        simulation_params={"country": "us", "scope": "macro"},
        country_module=us_module,
        dataset=baseline.dataset,
        baseline=baseline,
        reform=simulation(national.reform),
        resolved_region_code=code,
    ).serialize()


def _assert_same(expected, actual, path=""):
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        assert set(expected) == set(actual), path
        for key in expected:
            _assert_same(expected[key], actual[key], f"{path}/{key}")
    elif isinstance(expected, list):
        assert len(expected) == len(actual), path
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            _assert_same(left, right, f"{path}/{index}")
    elif isinstance(expected, float):
        assert math.isclose(expected, actual, rel_tol=1e-12, abs_tol=1e-9), path
    else:
        assert expected == actual, path


def _reduce(national, us_module, regions):
    return build_national_output(
        national.children,
        country="us",
        simulation_params={"country": "us", "scope": "macro"},
        country_module=us_module,
        year=YEAR,
        regions=regions,
    )


class TestRegionBlocks:
    def test__children_carry_state_fips_from_their_input_dataset(self, national):
        household = national.children[0]["_microdata"]["baseline"]["household"]
        assert set(household["state_fips"]) == {15, 19}
        assert (
            national.children[1]["_microdata"]["dtypes"]["reform"]["household"][
                "state_fips"
            ]
            == "int64"
        )

    @pytest.mark.parametrize("code", sorted(STATES))
    def test__block_equals_a_separate_row_filtered_state_run(
        self, national, us_module, code
    ):
        output = _reduce(national, us_module, _strategies())
        block = output["regions"][code]
        direct = _separate_state_run(national, code, us_module)
        direct.update(model_version="1.0.0", data_version="data-1")

        _assert_same(direct, block)
        # The comparison is not vacuous: the block holds this state's own,
        # non-zero figures.
        households = STATES[code][1]
        assert block["budget"]["households"] == pytest.approx(
            float(
                national.baseline["household"]["household_weight"][
                    national.fips == STATES[code][0]
                ].sum()
            )
        )
        assert households > 0
        assert block["budget"]["benefit_spending_impact"] > 0
        assert len(block["decile"]["average"]) == 10
        districts = block["congressional_district_impact"]["districts"]
        assert len(districts) == STATES[code][2]
        assert {d["district"].split("-")[0] for d in districts} == {
            code.split("/")[1].upper()
        }

    def test__national_output_is_unchanged_by_the_blocks(self, national, us_module):
        with_blocks = _reduce(national, us_module, _strategies())
        without = _reduce(national, us_module, None)
        assert "regions" not in without
        assert {k: v for k, v in with_blocks.items() if k != "regions"} == without

    def test__blocks_add_up_to_the_national_budget(self, national, us_module):
        output = _reduce(national, us_module, _strategies())
        total = sum(
            block["budget"]["benefit_spending_impact"]
            for block in output["regions"].values()
        )
        assert total == pytest.approx(output["budget"]["benefit_spending_impact"])

    def test__a_failing_state_is_omitted_and_the_rest_are_kept(
        self, national, us_module, caplog
    ):
        regions = {
            **_strategies(),
            # No household matches, so this state's filter raises.
            "state/wy": RowFilterStrategy(
                variable_name="state_fips", variable_value=56
            ),
        }
        output = _reduce(national, us_module, regions)
        assert set(output["regions"]) == set(STATES)
        assert "state/wy" in caplog.text

    def test__every_state_failing_leaves_the_field_absent(
        self, national, us_module, monkeypatch
    ):
        def boom(*args, **kwargs):
            msg = "builder failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(nro, "_build_region_output", boom)
        output = _reduce(national, us_module, _strategies())
        assert "regions" not in output
        assert output["budget"]["households"] > 0

    def test__children_without_state_fips_omit_the_blocks(
        self, national, us_module, caplog
    ):
        children = []
        for child in national.children:
            payload = {
                side: {
                    entity: {k: v for k, v in columns.items() if k != "state_fips"}
                    for entity, columns in payload_side.items()
                }
                for side, payload_side in child["_microdata"].items()
            }
            children.append({**child, "_microdata": payload})
        output = build_national_output(
            children,
            country="us",
            simulation_params={"country": "us", "scope": "macro"},
            country_module=us_module,
            year=YEAR,
            regions=_strategies(),
        )
        assert "regions" not in output
        assert "Omitting the per-state block" in caplog.text


class TestHouseholdInputColumns:
    def test__skips_a_simulation_whose_input_is_not_loaded(self):
        unloaded = SimpleNamespace(dataset=SimpleNamespace(data=None))
        loaded = SimpleNamespace(
            dataset=SimpleNamespace(
                data=SimpleNamespace(
                    household=pd.DataFrame(
                        {"household_id": [1, 2, 3], "state_fips": [6, 15, 19]}
                    )
                )
            )
        )
        found = nro.household_input_columns(
            (unloaded, loaded), pd.Series([3, 1], index=[0, 1])
        )
        assert list(found["state_fips"]) == [19, 6]

    def test__leaves_out_a_column_that_misses_households(self):
        loaded = SimpleNamespace(
            dataset=SimpleNamespace(
                data=SimpleNamespace(
                    household=pd.DataFrame({"household_id": [1], "state_fips": [6]})
                )
            )
        )
        assert nro.household_input_columns((loaded,), pd.Series([1, 2])) == {}


class TestRegionEligibility:
    def _patch_resolution(self, monkeypatch, datasets):
        """Resolve each code to a row filter on the dataset ``datasets`` names."""

        def resolve(*, country_module, country, params):
            code = params.get("region") or country
            strategy = _strategies().get(code)
            return sr.RegionResolution(
                code=code, dataset_reference=datasets[code], scoping_strategy=strategy
            )

        def select(params, *, region_resolution):
            reference = region_resolution.dataset_reference
            return sr.DatasetSelection(reference, f"hf://{reference}", True)

        monkeypatch.setattr(sr, "_resolve_region", resolve)
        monkeypatch.setattr(sr, "_resolve_dataset_selection", select)

    def test__only_row_filters_on_the_national_dataset_qualify(self, monkeypatch):
        self._patch_resolution(
            monkeypatch,
            {
                "us": "national",
                "state/hi": "national",
                "state/ia": "national",
                "state/ca": "local-area",
            },
        )
        eligible = nro.row_filter_regions_on_dataset(
            ["state/hi", "state/ia", "state/ca", "state/zz"],
            country="us",
            country_module=None,
        )
        # CA's own run uses another dataset; ZZ does not resolve.
        assert sorted(eligible) == ["state/hi", "state/ia"]
        assert eligible["state/hi"].variable_value == 15

    def test__installed_bundle_routes_states_to_their_own_dataset_or_not(self):
        """Pin the interaction with issue #717 for the installed bundle.

        When the bundle gives state regions their own dataset (the ACS
        local-area file), no state qualifies and the national result carries
        no blocks. When states filter the national dataset, every state
        qualifies.
        """
        country_module = sr._country_module("us")
        codes = [f"state/{code}" for code in ("ca", "tx", "hi")]
        national_path = country_module.model.get_region("us").dataset_path
        state_paths = {
            country_module.model.get_region(code).dataset_path for code in codes
        }
        eligible = nro.row_filter_regions_on_dataset(
            codes, country="us", country_module=country_module
        )
        if state_paths <= {None, national_path}:
            assert sorted(eligible) == sorted(codes)
        else:
            assert eligible == {}

    def test__include_regions_defaults_on_and_false_turns_it_off(self):
        assert nro.include_regions_requested({}) is True
        assert nro.include_regions_requested({"include_regions": None}) is True
        assert nro.include_regions_requested({"include_regions": True}) is True
        assert nro.include_regions_requested({"include_regions": False}) is False
