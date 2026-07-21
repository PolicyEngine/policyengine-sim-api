"""Unit tests for the segmented-national fan-out runner (C5)."""

from types import SimpleNamespace

import pytest

from src.modal import segmented_national as sn
from policyengine_simulation_executor import simulation_runtime as sr


NATIONAL = {"country": "us", "scope": "macro", "time_period": "2026"}


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
        assert (
            sn.should_run_segmented_national({**NATIONAL, "segmented": True})
            is True
        )


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
            {**NATIONAL, "_telemetry": {"run_id": "r1"}}, ["state/ca"]
        )
        assert with_telemetry["_telemetry"] == {"run_id": "r1"}
        without = sn.build_group_child_payload(dict(NATIONAL), ["state/ca"])
        assert "_telemetry" not in without

    def test__never_forwards_parent_metadata(self):
        payload = sn.build_group_child_payload(
            {**NATIONAL, "_metadata": {"resolved_app_name": "x"}}, ["state/ca"]
        )
        assert "_metadata" not in payload


class FakeCall:
    """A FunctionCall whose result resolves after N polls (or raises)."""

    def __init__(self, result, polls_until_ready=0, error=None):
        self.result = result
        self.polls_until_ready = polls_until_ready
        self.error = error
        self.cancelled = False

    def get(self, timeout):
        if self.error is not None:
            raise self.error
        if self.polls_until_ready > 0:
            self.polls_until_ready -= 1
            raise TimeoutError
        return self.result

    def cancel(self):
        self.cancelled = True


class FakeModal:
    def __init__(self, calls):
        self._calls = list(calls)
        self.spawned_payloads = []
        self.from_name_args = None
        fake = self

        class _Function:
            @staticmethod
            def from_name(app_name, function_name):
                fake.from_name_args = (app_name, function_name)
                return SimpleNamespace(spawn=fake._spawn)

        self.Function = _Function

    def _spawn(self, payload):
        self.spawned_payloads.append(payload)
        return self._calls[len(self.spawned_payloads) - 1]


def _runner(fake_modal, params=None):
    return sn.SegmentedNationalRunner(
        params or dict(NATIONAL),
        app_name="policyengine-simulation-py-test",
        modal_module=fake_modal,
        poll_interval_seconds=0.001,
        poll_interval_max_seconds=0.002,
    )


def _twenty_calls(**kwargs):
    return [FakeCall({"child": i}, **kwargs) for i in range(20)]


class TestSegmentedNationalRunner:
    def test__spawns_one_child_per_partition_group(self, monkeypatch):
        fake = FakeModal(_twenty_calls())
        runner = _runner(fake)
        monkeypatch.setattr(runner, "_reduce", lambda results: {"ok": results})

        result = runner.run()

        assert fake.from_name_args == (
            "policyengine-simulation-py-test",
            "run_simulation",
        )
        assert len(fake.spawned_payloads) == 20
        groups = [p["region_group"] for p in fake.spawned_payloads]
        assert groups == [list(g) for g in runner.groups]
        assert all(p["_emit_microdata"] is True for p in fake.spawned_payloads)
        # Results arrive in group order regardless of completion order.
        assert result == {"ok": [{"child": i} for i in range(20)]}

    def test__collects_out_of_order_completions_in_group_order(
        self, monkeypatch
    ):
        calls = [
            FakeCall({"child": i}, polls_until_ready=(19 - i) % 3)
            for i in range(20)
        ]
        runner = _runner(FakeModal(calls))
        monkeypatch.setattr(runner, "_reduce", lambda results: results)
        assert runner.run() == [{"child": i} for i in range(20)]

    def test__child_failure_fails_fast_and_cancels_the_rest(self, monkeypatch):
        calls = _twenty_calls(polls_until_ready=5)
        calls[7] = FakeCall(None, error=ValueError("boom"))
        runner = _runner(FakeModal(calls))
        monkeypatch.setattr(runner, "_reduce", lambda results: results)

        with pytest.raises(RuntimeError, match="Segmented national child failed"):
            runner.run()
        assert all(c.cancelled for c in calls if c.error is None)

    def test__reduce_receives_clean_params_and_all_children(self, monkeypatch):
        captured = {}

        def fake_build_national_output(child_results, **kwargs):
            captured["children"] = child_results
            captured.update(kwargs)
            return {"budget": {}}

        monkeypatch.setattr(
            sn, "build_national_output", fake_build_national_output
        )
        monkeypatch.setattr(sr, "_country_module", lambda c: "fake-country-module")

        params = {**NATIONAL, "segmented": True, "_telemetry": {"run_id": "r"}}
        runner = _runner(FakeModal(_twenty_calls()), params=params)
        assert runner.run() == {"budget": {}}

        assert len(captured["children"]) == 20
        assert captured["country"] == "us"
        assert captured["year"] == 2026
        assert captured["country_module"] == "fake-country-module"
        assert "segmented" not in captured["simulation_params"]
        assert "_telemetry" not in captured["simulation_params"]

    def test__uk_has_no_partition_to_run(self):
        with pytest.raises(ValueError, match="No national partition"):
            _runner(FakeModal([]), params={"country": "uk", "scope": "macro"})


class TestDispatchRunSimulation:
    def test__eligible_national_takes_the_segmented_path(self, monkeypatch):
        calls = {}

        def fake_segmented(params, *, app_name):
            calls["segmented"] = (params, app_name)
            return {"segmented": True}

        monkeypatch.setattr(sn, "run_segmented_national_impl", fake_segmented)
        monkeypatch.setattr(
            sr,
            "run_simulation_impl",
            lambda params: pytest.fail("monolithic path must not run"),
        )
        result = sn.dispatch_run_simulation(dict(NATIONAL), app_name="app-x")
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
    def test__everything_else_takes_the_monolithic_path(
        self, monkeypatch, params
    ):
        monkeypatch.setattr(
            sn,
            "run_segmented_national_impl",
            lambda *a, **k: pytest.fail("segmented path must not run"),
        )
        monkeypatch.setattr(
            sr, "run_simulation_impl", lambda params: {"monolithic": True}
        )
        assert sn.dispatch_run_simulation(params, app_name="app-x") == {
            "monolithic": True
        }
