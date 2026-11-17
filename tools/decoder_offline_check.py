"""Synthetic-only real-browser decoder setup check; no meeting, model or config.

Run in the existing sandboxed test image, with network=none and no private mount.
Results contain only fixed setup labels, API flags, hashes and cleanup booleans.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import time

from teams_transcription.browser import Browser
from teams_transcription.b_document_policy import DocumentPolicyCollector
from teams_transcription.b_no_plc_startup import NO_PLC_RECEIVER_JS


CASES = ('unrestricted', 'blob_allowed', 'blob_denied', 'decoder_api_missing')


def case_verified(case):
    """Expected failures are evidence; constructed decoders must prove cleanup."""
    cleanup = case['cleanup']
    return (case['expected_result_matched'] and cleanup['receiver_ended'] is True
        and case['intercepted_requests'] == 1
        and (not case['setup_passed'] or
            cleanup['context_closed'] is True and cleanup['decoder_invalid'] is True))


def native_meter_candidate(source):
    """Test-local intervention; refuse source drift, never change runtime defaults."""
    before="    setupStep='worklet_url';"
    after="    mute=own(ctx.createGain());"
    if source.count(before)!=1 or source.count(after)!=1:
        raise ValueError('offline_candidate_source_drift')
    begin,end=source.index(before),source.index(after)
    source=source[:begin]+("    setupStep='render_meter';\n"
        "    meter=own(createOfflineNativeMeter(ctx,monitorTrack,()=>fail('b_no_plc_meter_failed')));\n"
        "    state.offlineMeter=meter;\n")+source[end:]
    replacements=(
        ("typeof AudioWorkletNode!=='function'", "typeof MediaStreamTrackProcessor!=='function'"),
        ("if(!ctx.audioWorklet||ctx.sampleRate!==48000)", "if(ctx.sampleRate!==48000)"),
        ('startup=createNoPlcStartup(writer,ctx,meter,requireOpen)',
         'startup=createOfflineNativeStartup(writer,ctx,meter,requireOpen)'),
        ('    const cleanup=[];', '    const cleanup=[];if(meter?.closing)cleanup.push(meter.closing);'),
    )
    for old,new in replacements:
        if source.count(old)!=1:raise ValueError('offline_candidate_source_drift')
        source=source.replace(old,new)
    return Path(__file__).with_name('decoder_native_meter_candidate.js').read_text()+source


async def run_case(browser, mode, *, native_meter=False):
    context = await browser.browser.new_context(permissions=[])
    policy = None
    page = None
    requests = 0

    async def route(request):
        nonlocal requests
        requests += 1
        if request.request.url != 'https://decoder-offline.invalid/':
            await request.abort()
            return
        headers = {}
        if mode in ('blob_allowed', 'blob_denied'):
            headers['Content-Security-Policy'] = "default-src 'none'; script-src 'self'" + (
                ' blob:' if mode == 'blob_allowed' else '')
        await request.fulfill(content_type='text/html',
            body='<!doctype html><title>Synthetic decoder check</title>', headers=headers)

    try:
        await context.route('**/*', route)
        page = await context.new_page()
        policy = DocumentPolicyCollector(page)
        await page.goto('https://decoder-offline.invalid/')
        before = time.monotonic()
        receiver_source=native_meter_candidate(NO_PLC_RECEIVER_JS) if native_meter else NO_PLC_RECEIVER_JS
        result = await asyncio.wait_for(page.evaluate(
            'async mode=>{' + receiver_source + r'''
              const pc=new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true});
              const receiver=pc.addTransceiver('audio',{direction:'recvonly'}).receiver;
              const original=globalThis.AudioDecoder;
              let decoder=null,failure=null;
              if(mode==='decoder_api_missing')globalThis.AudioDecoder=undefined;
              try {
                decoder=await createNoPlcReceiver(receiver,()=>{});
                if(!decoder.valid())throw Error('synthetic_decoder_invalid');
                return {setup_passed:true,environment:noPlcSetupEnvironment(receiver,decoder.ctx)};
              } catch(error) {
                failure=error?.bNoPlcSetup;
                if(!failure)throw Error('synthetic_failure_not_classified');
                return {setup_passed:false,failure};
              } finally {
                decoder?.close();await decoder?.closing;pc.close();
                globalThis.AudioDecoder=original;
                window.syntheticCleanup={receiver_ended:receiver.track.readyState==='ended',
                  context_closed:decoder?decoder.ctx.state==='closed':null,
                  decoder_invalid:decoder?!decoder.valid():null};
              }
            }''', mode), 10)
        result.update(case=mode, native_meter_candidate=native_meter,
            elapsed_ms=round((time.monotonic()-before)*1000),
            document_policy=policy.snapshot(page.main_frame),
            cleanup=await page.evaluate('()=>syntheticCleanup'))
        result['expected_result_matched'] = (
            result['setup_passed'] is True if mode in ('unrestricted', 'blob_allowed') or native_meter else
            result['setup_passed'] is False and result['failure']['stage'] ==
            ('browser_api' if mode == 'decoder_api_missing' else 'worklet_module'))
        result['intercepted_requests'] = requests
        return result
    finally:
        if policy is not None:
            await policy.close()
        await context.close()


async def check(*, native_meter=False):
    async with Browser() as browser:
        cases = [await run_case(browser, mode) for mode in CASES]
        if native_meter:
            cases.append(await run_case(browser,'blob_denied',native_meter=True))
        return {'scope': 'synthetic decoder construction only; no ICE/RTP/audio E2E',
            'chromium_version': browser.browser.version,
            'architecture': platform.machine(),
            'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'candidate_helper_sha256': hashlib.sha256(
                Path(__file__).with_name('decoder_native_meter_candidate.js').read_bytes()).hexdigest()
                if native_meter else None,
            'candidate_receiver_sha256': hashlib.sha256(
                native_meter_candidate(NO_PLC_RECEIVER_JS).encode()).hexdigest()
                if native_meter else None,
            'receiver_sha256': hashlib.sha256(NO_PLC_RECEIVER_JS.encode()).hexdigest(),
            'meeting_or_model_called': False, 'credentials_loaded': False,
            'browser_sandbox': True, 'microphone_permissions': [],
            'cases': cases, 'success': all(case_verified(c) for c in cases)}


async def audio_check():
    """Real codecs/native audio, synthetic encoded-stream source; no ICE or RTP."""
    source = native_meter_candidate(NO_PLC_RECEIVER_JS)
    fixture = Path(__file__).with_name('decoder_audio_check.js').read_text()
    async with Browser() as browser:
        results = []
        for mode in ('mono', 'stereo', 'starvation', 'bad_timestamp', 'stop_early',
                     'dropped_packet', 'duplicate_packet', 'timestamp_wrap', 'observer_error',
                     'stop_in_checkpoint', 'meter_control', 'meter_stalled'):
            context = await browser.browser.new_context(permissions=[])
            requests = 0

            async def route(request):
                nonlocal requests
                requests += 1
                if request.request.url != 'https://decoder-offline.invalid/':
                    await request.abort()
                    return
                await request.fulfill(content_type='text/html', body='<!doctype html><title>Synthetic audio</title>',
                    headers={'Content-Security-Policy': "default-src 'none'; script-src 'self'"})

            try:
                await context.route('**/*', route)
                page = await context.new_page()
                await page.goto('https://decoder-offline.invalid/')
                result = await asyncio.wait_for(page.evaluate(
                    'async mode=>{' + source + fixture +
                    "\nreturn mode.startsWith('meter_')?await runMeterDropCheck(mode==='meter_stalled'):await runDecoderAudioCheck(mode);}", mode), 15)
                result['intercepted_requests'] = requests
                results.append(result)
            finally:
                await context.close()
        quiet = {c['mode']: c for c in results if c['mode'].startswith('meter_')}
        counterexample = (quiet.get('meter_control', {}).get('observed_nonzero_frames', 0) > 0
            and quiet['meter_control'].get('errors') == 0
            and quiet.get('meter_stalled', {}).get('observed_nonzero_frames') == 0
            and quiet['meter_stalled'].get('errors') == 0
            and quiet['meter_stalled'].get('stall_injected') is True
            and quiet['meter_stalled']['stall_start_context_seconds'] < quiet['meter_stalled']['tone_start_context_seconds']
            and quiet['meter_stalled']['stall_end_context_seconds'] > quiet['meter_stalled']['tone_end_context_seconds'])
        return {'scope': 'real WebCodecs/native audio; synthetic encoded-stream source; no ICE/RTP',
            'chromium_version': browser.browser.version, 'architecture': platform.machine(),
            'candidate_receiver_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'fixture_sha256': hashlib.sha256(fixture.encode()).hexdigest(),
            'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'candidate_runtime_eligible': False,
            'quiet_loss_counterexample_observed': counterexample,
            'runtime_blocker': 'capture observer can miss scheduled audio; cannot replace trusted render quiet',
            'credentials_loaded': False, 'model_or_meeting_called': False,
            'pcm_exported': False, 'cases': results,
            'experiment_completed': all(audio_case_safe(c) for c in results)}


def audio_case_safe(case):
    cleanup = case['cleanup']
    common = (case.get('phase') == 'complete' and cleanup.get('context_closed') is True
        and case['intercepted_requests'] == 1)
    if case['mode'].startswith('meter_'):
        return common and cleanup.get('tracks_ended') is True
    return common and case.get('native_writes') == 0 and all(cleanup.get(key) is True
        for key in ('input_track_ended', 'decoder_invalid', 'repeat_close_same_promise', 'stream_cancelled'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--native-meter-candidate', action='store_true',
        help='Test-only native observation candidate; never changes installed app defaults')
    parser.add_argument('--audio', action='store_true', help='Real synthetic codec/native-audio experiment')
    args = parser.parse_args()
    result = asyncio.run(audio_check() if args.audio else check(native_meter=args.native_meter_candidate))
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 0 if result.get('experiment_completed', result.get('success', False)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
