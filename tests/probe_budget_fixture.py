"""Test-only event-synchronized deadlines; never used by a production caller."""
import asyncio
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def blocked_phase_budget(probe, phase):
    """Arm the same overall cancellation only after the tested stage is entered.

    Startup scheduling time cannot decide whether the fixture reaches its blocked
    stage. The separate real-budget test covers expiry in prerequisite work.
    Neither the product's timeout nor cleanup deadline is changed.
    """
    timeout = asyncio.timeout
    deadline = None
    observe = probe.observe_stage

    def overall(delay):
        nonlocal deadline
        if deadline is None and delay == probe.probe_timeout:
            deadline = timeout(None)
            return deadline
        return timeout(delay)

    def entered(name, state, failure_kind=None):
        observe(name, state, failure_kind)
        if name == phase and state == 'entered':
            assert deadline is not None
            deadline.reschedule(asyncio.get_running_loop().time() + probe.probe_timeout)

    with patch.object(probe, 'observe_stage', side_effect=entered), \
            patch('teams_transcription.live_summary_probe.asyncio.timeout', side_effect=overall):
        yield
