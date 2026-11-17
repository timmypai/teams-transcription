"""Fixed synthetic application receiver tests; no config, OAuth or model entrypoint."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest



def failure_code(trace):
    for marker,code in (('b_no_plc_startup_timeout','startup_timeout'),
            ('fixture_ice_timeout','local_ice_timeout'),('fixture ICE timeout','local_ice_timeout'),
            ('No usable sandbox','sandbox_unavailable'),('SUID sandbox','sandbox_unavailable'),
            ('TimeoutError','timeout'),('AssertionError','assertion_failed')):
        if marker in trace:return code
    return 'unclassified_test_failure'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    os.umask(0o077);args.output.mkdir(mode=0o700)
    root=Path(__file__).resolve().parents[1]
    sys.path[:0]=[str(root/'tests'),str(root)]
    from teams_transcription import browser,b_media_profile,b_browser_tx,b_no_plc_startup
    if any(Path(module.__file__).resolve().parent!=root/'teams_transcription' for module in
            (browser,b_media_profile,b_browser_tx,b_no_plc_startup)):
        raise RuntimeError('application_source_boundary_rejected')
    receiver=b_no_plc_startup.NO_PLC_RECEIVER_JS
    if b_media_profile.NO_PLC_PEER_JS.count(receiver)!=1 or b_browser_tx.TARGET_ANSWER_JS.count(receiver)!=1:
        raise RuntimeError('application_receiver_selection_rejected')
    launched=[]
    original_launch=browser.launch_sandboxed_chromium
    async def observed_launch(*values,**keywords):
        instance=await original_launch(*values,**keywords)
        launched.append(instance.version)
        return instance
    browser.launch_sandboxed_chromium=observed_launch
    # Private process-level redirection also covers browser/driver stderr.
    # These fixed test sources use only synthetic oscillators/peers; no accounts.
    sys.stdout.flush();sys.stderr.flush()
    saved=[os.dup(1),os.dup(2)]
    try:
        with (args.output/'private-tests.log').open('w') as private:
            private.flush();os.dup2(private.fileno(),1);os.dup2(private.fileno(),2)
            suite=unittest.defaultTestLoader.loadTestsFromNames(('test_b_no_plc',
                'test_b_live.IncomingTrackTests.test_native_peer_installs_no_pcm_tap_or_callback',
                'test_b_browser_tx.BrowserTXTests.test_ordered_first_last_and_continuous_middle_repeat_same_native_track'))
            result=unittest.TextTestRunner(stream=private,verbosity=2).run(suite)
            private.flush()
    finally:
        sys.stdout.flush();sys.stderr.flush()
        for number,descriptor in enumerate(saved,1):os.dup2(descriptor,number);os.close(descriptor)
        browser.launch_sandboxed_chromium=original_launch
    report={'schema_version':1,'scope':'source-loaded integrated application receiver; synthetic local browser fixture',
        'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'success':result.wasSuccessful() and bool(launched) and not result.skipped,
        'browser_launches':len(launched),'browser_versions':sorted(set(launched))[:8],
        'actual_browser_executed':bool(launched),'application_source_verified':True,
        'both_application_callers_select_receiver':True,
        'selected_browser_case_paths':['receiver','native_peer_offer','synthetic_owned_relay'],
        'full_application_e2e_verified':False,
        'receiver_js_sha256':hashlib.sha256(receiver.encode()).hexdigest(),
        'failed_test_ids':[case.id()[:200] for case,_ in (result.failures+result.errors)][:16],
        'failure_codes':[{'test_id':case.id()[:200],'code':failure_code(trace)}
            for case,trace in (result.failures+result.errors)[:16]],
        'private_log_uploaded':False,
        'model_called':False,'Oracle_accessed':False,'new_image_tested':False,
        'teams_remote_or_actual_Live_verified':False,'audio_continuity_verified':False}
    (args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    return 0 if report['success'] else 2


if __name__=='__main__':raise SystemExit(main())
