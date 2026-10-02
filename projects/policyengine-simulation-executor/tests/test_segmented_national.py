"""Unit tests for the segmented-national fan-out runner (C5/C6 + review fixes)."""

from types import SimpleNamespace

import pytest
from conftest import NoOpObservabilityRuntime
from policyengine_simulation_contract.spm import SPMInputError

from policyengine_simulation_executor import simulation_runtime as sr
from policyengine_simulation_executor import spm as executor_spm
from policyengine_simulation_executor.national_partition import (
    US_NATIONAL_REGION_GROUPS,
)
from src.modal import segmented_national as sn

NATIONAL = {"country": "us", "scope": "macro", "time_period": "2026"}
TEST_RUNTIME = NoOpObservabilityRuntime()


def _bare_country_module():
    """A country module whose model has no loadable region registry."""
    return SimpleNamespace(
        model=SimpleNamespace(get_region=lambda code: None, region_registry=None)
    )


class TestIsPlainNationalMacro:
    def test__plain_national_shape(self):
        assert sn.is_plain_national_macro(dict(NATIONAL)) is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"region": "state/ut"},
            {"region_group": ["state/hi"]},
            {"country": "uk"},
            {"scope": "household"},
            {"scope": None},
        ],
    )
    def test__non_national_shapes(self, overrides):
        assert sn.is_plain_national_macro({**NATIONAL, **overrides}) is False

    def test__knobs_do_not_change_the_shape(self):
        assert sn.is_plain_national_macro({**NATIONAL, "segmented": False}) is True

    @pytest.mark.parametrize("region", ["us", "US", " us "])
    def test__v1_style_region_us_is_national(self, region):
        # API v1 sends region:"us" on every national macro run; both
        # national spellings must match (simulation_runtime normalises
        # None/empty/"us" to the country).
        assert sn.is_plain_national_macro({**NATIONAL, "region": region}) is True


class TestShouldRunSegmentedNational:
    def test__plain_us_national_macro_is_eligible(self):
        assert sn.should_run_segmented_national(dict(NATIONAL)) is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"region": "state/ut"},
            {"region_group": ["state/hi", "state/ia"]},
            {"segmented": False},
            {"country": "uk"},
            {"scope": "household"},
            {"scope": None},
            {"include_cliffs": True},
        ],
    )
    def test__ineligible_variants(self, overrides):
        params = {**NATIONAL, **overrides}
        assert sn.should_run_segmented_national(params) is False

    def test__explicit_segmented_true_is_eligible(self):
        assert sn.should_run_segmented_national({**NATIONAL, "segmented": True}) is True

    def test__v1_style_region_us_is_eligible(self):
        assert sn.should_run_segmented_national({**NATIONAL, "region": "us"}) is True

    @pytest.mark.parametrize("policy_key", ["reform", "baseline"])
    def test__labor_supply_response_reforms_fall_back_monolithic(self, policy_key):
        # The reduce's stand-ins carry no policy, so LSR would silently
        # zero out (see LSR_PARAMETER_PREFIX) — these must run monolithic.
        params = {
            **NATIONAL,
            policy_key: {
                "gov.simulation.labor_supply_responses.elasticities.income.all": {
                    "2026-01-01.2100-12-31": -0.05
                }
            },
        }
        assert sn.should_run_segmented_national(params) is False

    def test__non_lsr_reform_stays_eligible(self):
        params = {
            **NATIONAL,
            "reform": {
                "gov.irs.credits.ctc.refundable.fully_refundable": {
                    "2023-01-01.2100-12-31": True
                }
            },
        }
        assert sn.should_run_segmented_national(params) is True


