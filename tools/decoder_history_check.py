"""Synthetic policy-equivalent native history investigation, no meeting/model."""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import platform
from types import SimpleNamespace
from unittest.mock import AsyncMock

from teams_transcription.b_no_plc_startup import NO_PLC_RECEIVER_JS as LEGACY
from teams_transcription.b_no_plc_history import NO_PLC_RECEIVER_JS as HISTORY
from teams_transcription.browser import Browser
from teams_transcription.b_native_output import NativeTrackOutput


QUIET_CHECK_JS = r"""
async function checkHistoryQuiet(mode){
  const ctx=new AudioContext({sampleRate:48000});await ctx.resume();
  const nodes=[],disconnect=[];
  const own=node=>{nodes.push(node);disconnect.push(node.disconnect.bind(node));return node;};
  const guard=()=>{if(ctx.state!=='running')throw Error('fixture_context_closed');};
  const meter=createRenderHistory(ctx,own,()=>{},guard),mute=ctx.createGain();mute.gain.value=0;
  meter.connect(mute);mute.connect(ctx.destination);
  const oscillator=ctx.createOscillator(),left=ctx.createGain(),right=ctx.createGain();
  const merger=ctx.createChannelMerger(2);left.gain.value=0;right.gain.value=0;
  oscillator.frequency.value=1000;oscillator.connect(left);oscillator.connect(right);
  left.connect(merger,0,0);right.connect(merger,0,1);merger.connect(meter);oscillator.start();
  const sleep=ms=>new Promise(r=>setTimeout(r,ms)),rows=[],cleanup={};
  try{
    const base=ctx.currentTime,toneStart=base+.08,toneEnd=base+.16;
    left.gain.setValueAtTime(.2,toneStart);left.gain.setValueAtTime(0,toneEnd);
    right.gain.setValueAtTime(mode==='antiphase'?-.2:0,toneStart);
    right.gain.setValueAtTime(0,toneEnd);
    meter.snapshot();await sleep(20);rows.push(meter.snapshot());
    const stallStart=ctx.currentTime;
    await sleep(mode==='long_stall'?1000:220);
    const stallEnd=ctx.currentTime,afterStall=meter.snapshot();rows.push(afterStall);
    const duplicate=meter.snapshot();
    for(let i=0;i<42;i++){await sleep(50);rows.push(meter.snapshot());}
    return {mode,tone_start:toneStart,tone_end:toneEnd,stall_start:stallStart,stall_end:stallEnd,
      after_stall:afterStall,duplicate,rows,
      recovered_quiet:rows.some(r=>r.coverage_valid&&r.quiet_seconds>=.8),cleanup};
  }finally{
    meter.port.close();oscillator.stop();oscillator.disconnect();left.disconnect();right.disconnect();
    merger.disconnect();meter.disconnect();mute.disconnect();for(const fn of disconnect)try{fn();}catch{}
    await ctx.close();cleanup.context_closed=ctx.state==='closed';
  }
}
"""


def instrument_history(source):
    """Read-only test tap of existing native history, no extra sink/pull node."""
    changes = (
        ('  let closed=false,last=null,cached=null,quietStart=null,epoch=0,calibrated=false;',
         '  let syntheticBounds=null;\n  let closed=false,last=null,cached=null,quietStart=null,epoch=0,calibrated=false;'),
        ('    const after=Math.round(ctx.currentTime*rate);',
         '    const after=Math.round(ctx.currentTime*rate);\n    syntheticBounds={before,after};'),
        ('  return inlet;', '  inlet.syntheticRead=()=>Object.assign(buffers.slice(0,2).map(b=>b.slice()),syntheticBounds);\n  return inlet;'),
        ('    meter=own(createRenderHistory(ctx,own,protect,requireOpen));',
         '    meter=own(createRenderHistory(ctx,own,protect,requireOpen));\n    state.syntheticRead=()=>meter.syntheticRead();'),
        ('  return {waitMarker:attempt=>wait(true,attempt),waitQuiet:()=>wait(false),probeValue,close};',
         "  globalThis.syntheticProbeSummary=()=>{const b=new Float32Array(size);analyser.getFloatTimeDomainData(b);"
         "let p=0,n=0,q=0,max=0;for(const v of b){max=Math.max(max,Math.abs(v));"
         "if(Math.abs(v-.125)<1e-6)p++;if(Math.abs(v+.125)<1e-6)n++;if(Math.abs(v-.25)<1e-6)q++;}"
         "b.fill(0);return {positive:p,negative:n,quarter:q,max,context_state:ctx.state};};\n"
         '  return {waitMarker:attempt=>wait(true,attempt),waitQuiet:()=>wait(false),probeValue,close};'),
    )
    for old, new in changes:
        if source.count(old) != 1:
            raise ValueError('history_test_source_drift')
        source = source.replace(old, new)
    return source


