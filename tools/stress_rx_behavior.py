"""Offline RX behavior regression under slow real SQLite status writes.

Only the listed behavior tests use the slower adapter. Deadline/expiry/reaping
tests must run with their original fixture and remain in ci_contracts.py.
No browser, provider, account or actual audio is contacted by these fixtures.
"""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]

from teams_transcription.storage import Session


BEHAVIOR_TESTS = (
    'test_slow_final_stats_closes_ingress_before_await',
    'test_provider_error_during_media_close_cannot_complete_capture',
    'test_confirmed_end_drains_accepted_pcm_and_keeps_reason',
    'test_native_A_does_not_require_or_push_slow_or_unavailable_observer',
    'test_native_observer_burst_cannot_create_A_queue_or_sample_limit',
    'test_recording_only_overflow_is_a_real_recording_failure_with_drained_tail',
    'test_unavailable_PCM_stats_do_not_stop_native_A',
    'test_unavailable_or_transient_native_diagnostics_are_not_source_failure',
    'test_native_source_runtime_failure_is_not_observer_success',
    'test_native_saved_WAV_uses_finite_recording_adapter_not_live_push',
    'test_saved_WAV_failure_still_fails_native_capture',
    'test_capture_requires_current_owner_confirmation_and_single_live',
    'test_owner_stop_cancels_pending_live_start_without_capture',
)


def main():
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    update = Session.update

    def delayed_update(self, *args, **kwargs):
        # Deliberately block where synchronous SQLite work blocks the real loop.
        time.sleep(.03)
        return update(self, *args, **kwargs)

    names = ['test_rx_contract.RxContractTests.' + name
        for _ in range(3) for name in BEHAVIOR_TESTS]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    with (directory / 'rx-behavior-stress.log').open('w') as log:
        with patch.object(Session, 'update', delayed_update):
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    receipt = {'scope':'controlled offline RX behavior stress',
        'test_methods':list(BEHAVIOR_TESTS), 'repeats':3,
        'status_write_delay_seconds':.03,
        'tests':result.testsRun, 'failures':len(result.failures),
        'errors':len(result.errors), 'skipped':len(result.skipped),
        'success':result.wasSuccessful(), 'teams_or_model_called':False,
        'audio_e2e_tested':False, 'deadline_tests_included':False}
    (directory / 'rx-behavior-stress.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
