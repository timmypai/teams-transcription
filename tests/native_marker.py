"""Synthetic-test marker identity, separate from signal/quiet and RTP assertions."""
import math


def marker_present(row):
    # Total RMS plus zero-crossing count cannot identify a frequency in a PLC
    # transient. The worklet measures the actual 1100Hz component in the SAME
    # 480-sample window. Keep the original .06 RMS threshold; missing evidence
    # is an assertion error, not silence. No production gate uses this helper.
    value=row['marker_rms']
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        raise AssertionError('marker_projection_unverified')
    return value>.06
