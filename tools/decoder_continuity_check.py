"""Synthetic pacing counterexamples for the production target decoder; no RTC/model."""
import argparse
import asyncio
import hashlib
import json
import platform
from pathlib import Path

from teams_transcription.b_no_plc_history import NO_PLC_RECEIVER_JS
from teams_transcription.b_no_plc_history import with_render_history
from teams_transcription.b_no_plc_startup import with_startup_primer, LEGACY_NO_PLC_RECEIVER_JS
from teams_transcription.browser import Browser
from decoder_history_check import instrument_history


MODES = ('paced', 'burst', 'reordered', 'writer_stall')
STRESS_MODES = ('long_paced', 'long_jitter', 'slow_consumer', 'cancel_wait')
FULL_MODES = ('full_paced', 'full_burst', 'full_slow', 'two_questions', 'beta_long')
BASELINE_RECEIVER_JS = with_render_history(with_startup_primer(LEGACY_NO_PLC_RECEIVER_JS))


def instrument_quality(source, mode, candidate=None):
    """Bounded scalar timing only; the intentional stall is a fixture perturbation."""
    if mode not in MODES + STRESS_MODES + FULL_MODES:
        raise ValueError('fixture_delivery_plan')
    if candidate not in (None, 'clock_lead_40ms', 'production'):
        raise ValueError('fixture_playout_candidate')
    source = instrument_history(source)
    ready_call = 'await writePacer.ready();requireOpen();' if candidate == 'production' else 'await writer.ready;requireOpen();'
    write_call = 'await writePacer.write(audio);requireOpen();' if candidate == 'production' else 'await writer.write(audio);requireOpen();'
    changes = (
        ('        const {done,value:frame}=await reader.read();',
         '        const readStart=performance.now();\n'
         '        const {done,value:frame}=await reader.read();\n'
         '        const timing={read_wait_ms:performance.now()-readStart,\n'
         '          packet:frame?.syntheticIndex,read_at_ms:performance.now(),\n'
         '          arrival_ms:frame?.syntheticArrival,rtp_ticks:frame?.timestamp};\n'
         '        if(!done){syntheticPumpSummary.read_packets++;\n'
         '          timing.offered_at_read=syntheticOffered;\n'
         '          timing.queued_at_read=syntheticOffered-syntheticPumpSummary.read_packets;\n'
         '          syntheticPumpSummary.offered_minus_read_sampled_peak=Math.max('
         'syntheticPumpSummary.offered_minus_read_sampled_peak,syntheticOffered-syntheticPumpSummary.read_packets);}\n'
         '        const decodeStart=performance.now();'),
        ('        packetResolve=null;packetReject=null;requireOpen();',
         '        packetResolve=null;packetReject=null;requireOpen();\n'
         '        timing.codec_and_decode_ms=performance.now()-decodeStart;\n'
         '        timing.decode_end_ms=performance.now();'),
        ('            ' + ready_call,
         '            const writeStart=performance.now();\n'
         + ("            if(input.packets===8)await new Promise(r=>setTimeout(r,120));\n"
            if mode == 'writer_stall' else '')
         + ('            await new Promise(r=>setTimeout(r,30));requireOpen();\n'
            if mode in ('slow_consumer', 'full_slow') else '')
         + '            timing.injected_delay_ms=performance.now()-writeStart;\n'
         '            const readyStart=performance.now();\n'
         '            ' + ready_call + '\n'
         '            timing.ready_ms=performance.now()-readyStart;\n'
         '            timing.write_call_ms=performance.now();\n'
         '            timing.context_before_write=ctx.currentTime;\n'
         '            globalThis.syntheticNativeWrite=null;'),
        ('            ' + write_call,
         '            if(syntheticCloseRequested)syntheticWritesAfterClose++;\n'
         '            ' + write_call + '\n'
         '            timing.write_complete_ms=performance.now();\n'
         '            timing.context_after_write=ctx.currentTime;\n'
         '            timing.native_write=globalThis.syntheticNativeWrite;\n'
         '            timing.audio_frames=frames;\n'
         '            timing.write_ms=performance.now()-writeStart;\n'
         '            timing.ingress_to_write_ms=performance.now()-frame.syntheticArrival;\n'
         '            syntheticPumpSummary.writes++;\n'
         '            for(const key of ["read_wait_ms","codec_and_decode_ms","write_ms",'
         '"clock_wait_ms","ingress_to_write_ms"])if(Number.isFinite(timing[key])){\n'
         '              syntheticPumpSummary[key+"_max"]=Math.max('
         'syntheticPumpSummary[key+"_max"]||0,timing[key]);}\n'
         '            if(globalThis.syntheticPumpMetrics.length<2000)'
         'globalThis.syntheticPumpMetrics.push(timing);'),
    )
    for old, new in changes:
        if source.count(old) != 1:
            raise ValueError('quality_test_source_drift')
        source = source.replace(old, new)
    if candidate == 'clock_lead_40ms':
        # Experimental pacing, NOT a native consumer acknowledgement. Do not
        # feed this estimated budget into source/render quiet or drain claims.
        changes = (
            ('  const pending=[],disconnectors=[],clock=createNoPlcRtpClock();',
             '  let syntheticPlayoutHead=null;\n'
             '  const pending=[],disconnectors=[],clock=createNoPlcRtpClock();'),
            ('            const writeStart=performance.now();',
             '            const paceStart=performance.now();\n'
             '            const duration=audio.numberOfFrames/48000;\n'
             '            if(syntheticPlayoutHead===null)syntheticPlayoutHead=ctx.currentTime;\n'
             '            while(Math.max(syntheticPlayoutHead,ctx.currentTime)+duration-ctx.currentTime>'
             'Math.max(.04,duration)+1e-7){\n'
             '              globalThis.syntheticPaceWaiting=true;\n'
             '              try{await new Promise(r=>setTimeout(r,5));requireOpen();}\n'
             '              finally{globalThis.syntheticPaceWaiting=false;}\n'
             '            }\n'
             '            timing.clock_wait_ms=performance.now()-paceStart;\n'
             '            const writeStart=performance.now();'),
            ('            await writer.write(audio);requireOpen();',
             '            const clockBeforeWrite=ctx.currentTime;\n'
             '            await writer.write(audio);requireOpen();\n'
             '            syntheticPlayoutHead=Math.max(syntheticPlayoutHead,clockBeforeWrite)+duration;'),
        )
        for old, new in changes:
            if source.count(old) != 1:
                raise ValueError('quality_test_source_drift')
            source = source.replace(old, new)
    if candidate == 'production':
        before = '        await cancellable(new Promise(resolve=>{timer=setTimeout(()=>{timer=null;resolve();},5);}));'
        if source.count(before) != 1:
            raise ValueError('quality_test_source_drift')
        source = source.replace(before, '        globalThis.syntheticPaceWaiting=true;\n'
            '        try{' + before.strip() + '}finally{globalThis.syntheticPaceWaiting=false;}')
        before = '      await cancellable(writer.write(audio));'
        if source.count(before) != 1:
            raise ValueError('quality_test_source_drift')
        source = source.replace(before,
            '      const nativeBegin=performance.now(),nativeClock=ctx.currentTime;\n'
            + before + '\n'
            '      globalThis.syntheticNativeWrite={begin_ms:nativeBegin,end_ms:performance.now(),\n'
            '        context_before:nativeClock,context_after:ctx.currentTime};')
    return ('globalThis.syntheticPumpMetrics=[];globalThis.syntheticPaceWaiting=false;\n'
        'globalThis.syntheticPumpSummary={read_packets:0,writes:0,offered_minus_read_sampled_peak:0};\n' + source)


