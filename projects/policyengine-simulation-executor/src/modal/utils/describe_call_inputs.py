"""Print a Modal function call's inputs, with the container that runs each.

Usage, from projects/policyengine-simulation-executor:
``uv run --frozen python -m src.modal.utils.describe_call_inputs <call-id>``.
The Modal client reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.

Prints a JSON array with one object per input of the call:
``{"input_id": ..., "container_id": ..., "status": ...}``. ``container_id``
is Modal's task ID (``ta-...``), which ``modal container stop`` takes, or
empty while Modal hasn't given the input a container. ``status`` names a
``modal.call_graph.InputStatus``: ``PENDING`` until the input finishes, then
``SUCCESS``, ``FAILURE``, ``INIT_FAILURE``, ``TERMINATED`` or ``TIMEOUT``.

Modal returns the whole call graph a call is part of, where a call spawned
inside another call's input is nested under that input, so the call's own
inputs are found by its ID anywhere in the graph. Modal records the graph
best-effort and not in real time, so a new call may be missing and a caller
asks again. A failure to ask is printed on standard error, with exit status
1. The staging preemption check
(``.github/scripts/simulation-staging-preemption-check.sh``) uses this to
find the container to stop, and the container that ran the call again.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import import_module
from typing import Any


def call_inputs(
    modal_call_id: str,
    *,
    modal_module: Any = None,  # noqa: ANN401  # the modal module, or a stand-in for it
) -> list[dict[str, str]]:
    """Return each input of call ``modal_call_id``, in the graph's order.

    The graph is walked depth first, each input before the calls spawned
    inside it.
    """
    modal = modal_module if modal_module is not None else import_module("modal")
    graph = modal.FunctionCall.from_id(modal_call_id).get_call_graph()
    found: list[dict[str, str]] = []
    waiting = list(reversed(graph))
    while waiting:
        input_info = waiting.pop()
        if input_info.function_call_id == modal_call_id:
            found.append(
                {
                    "input_id": input_info.input_id,
                    "container_id": input_info.task_id,
                    "status": input_info.status.name,
                }
            )
        waiting.extend(reversed(input_info.children))
    return found


def main(
    argv: list[str] | None = None,
    *,
    modal_module: Any = None,  # noqa: ANN401  # the modal module, or a stand-in for it
) -> int:
    """Print the call's inputs as JSON; return the process's exit status."""
    parser = argparse.ArgumentParser(
        description="Print a Modal function call's inputs and their containers."
    )
    parser.add_argument("modal_call_id", help="the Modal function call ID (fc-...)")
    arguments = parser.parse_args(argv)
    try:
        inputs = call_inputs(arguments.modal_call_id, modal_module=modal_module)
    except Exception as error:  # noqa: BLE001  # any failure to ask is the caller's to retry
        print(
            f"Could not read Modal call {arguments.modal_call_id}'s call graph: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(inputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