class TestBuildGroupChildPayload:
    def test__scopes_to_group_and_requests_microdata(self):
        params = {**NATIONAL, "reform": {"x": 1}, "segmented": True}
        payload = sn.build_group_child_payload(params, ["state/hi", "state/ia"])
        assert payload["region_group"] == ["state/hi", "state/ia"]
        assert payload["_emit_microdata"] is True
        assert payload["reform"] == {"x": 1}
        assert "segmented" not in payload
        assert "_metadata" not in payload

    def test__reattaches_telemetry_only_when_present(self):
        with_telemetry = sn.build_group_child_payload(
            {**NATIONAL, "_telemetry": {"submission_claim_id": "p1"}}, ["state/ca"]
        )
        assert with_telemetry["_telemetry"] == {"submission_claim_id": "p1"}
        without = sn.build_group_child_payload(dict(NATIONAL), ["state/ca"])
        assert "_telemetry" not in without

    def test__never_forwards_parent_metadata(self):
        payload = sn.build_group_child_payload(
            {**NATIONAL, "_metadata": {"resolved_app_name": "x"}}, ["state/ca"]
        )
        assert "_metadata" not in payload

    def test__strips_v1_style_region_us_from_children(self):
        # A child must scope by its region_group alone, never a stale
        # parent region.
        payload = sn.build_group_child_payload(
            {**NATIONAL, "region": "us"}, ["state/ca"]
        )
        assert "region" not in payload
        assert payload["region_group"] == ["state/ca"]


class FakeCall:
    """A FunctionCall whose result resolves after N polls (or raises)."""

    def __init__(self, result, polls_until_ready=0, error=None, errors_once=0):
        self.result = result
        self.polls_until_ready = polls_until_ready
        self.error = error
        self.errors_once = errors_once
        self.cancelled = False

    def get(self, timeout):
        if self.errors_once > 0:
            self.errors_once -= 1
            raise ConnectionError("transient control-plane blip")
        if self.error is not None:
            raise self.error
        if self.polls_until_ready > 0:
            self.polls_until_ready -= 1
            raise TimeoutError
        return self.result

    def cancel(self):
        self.cancelled = True


class FakeModal:
    def __init__(self, calls, fail_spawn_at=None):
        self._calls = list(calls)
        self.fail_spawn_at = fail_spawn_at
        self.spawned_payloads = []
        self.spawned_observability_contexts = []
        self.from_name_args = None
        fake = self

        class _Function:
            @staticmethod
            def from_name(app_name, function_name):
                fake.from_name_args = (app_name, function_name)
                return SimpleNamespace(spawn=fake._spawn)

        self.Function = _Function

    def _spawn(self, payload, *, observability_context=None):
        if (
            self.fail_spawn_at is not None
            and len(self.spawned_payloads) == self.fail_spawn_at
        ):
            raise ConnectionError("spawn RPC failed")
        self.spawned_payloads.append(payload)
        self.spawned_observability_contexts.append(observability_context)
        return self._calls[len(self.spawned_payloads) - 1]


def _runner(fake_modal, params=None):
    return sn.SegmentedNationalRunner(
        params or dict(NATIONAL),
        app_name="policyengine-simulation-py-test",
        runtime=TEST_RUNTIME,
        modal_module=fake_modal,
        poll_interval_seconds=0.001,
        poll_interval_max_seconds=0.002,
    )


def _twenty_calls(**kwargs):
    return [FakeCall({"child": i}, **kwargs) for i in range(20)]


@pytest.fixture
def bare_country(monkeypatch):
    monkeypatch.setattr(sr, "_country_module", lambda c: _bare_country_module())


