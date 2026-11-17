"""Read the synthetic JSON only; never runs a browser, model or cleanup."""
import gzip
import json
from pathlib import Path
import statistics
import sys


def summary(value):
    return {'first': value[0], 'last': value[-1], 'min': min(value),
        'max': max(value), 'mean': statistics.mean(value)}


def analyze(data):
    cases = []
    for case in data['cases']:
        if case['delivery_plan'] not in ('full_paced', 'full_slow'):
            raise ValueError('not_fixed_flow_pair')
        rows = case['synthetic_pump_metrics']
        if [row['packet'] for row in rows] != list(range(500)):
            raise ValueError('incomplete_or_ambiguous_flow_trace')
        render = case['full_render']
        clocks = {row['packet']: row for row in render['packet_clocks']}
        if len(clocks) != 500:
            raise ValueError('render_clock_unknown')
        first, last = rows[0], rows[-1]
        cases.append({'mode': case['delivery_plan'], 'packets': len(rows),
            'offered_packets_per_second': 499000 / (last['arrival_ms'] - first['arrival_ms']),
            'read_packets_per_second': 499000 / (last['read_at_ms'] - first['read_at_ms']),
            'native_write_packets_per_second': 499000 / (
                last['native_write']['begin_ms'] - first['native_write']['begin_ms']),
            'render_context_span_ms': (clocks[499]['context_frame'] - clocks[0]['context_frame']) / 48,
            'queue_at_read_sampled_peak': max(row['queued_at_read'] for row in rows),
            'arrival_to_read_ms': summary([r['read_at_ms'] - r['arrival_ms'] for r in rows]),
            'injected_delay_ms': summary([r['injected_delay_ms'] for r in rows]),
            'native_write_ms': summary([r['native_write']['end_ms'] - r['native_write']['begin_ms'] for r in rows]),
            'write_call_to_native_begin_ms': summary([r['native_write']['begin_ms'] - r['write_call_ms'] for r in rows]),
            'write_end_to_render_observation_ms': summary([clocks[r['packet']]['observed_ms'] - r['native_write']['end_ms'] for r in rows]),
            'matched_packets': render['matched_packets'], 'missing_packets': render['missing_packets'],
            'ambiguous_or_duplicate_packets': render['ambiguous_or_duplicate_packets'],
            'whole_payload_contiguous': render['all_payload_samples_verified'],
            'inter_packet_gaps': len(render['inter_packet_gaps']),
            'zero_gap_frames': sum(gap['frames'] for gap in render['inter_packet_gaps'] if gap['all_zero']),
            'packet_observation_delay_ms': render['packet_observation_delay_ms'],
            'cleanup': case['cleanup']})
    return {'scope': 'fixed synthetic timer-push pair, not RTC/Oracle/model',
        'queue_scope': 'fixture encoded ReadableStream count sampled after read, not native FIFO peak',
        'clock_scope': 'native AudioContext history coordinate, snapshot quantization; not remote consumption or ear-end latency',
        'architecture': data['architecture'], 'chromium': data['chromium'],
        'source_sha256': data['source_sha256'], 'fixture_sha256': data['fixture_sha256'], 'cases': cases}


if __name__ == '__main__':
    path = Path(sys.argv[1])
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('trace_size_limit')
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rb') as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('trace_size_limit')
    print(json.dumps(analyze(json.loads(raw)), indent=2, allow_nan=False))
