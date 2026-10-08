"""Deterministic checks of the bounded testing-only synchronization."""

from unittest.mock import Mock

import pytest

from policyengine_simulation_executor.precompute_benchmark.testing import (
    synchronize_testing_workers,
)


def test_waits_until_both_workers_publish_ready():
    publish = Mock()
    ready = Mock(side_effect=[False, False, True])
    sleep = Mock()
    synchronize_testing_workers(publish, ready, clock=lambda: 0, sleep=sleep)
    publish.assert_called_once_with()
    assert sleep.call_count == 2
    sleep.assert_called_with(10)


def test_absent_peer_fails_instead_of_claiming_concurrency():
    with pytest.raises(TimeoutError, match="Second testing worker"):
        synchronize_testing_workers(
            Mock(),
            lambda: False,
            timeout_seconds=1,
            clock=Mock(side_effect=[0, 0.5, 1.1]),
            sleep=Mock(),
        )