def memory_snapshot():
    """Container cgroup includes browser and fixture; not JS-only or per-case peak."""
    result = {}
    for name in ('current', 'peak', 'max'):
        try:
            value = Path('/sys/fs/cgroup/memory.' + name).read_text().strip()
            result[name + '_bytes'] = int(value) if value.isdigit() else None
        except OSError:
            result[name + '_bytes'] = None
    return result


async def check(modes=MODES, candidate=None):
    fixture = Path(__file__).with_name('decoder_audio_check.js').read_text()
    cases = []
    async with Browser() as browser:
        version = browser.browser.version
        for mode in modes:
            if mode == 'cancel_wait' and candidate is None:
                raise ValueError('fixture_cancel_requires_candidate')
            receiver_source = NO_PLC_RECEIVER_JS if candidate == 'production' else BASELINE_RECEIVER_JS
            source = instrument_quality(receiver_source, mode, candidate)
            context = await browser.browser.new_context(permissions=[])
            count = 0

            async def route(request):
                nonlocal count
                count += 1
                if request.request.url != 'https://continuity-offline.invalid/':
                    await request.abort()
                    return
                await request.fulfill(content_type='text/html', body='<!doctype html>',
                    headers={'Content-Security-Policy': "default-src 'none'; "
                        "script-src 'self' 'nonce-synthetic-fixed'; require-trusted-types-for 'script'"})

            try:
                await context.route('**/*', route)
                page = await context.new_page()
                await page.goto('https://continuity-offline.invalid/')
                result = await asyncio.wait_for(page.evaluate('async mode=>{' + source + fixture
                    + 'return await runDecoderAudioCheck("mono",true,mode);}', mode),
                    45 if mode == 'beta_long' else 20)
                result.update(delivery_plan=mode, playout_candidate=candidate, intercepted_requests=count,
                    container_memory=memory_snapshot(),
                    instrumented_source_sha256=hashlib.sha256(source.encode()).hexdigest())
                cases.append(result)
                print(json.dumps({'completed_case': mode}), flush=True)
            finally:
                await context.close()
    return {'scope': 'synthetic encoded Opus push fixture; no ICE/network/microphone/model; '
            'same-thread timers are not a network arrival model; mounted candidate source',
        'architecture': platform.machine(), 'chromium': version,
        'source_sha256': hashlib.sha256(receiver_source.encode()).hexdigest(),
        'fixture_sha256': hashlib.sha256(fixture.encode()).hexdigest(),
        'playout_candidate': candidate,
        'candidate_limit': 'clock-based lead estimate only; not consumption ACK, '
            'not used for quiet/drain; production selects the actual integrated receiver',
        'queue_metric_limit': 'offered_minus_read_sampled_peak is fixture offers minus reads sampled after read; '
            'not an exact queue peak or browser internal queue bound',
        'memory_limit': 'cgroup current/peak includes browser, fixture and earlier cases in this container; '
            'not JS-only allocation or proof of long-run leak freedom',
        'observer_limit': 'native analyser snapshot and bounded scalar pump timings perturb timing; '
            'no exhaustive duplicate-sample proof; late Opus decode prediction can affect later samples',
        'cases': cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--case', choices=MODES + STRESS_MODES + FULL_MODES)
    parser.add_argument('--candidate', choices=('clock_lead_40ms', 'production'))
    args = parser.parse_args()
    result = asyncio.run(check((args.case,) if args.case else MODES, args.candidate))
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    # A counterexample is useful evidence, not a playback qualification PASS.
    return 0 if all(c.get('phase') == 'complete' and c.get('cleanup') and
        all(c['cleanup'].values()) for c in result['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