class TestSegmentedNationalRunner:
    def test__spawns_one_child_per_group_into_the_segment_pool(
        self, monkeypatch, bare_country
    ):
        fake = FakeModal(_twenty_calls())
        runner = _runner(fake)
        monkeypatch.setattr(
            runner, "_reduce", lambda results, country_module: {"ok": results}
        )

        result = runner.run()

        # Children draw from the dedicated segment pool, never the
        # gateway-facing run_simulation pool the parent occupies.
        assert fake.from_name_args == (
            "policyengine-simulation-py-test",
            sn.SEGMENT_FUNCTION_NAME,
        )
        assert len(fake.spawned_payloads) == 20
        groups = [p["region_group"] for p in fake.spawned_payloads]
        assert groups == [list(g) for g in runner.groups]
        assert all(p["_emit_microdata"] is True for p in fake.spawned_payloads)
        assert all(
            context == TEST_RUNTIME.capture_context()
            for context in fake.spawned_observability_contexts
        )
        assert all(
            "_observability_context" not in payload for payload in fake.spawned_payloads
        )
        assert result == {"ok": [{"child": i} for i in range(20)]}

    def test__collects_out_of_order_completions_in_group_order(
        self, monkeypatch, bare_country
    ):
        calls = [
            FakeCall({"child": i}, polls_until_ready=(19 - i) % 3) for i in range(20)
        ]
        runner = _runner(FakeModal(calls))
        monkeypatch.setattr(runner, "_reduce", lambda results, country_module: results)
        assert runner.run() == [{"child": i} for i in range(20)]

    def test__child_failure_fails_fast_and_cancels_the_rest(
        self, monkeypatch, bare_country
    ):
        calls = _twenty_calls(polls_until_ready=5)
        calls[7] = FakeCall(None, error=ValueError("boom"))
        runner = _runner(FakeModal(calls))
        monkeypatch.setattr(runner, "_reduce", lambda results, country_module: results)

        with pytest.raises(RuntimeError, match="Segmented national child failed"):
            runner.run()
        assert all(c.cancelled for c in calls if c.error is None)

    def test__one_transient_poll_error_is_tolerated(self, monkeypatch, bare_country):
        # A single control-plane blip on one handle must not kill the job.
        calls = _twenty_calls()
        calls[3] = FakeCall({"child": 3}, errors_once=1)
        runner = _runner(FakeModal(calls))
        monkeypatch.setattr(runner, "_reduce", lambda results, country_module: results)
        assert runner.run() == [{"child": i} for i in range(20)]

    def test__spawn_failure_cancels_already_spawned_children(
        self, monkeypatch, bare_country
    ):
        calls = _twenty_calls(polls_until_ready=100)
        fake = FakeModal(calls, fail_spawn_at=15)
        runner = _runner(fake)
        monkeypatch.setattr(runner, "_reduce", lambda results, country_module: results)

        with pytest.raises(ConnectionError):
            runner.run()
        assert all(c.cancelled for c in calls[:15])

    def test__uncovered_registry_states_ride_in_the_last_group(
        self, monkeypatch, bare_country
    ):
        partition_codes = [
            code for group in US_NATIONAL_REGION_GROUPS for code in group
        ]
        registry = SimpleNamespace(
            regions=[
                SimpleNamespace(code=code, region_type="state")
                for code in partition_codes + ["state/pr"]
            ]
        )
        country_module = SimpleNamespace(
            model=SimpleNamespace(
                get_region=lambda code: None, region_registry=registry
            )
        )
        monkeypatch.setattr(sr, "_country_module", lambda c: country_module)

        fake = FakeModal([FakeCall({"child": i}) for i in range(20)])
        runner = _runner(fake)
        monkeypatch.setattr(runner, "_reduce", lambda results, country_module: results)
        runner.run()

        assert runner.groups[-1][-1] == "state/pr"
        assert fake.spawned_payloads[-1]["region_group"][-1] == "state/pr"
        # Only the last group changes.
        assert [p["region_group"] for p in fake.spawned_payloads[:-1]] == [
            list(g) for g in US_NATIONAL_REGION_GROUPS[:-1]
        ]

    def test__reduce_receives_clean_params_and_all_children(self, monkeypatch):
        captured = {}

        def fake_build_national_output(child_results, **kwargs):
            captured["children"] = child_results
            captured.update(kwargs)
            return {"budget": {}}

        monkeypatch.setattr(sn, "build_national_output", fake_build_national_output)
        monkeypatch.setattr(sr, "_country_module", lambda c: _bare_country_module())

        params = {
            **NATIONAL,
            "segmented": True,
            "_telemetry": {"submission_claim_id": "p"},
        }
        runner = _runner(FakeModal(_twenty_calls()), params=params)
        assert runner.run() == {"budget": {}}

        assert len(captured["children"]) == 20
        assert captured["country"] == "us"
        assert captured["year"] == 2026
        assert "segmented" not in captured["simulation_params"]
        assert "_telemetry" not in captured["simulation_params"]

    def test__uk_has_no_partition_to_run(self):
        with pytest.raises(ValueError, match="No national partition"):
            _runner(FakeModal([]), params={"country": "uk", "scope": "macro"})


