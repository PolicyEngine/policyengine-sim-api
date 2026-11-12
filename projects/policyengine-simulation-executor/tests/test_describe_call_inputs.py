"""The Modal helper the staging preemption check uses to find a call's container.

``describe_call_inputs`` asks Modal for the call graph a call is part of and
prints each of the call's inputs with the container (Modal's task ID) that
runs or ran it. The check stops that container with ``modal container
stop``, then waits for the input to finish in another one.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from src.modal.utils import describe_call_inputs as describe

CALL_ID = "fc-01HOUSEHOLD"
OTHER_CALL_ID = "fc-01COORDINATOR"


def _input(
    call_id: str,
    *,
    input_id: str,
    container_id: str,
    status: str,
    children: tuple[SimpleNamespace, ...] = (),
) -> SimpleNamespace:
    """Return a stand-in for ``modal.call_graph.InputInfo``, its status named."""
    return SimpleNamespace(
        input_id=input_id,
        function_call_id=call_id,
        task_id=container_id,
        status=SimpleNamespace(name=status),
        children=list(children),
    )


def _modal_answering(
    graph: list[SimpleNamespace] | None = None, error: Exception | None = None
) -> tuple[SimpleNamespace, list[str]]:
    """Return a stand-in modal module and the call IDs it was asked about."""
    asked: list[str] = []

    def from_id(call_id: str) -> SimpleNamespace:
        asked.append(call_id)

        def get_call_graph() -> list[SimpleNamespace]:
            if error is not None:
                raise error
            return graph or []

        return SimpleNamespace(get_call_graph=get_call_graph)

    return SimpleNamespace(FunctionCall=SimpleNamespace(from_id=from_id)), asked


def test_the_calls_inputs_are_listed_wherever_they_sit_in_the_graph():
    """Modal nests a call spawned inside another call's input under that input."""
    graph = [
        _input(
            OTHER_CALL_ID,
            input_id="in-parent",
            container_id="ta-parent",
            status="PENDING",
            children=(
                _input(
                    CALL_ID,
                    input_id="in-stopped",
                    container_id="ta-stopped",
                    status="TERMINATED",
                ),
                _input(
                    OTHER_CALL_ID,
                    input_id="in-sibling",
                    container_id="ta-sibling",
                    status="SUCCESS",
                ),
            ),
        ),
        _input(CALL_ID, input_id="in-again", container_id="ta-again", status="SUCCESS"),
    ]
    modal, asked = _modal_answering(graph)

    inputs = describe.call_inputs(CALL_ID, modal_module=modal)

    assert asked == [CALL_ID]
    assert inputs == [
        {
            "input_id": "in-stopped",
            "container_id": "ta-stopped",
            "status": "TERMINATED",
        },
        {"input_id": "in-again", "container_id": "ta-again", "status": "SUCCESS"},
    ]


def test_an_input_without_a_container_has_an_empty_container_id():
    """Modal hasn't given a queued input a container yet."""
    modal, _ = _modal_answering(
        [_input(CALL_ID, input_id="in-queued", container_id="", status="PENDING")]
    )

    assert describe.call_inputs(CALL_ID, modal_module=modal) == [
        {"input_id": "in-queued", "container_id": "", "status": "PENDING"}
    ]


def test_a_call_missing_from_its_graph_has_no_inputs():
    """Modal records the call graph best-effort, so a new call may be missing."""
    modal, _ = _modal_answering(
        [
            _input(
                OTHER_CALL_ID,
                input_id="in-other",
                container_id="ta-1",
                status="SUCCESS",
            )
        ]
    )

    assert describe.call_inputs(CALL_ID, modal_module=modal) == []


def test_main_prints_the_inputs_as_json(capsys):
    modal, _ = _modal_answering(
        [_input(CALL_ID, input_id="in-1", container_id="ta-1", status="PENDING")]
    )

    exit_code = describe.main([CALL_ID], modal_module=modal)

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == [
        {"input_id": "in-1", "container_id": "ta-1", "status": "PENDING"}
    ]


def test_main_reports_a_failure_to_ask_modal_and_exits_non_zero(capsys):
    modal, _ = _modal_answering(error=RuntimeError("the call graph is unavailable"))

    exit_code = describe.main([CALL_ID], modal_module=modal)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert CALL_ID in captured.err
    assert "RuntimeError: the call graph is unavailable" in captured.err


def test_main_needs_one_call_id():
    modal, _ = _modal_answering()

    with pytest.raises(SystemExit) as refused:
        describe.main([], modal_module=modal)

    assert refused.value.code == 2