def qualification_checks(result):
    """Explicit fixture acceptance, separate from Oracle/remote audio acceptance."""
    cases = {c['mode']: c for c in result['cases']}
    blocked, allowed = cases.get('legacy_blocked', {}), cases.get('legacy_allowed', {})
    checks = {'matrix_complete': set(cases) == set(MODES),
        'policy_failure_reproduced': blocked.get('constructed') is False
            and blocked.get('failure', {}).get('stage') == 'worklet_module',
        'blob_control_constructed': allowed.get('constructed') is True,
        'one_synthetic_document_only': all(c.get('intercepted_requests') == 1 for c in cases.values())}
    for mode in ('mono', 'stereo', 'starvation', 'voiced_then_quiet'):
        case = cases.get(mode, {})
        startup = case.get('startup', {})
        checks[mode + '_source_and_checkpoint'] = (case.get('full_packet_verified_count') == 16
            and case.get('passive_tail_identity_observed') is True
            and startup.get('version') == 'native-history-gated-v2'
            and startup.get('status') == 'native_generator_checkpoint_observed'
            and startup.get('generator_marker_observed') is True
            and startup.get('probe_tail_quiet') is True and startup.get('probe_output_muted') is True
            and case.get('native_writes') == 0 and case.get('errors') == []
            and case.get('cleanup') and all(v is True for v in case['cleanup'].values()))
        if mode != 'voiced_then_quiet':
            checks[mode + '_no_extra_signal'] = (case.get('outside_verified_payload_max_abs') is not None
                and case['outside_verified_payload_max_abs'] <= 1e-6)
    replay = cases.get('voiced_then_quiet', {}).get('production_consumer_replay', {})
    quiet = replay.get('quiet_seconds')
    checks['production_consumer_completed'] = (type(replay.get('completed_at_trace_index')) is int
        and replay.get('render_activity_observed') is True
        and type(quiet) in (int, float) and math.isfinite(quiet) and quiet >= .8
        and replay.get('counter_domain') == 'target_decoded_input_v2'
        and replay.get('render_activity_frames') is None)
    for mode in ('short_stall', 'antiphase'):
        checks[mode + '_not_false_quiet'] = cases.get(mode, {}).get('after_stall', {}).get('active') is True
    long = cases.get('long_stall', {}).get('after_stall', {})
    checks['long_gap_unknown'] = long.get('coverage_valid') is False and long.get('quiet_seconds') == 0
    bad, stop = cases.get('bad_timestamp', {}), cases.get('stop_early', {})
    checks['first_error_preserved'] = (bad.get('errors') == ['b_no_plc_rtp_timestamp']
        and bad.get('stats', {}).get('input', {}).get('packets') == 4 and bad.get('native_writes') == 0)
    checks['early_stop_cleaned'] = (0 < stop.get('stats', {}).get('input', {}).get('packets', 16) < 16
        and stop.get('errors') == [] and stop.get('native_writes') == 0
        and stop.get('cleanup') and all(v is True for v in stop['cleanup'].values()))
    checks['contexts_cleaned'] = all(cases.get(mode, {}).get('cleanup', {}).get('context_closed') is True
        for mode in MODES if not mode.startswith('legacy_'))
    return {key: value is True for key, value in checks.items()}


MODES = ('legacy_blocked', 'legacy_allowed', 'mono', 'stereo', 'voiced_then_quiet', 'starvation',
    'bad_timestamp', 'stop_early', 'short_stall', 'long_stall', 'antiphase')