class TestDispatchRunSimulation:
    def test__eligible_national_takes_the_segmented_path(self, monkeypatch):
        calls = {}

        def fake_segmented(params, *, app_name, runtime):
            calls["segmented"] = (params, app_name)
            return {"segmented": True}

        monkeypatch.setattr(sn, "run_segmented_national_impl", fake_segmented)
        monkeypatch.setattr(executor_spm, "normalize_runtime_spm", lambda params: None)
        monkeypatch.setattr(
            sr,
            "run_simulation_impl",
            lambda params, **kwargs: pytest.fail("monolithic path must not run"),
        )
        result = sn.dispatch_run_simulation(
            dict(NATIONAL), app_name="app-x", runtime=TEST_RUNTIME
        )
        assert result == {"segmented": True}
        assert calls["segmented"][1] == "app-x"

    @pytest.mark.parametrize(
        "params",
        [
            {**NATIONAL, "segmented": False},
            {**NATIONAL, "region": "state/ut"},
            {"country": "uk", "scope": "macro"},
        ],
    )
    def test__everything_else_takes_the_monolithic_path(self, monkeypatch, params):
        monkeypatch.setattr(
            sn,
            "run_segmented_national_impl",
            lambda *a, **k: pytest.fail("segmented path must not run"),
        )
        monkeypatch.setattr(executor_spm, "normalize_runtime_spm", lambda params: None)
        monkeypatch.setattr(
            sr, "run_simulation_impl", lambda params, **kwargs: {"monolithic": True}
        )
        assert sn.dispatch_run_simulation(
            params, app_name="app-x", runtime=TEST_RUNTIME
        ) == {"monolithic": True}


SPM_SELECTION = {
    "forecast_content_sha256": "a" * 64,
    "scenario": "ce_trend",
    "geography_kind": "national",
    "geography_id": None,
    "county_vintage": "2020",
    "as_of": None,
}


def _spm_receipt(*, selection=SPM_SELECTION, year="2026"):
    return {
        "schema_version": "canonical-spm-provenance-v2",
        "forecast_id": "test-only",
        "forecast_sha256": selection["forecast_content_sha256"],
        "scenario": selection["scenario"],
        "geography_kind": selection["geography_kind"],
        "geography_id": selection["geography_id"],
        "county_vintage": selection["county_vintage"],
        "as_of": selection["as_of"],
        "years": [year],
        "runtime_versions": {
            "policyengine": "test-only",
            "policyengine-core": "test-only",
            "policyengine-us": "test-only",
            "spm-calculator": "test-only",
        },
    }


def _spm_child(index, *, selection=SPM_SELECTION, year="2026"):
    """One child's result carrying that segment's own SPM receipts."""
    return {
        "child": index,
        "spm_provenance": {
            "schema_version": "canonical-spm-comparison-v2",
            "baseline": {
                "receipt": _spm_receipt(selection=selection, year=year),
                "execution_count": 1,
            },
            "reform": {
                "receipt": _spm_receipt(selection=selection, year=year),
                "execution_count": 1,
            },
        },
    }


def _spm_child_missing(index, field):
    """A child whose canonical receipt lost a resolved option."""
    child = _spm_child(index)
    del child["spm_provenance"]["baseline"]["receipt"][field]
    return child


@pytest.fixture
def stub_reduce(monkeypatch):
    """The microdata reduce itself is covered by
    test_segmented_national_reduce; these tests pin the SPM merge."""
    monkeypatch.setattr(
        sn, "build_national_output", lambda children, **kwargs: {"budget": {}}
    )


