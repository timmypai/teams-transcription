"""Failure metadata keeps useful classes without private exception content."""
import json
import unittest

from playwright._impl._errors import TargetClosedError

from teams_transcription.diagnostics import failure_diagnostic


class FailureDiagnosticTests(unittest.TestCase):
    def test_actual_playwright_target_closed_type_is_preserved_without_text(self):
        result = failure_diagnostic(TargetClosedError('private-token SDP https://fixture.invalid'), 'browser_open')
        self.assertEqual(result, {'phase': 'browser_open', 'exception_type': 'TargetClosedError',
                                  'error_code': 'operation_failed'})
        self.assertNotIn('private-token', json.dumps(result))

    def test_existing_error_types_and_timeout_classification_stay_distinct(self):
        for kind in (TimeoutError, RuntimeError, ValueError, OSError, InterruptedError):
            with self.subTest(kind=kind):
                result = failure_diagnostic(kind('private-token'), 'rx_start')
                self.assertEqual(result['exception_type'], kind.__name__)
                self.assertEqual(result['error_code'], 'timeout' if kind is TimeoutError else 'operation_failed')
                self.assertNotIn('private-token', json.dumps(result))

    def test_untrusted_class_name_and_phase_remain_bounded(self):
        kind = type('private-token', (Exception,), {})
        result = failure_diagnostic(kind('private-SDP'), 'private-meeting-url')
        self.assertEqual(result, {'phase': 'unknown', 'exception_type': 'Exception',
                                  'error_code': 'operation_failed'})
