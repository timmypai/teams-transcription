"""Replay a real captured no-marker browser control, no invented audio results."""
import json
from pathlib import Path
import unittest

from native_marker import marker_present


class MarkerReplayTests(unittest.TestCase):
    def test_captured_no_marker_plc_tail_is_red_for_old_rule_green_for_marker_identity(self):
        capture=json.loads((Path(__file__).parent/'fixtures/revocation_no_marker_44352585609.json').read_text())
        self.assertFalse(capture['post_revoke_1100hz_source_dispatched'])
        self.assertEqual(capture['source_frequency_hz'],550)
        self.assertEqual(capture['sender_packets_at_boundary'],capture['sender_packets_final'])
        rows=capture['suspect_bins']
        # This is the exact failed-main discriminator applied to OBSERVED bins,
        # not an assertion failure injected by mutating production code.
        with self.assertRaises(AssertionError):
            self.assertFalse(any(row['rms']>.06 and 950<=row['frequency']<=1250 for row in rows))
        self.assertFalse(any(marker_present(row) for row in rows))
        self.assertEqual(capture['final_200ms_peak_rms'],0)

    def test_missing_invalid_or_unmeasured_marker_cannot_mean_silence(self):
        for row in ({},{'marker_rms':None},{'marker_rms':float('nan')},{'marker_rms':-1},{'marker_rms':True}):
            with self.assertRaises((KeyError,AssertionError)):marker_present(row)
        self.assertTrue(marker_present({'marker_rms':.060001}))
        self.assertFalse(marker_present({'marker_rms':.06}))