async def check(modes=MODES):
    fixture = Path(__file__).with_name('decoder_audio_check.js').read_text()
    source = instrument_history(HISTORY)
    results = []
    async with Browser() as browser:
        for mode in modes:
            context = await browser.browser.new_context(permissions=[])
            count = 0

            async def route(request):
                nonlocal count
                count += 1
                if request.request.url != 'https://history-offline.invalid/':
                    await request.abort()
                    return
                csp = "default-src 'none'; script-src 'self' 'nonce-synthetic-fixed'"
                if mode == 'legacy_allowed':
                    csp += ' blob:'
                csp += "; require-trusted-types-for 'script'"
                await request.fulfill(content_type='text/html', body='<!doctype html><title>Synthetic history</title>',
                    headers={'Content-Security-Policy': csp})

            try:
                await context.route('**/*', route)
                page = await context.new_page()
                await page.goto('https://history-offline.invalid/')
                if mode.startswith('legacy_'):
                    code = 'async()=>{' + LEGACY + r'''
                      const pc=new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true});
                      const receiver=pc.addTransceiver('audio',{direction:'recvonly'}).receiver;
                      let decoder;try{decoder=await createNoPlcReceiver(receiver,()=>{});
                        return {constructed:true};}
                      catch(error){return {constructed:false,failure:error.bNoPlcSetup};}
                      finally{decoder?.close();await decoder?.closing;pc.close();}
                    }'''
                    result = await asyncio.wait_for(page.evaluate(code), 15)
                else:
                    code = 'async mode=>{' + source + fixture + QUIET_CHECK_JS + r'''
                      return ['short_stall','long_stall','antiphase'].includes(mode)?
                        await checkHistoryQuiet(mode):await runDecoderAudioCheck(mode,true);
                    }'''
                    result = await asyncio.wait_for(page.evaluate(code, mode), 20)
                result.update(mode=mode, intercepted_requests=count)
                if mode not in ('legacy_blocked', 'legacy_allowed', 'short_stall', 'long_stall', 'antiphase'):
                    result['synthetic_probe_summary'] = await page.evaluate('()=>syntheticProbeSummary()')
                if mode == 'voiced_then_quiet' and result.get('synthetic_media_trace'):
                    trace = iter(result['synthetic_media_trace'])
                    sink = SimpleNamespace(observe=AsyncMock(side_effect=lambda: next(trace)),
                        set_muted=AsyncMock(), pause=AsyncMock(), stop=AsyncMock())
                    output = NativeTrackOutput('synthetic-only', sink)
                    # Scalar completion replay has no actual sender to unmute.
                    # It cannot produce a real target-enable clock receipt.
                    await output.begin_speech('synthetic-only')
                    completion = None
                    for index in range(1, len(result['synthetic_media_trace'])):
                        if await output.observe():
                            completion = index
                            break
                    result['production_consumer_replay'] = {
                        'scope': 'production NativeTrackOutput with actual browser render/target counters and independent decoded reference source trace; synthetic sink, no RTC',
                        'completed_at_trace_index': completion,
                        'render_activity_observed': output.render_activity_observed,
                        'quiet_seconds': output.quiet_received_seconds,
                        'counter_domain': output.counter_domain,
                        'render_activity_frames': output.activity_frames}
                    await output.close()
                results.append(result)
                print(json.dumps({'completed_case': mode}), flush=True)
            finally:
                await context.close()
        result = {'scope': 'policy-equivalent synthetic codecs/native history; no ICE/RTP/meeting/model',
            'architecture': platform.machine(), 'chromium': browser.browser.version,
            'history_source_sha256': hashlib.sha256(HISTORY.encode()).hexdigest(),
            'instrumented_source_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'fixture_sha256': hashlib.sha256(fixture.encode()).hexdigest(),
            'cases': results}
        result['checks'] = qualification_checks(result)
        result['success'] = all(result['checks'].values())
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', choices=MODES)
    args = parser.parse_args()
    result = asyncio.run(check((args.case,) if args.case else MODES))
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