class TestSegmentedNationalSPM:
    def test__child_receipts_combine_into_one_national_receipt(
        self, bare_country, stub_reduce
    ):
        fake = FakeModal([FakeCall(_spm_child(i)) for i in range(20)])
        runner = _runner(fake, params={**NATIONAL, "spm": SPM_SELECTION})

        output = runner.run()

        # The resolved selection rides to every child unchanged. Completed
        # output publishes that configuration only inside the receipt.
        assert all(p["spm"] == SPM_SELECTION for p in fake.spawned_payloads)
        assert "spm_config" not in output
        # One receipt describes all identical child executions. The counts
        # prove that every segment contributed without duplicating metadata.
        provenance = output["spm_provenance"]
        assert provenance["baseline"]["execution_count"] == 20
        assert provenance["reform"]["execution_count"] == 20
        assert provenance["baseline"]["receipt"]["forecast_id"] == "test-only"
        assert provenance["baseline"]["receipt"]["county_vintage"] == "2020"
        assert output["budget"] == {}

    @pytest.mark.parametrize(
        "rogue,reason",
        [
            (
                _spm_child(7, selection={**SPM_SELECTION, "scenario": "zero_real"}),
                "differs from the resolved selection",
            ),
            (_spm_child(7, year="2025"), "does not cover the requested year"),
            (_spm_child_missing(7, "county_vintage"), "complete resolved"),
        ],
    )
    def test__child_receipt_disagreeing_with_the_request_is_typed(
        self, bare_country, stub_reduce, rogue, reason
    ):
        # One segment computed something other than what was requested:
        # fail with the public typed code, never publish a mixed national
        # result whose receipt does not describe every segment.
        calls = [FakeCall(_spm_child(i)) for i in range(20)]
        calls[7] = FakeCall(rogue)
        runner = _runner(FakeModal(calls), params={**NATIONAL, "spm": SPM_SELECTION})

        with pytest.raises(SPMInputError, match=reason) as error:
            runner.run()
        assert error.value.code == "SPM_CONFIGURATION_UNAVAILABLE"

    def test__unrequested_child_receipt_is_typed(self, bare_country, stub_reduce):
        # A historical request must not inherit a child's stray receipt.
        calls = [FakeCall({"child": i}) for i in range(20)]
        calls[3] = FakeCall(_spm_child(3))
        runner = _runner(FakeModal(calls))

        with pytest.raises(SPMInputError, match="Unexpected SPM receipt"):
            runner.run()

    def test__a_run_without_spm_adds_no_receipt_keys(self, bare_country, stub_reduce):
        runner = _runner(FakeModal([FakeCall({"child": i}) for i in range(20)]))
        assert runner.run() == {"budget": {}}


class TestDispatchResolvesSPM:
    def test__selection_is_resolved_before_the_fan_out(self, monkeypatch):
        # Children and the parent's receipt check must both see the
        # worker-resolved selection, not the caller's partial one.
        monkeypatch.setattr(
            executor_spm, "normalize_runtime_spm", lambda params: SPM_SELECTION
        )
        seen = {}

        def fake_segmented(params, *, app_name, runtime):
            seen["params"] = params
            return {"segmented": True}

        monkeypatch.setattr(sn, "run_segmented_national_impl", fake_segmented)

        result = sn.dispatch_run_simulation(
            {**NATIONAL, "spm": {"geography_kind": "national"}},
            app_name="app-x",
            runtime=TEST_RUNTIME,
        )

        assert result == {"segmented": True}
        assert seen["params"]["spm"] == SPM_SELECTION

    def test__typed_spm_error_spawns_nothing(self, monkeypatch):
        # Resolution failures are cheap only if they happen before 20
        # children are spawned.
        def unavailable(params):
            raise SPMInputError("SPM_GEOGRAPHY_REQUIRED", "County required")

        monkeypatch.setattr(executor_spm, "normalize_runtime_spm", unavailable)
        monkeypatch.setattr(
            sn,
            "run_segmented_national_impl",
            lambda *a, **k: pytest.fail("segmented path must not run"),
        )
        monkeypatch.setattr(
            sr,
            "run_simulation_impl",
            lambda params, **kwargs: pytest.fail("monolithic path must not run"),
        )

        with pytest.raises(SPMInputError) as error:
            sn.dispatch_run_simulation(
                dict(NATIONAL), app_name="app-x", runtime=TEST_RUNTIME
            )
        assert error.value.code == "SPM_GEOGRAPHY_REQUIRED"
