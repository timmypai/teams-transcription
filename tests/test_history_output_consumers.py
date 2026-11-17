"""History/input domain consumers; synthetic scalar observations only."""
import asyncio
import copy
import json
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock

from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription import b_browser_tx as tx


def history(clock=100000, *, epoch=1, active=False, latest=-1, valid=True,
            fresh=True, quiet_from=None, checkpoint=True):
    through=max(0,clock-512)
    return dict(kind='native-history-v1',clock_frame=clock,coverage_epoch=epoch,
        coverage_valid=valid,fresh=fresh,window_frames=32768,
        peak_rms=1000/32768 if active else 0.,active=active,
        latest_active_block_min_frame=latest,
        quiet_from_frame=quiet_from,quiet_through_frame=through,
        quiet_seconds=0. if quiet_from is None else max(0,through-quiet_from)/48000,
        graph_checkpoint_observed=checkpoint)


class HistoryOutputConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.media=dict(connected=True,frames=0,packets=0,duration=0.,energy=0.,
            source_frames=0,source_packets=0,source_duration=0.,source_energy=0.,
            counter_domain='target_decoded_input_v2',render_evidence=history())
        self.sink=AsyncMock()
        self.sink.observe.side_effect=lambda:copy.deepcopy(self.media)
        self.output=NativeTrackOutput('owned',self.sink,clock=lambda:100.)
        await self.output.begin_speech('owned')

    async def asyncTearDown(self):await self.output.close()

    async def restart(self):
        await self.output.close()
        self.media['render_evidence']=history()
        self.output=NativeTrackOutput('owned',self.sink,clock=lambda:100.)
        await self.output.begin_speech('owned')

    def advance(self,seconds,*,source_rms=0.,target_rms=0.):
        for prefix,rms in (('source_',source_rms),('',target_rms)):
            self.media[prefix+'frames']+=round(seconds*48000)
            self.media[prefix+'duration']+=seconds
            self.media[prefix+'energy']+=(rms/32768)**2*seconds
            self.media[prefix+'packets']+=1

    async def voice(self):
        self.advance(.1,source_rms=1000,target_rms=1000)
        self.media['render_evidence']=history(160000,epoch=2,active=True,latest=150000)
        self.assertFalse(await self.output.observe())

    async def test_target_input_activity_cannot_substitute_for_history_render_activity(self):
        self.advance(.1,source_rms=1000,target_rms=1000)
        self.media['render_evidence']=history(104800,quiet_from=100000)
        self.assertFalse(await self.output.observe())
        self.advance(.9)
        self.media['render_evidence']=history(148000,quiet_from=100000)
        self.assertFalse(await self.output.observe())

    async def test_history_activity_and_safe_quiet_complete_without_invented_render_counters(self):
        await self.voice()
        self.assertTrue(self.output.render_activity_observed)
        self.assertIsNone(self.output.activity_frames)
        self.assertEqual(self.output.counter_domain,'target_decoded_input_v2')
        self.advance(.4)
        self.media['render_evidence']=history(179200,epoch=2,quiet_from=160000)
        self.assertFalse(await self.output.observe())
        self.advance(.5)
        self.media['render_evidence']=history(203200,epoch=2,quiet_from=160000)
        self.assertTrue(await self.output.observe())
        self.assertAlmostEqual(self.output.received_duration,1.)
        self.assertAlmostEqual(self.output.received_energy,(1000/32768)**2*.1)
        self.assertEqual(self.output.frames,48000)
        result=await self.output.end_speech(quiet=True)
        self.assertEqual(result['status'],'quiet')
        self.assertFalse(result['drained'])

    async def test_real_source_must_also_be_quiet(self):
        await self.voice()
        self.advance(1.,source_rms=1000)
        self.media['render_evidence']=history(208000,epoch=2,quiet_from=160000)
        self.assertFalse(await self.output.observe())
        self.assertEqual(self.output.quiet_received_seconds,0.)
        self.advance(.9)
        self.media['render_evidence']=history(251200,epoch=2,quiet_from=160000)
        self.assertTrue(await self.output.observe())

    async def test_quiet_interval_cannot_reach_back_before_observed_render_activity(self):
        await self.voice()
        self.advance(.9)
        self.media['render_evidence']=history(170000,epoch=2,quiet_from=0)
        self.assertFalse(await self.output.observe())
        self.assertLess(self.output.quiet_received_seconds,.8)

    async def test_gap_epoch_and_graph_unknown_do_not_complete_or_abort_the_lease(self):
        for evidence in (history(208000,epoch=3,valid=False),
                history(208000,epoch=3,quiet_from=160000),
                history(208000,epoch=2,quiet_from=160000,checkpoint=False),
                history(208000,epoch=2)):
            with self.subTest(evidence=evidence):
                await self.restart();await self.voice()
                self.advance(1.)
                self.media['render_evidence']=evidence
                self.assertFalse(await self.output.observe())
                self.assertEqual(self.output.quiet_received_seconds,0.)
                self.assertTrue(self.output.active)

    async def test_short_voice_after_turn_notification_survives_first_delayed_poll(self):
        # The lease opened at target frame 100000. turn.created arrives before
        # the short voice at 110000..120000, but IPC delays the next poll until
        # 124000. Its history still proves the voice inside this output lease.
        self.output.quiet_received_seconds=0.
        self.advance(.1,source_rms=1000,target_rms=1000)
        self.media['render_evidence']=history(124000,epoch=2,active=True,latest=119000)
        self.assertFalse(await self.output.observe())
        self.assertTrue(self.output.render_activity_observed)
        self.assertEqual(self.output.quiet_received_seconds,0.)
        self.advance(.9)
        self.media['render_evidence']=history(167200,epoch=2,quiet_from=124000)
        self.assertTrue(await self.output.observe())

    async def test_new_turn_invalidates_both_quiet_tails_but_keeps_current_lease_activity(self):
        await self.voice();self.advance(1.)
        self.media['render_evidence']=history(208000,epoch=2,quiet_from=160000)
        self.assertTrue(await self.output.observe())
        self.output.quiet_received_seconds=0.
        self.media['render_evidence']=history(208000,epoch=2,quiet_from=160000,fresh=False)
        self.assertFalse(await self.output.observe())
        self.assertTrue(self.output.render_activity_observed)
        self.assertEqual(self.output.quiet_received_seconds,0.)
        # Old history cannot immediately restore quiet. Even after the render
        # tail recovers, real source packets must establish a fresh source tail.
        self.media['render_evidence']=history(256000,epoch=2,quiet_from=160000)
        self.assertFalse(await self.output.observe())
        self.assertEqual(self.output.quiet_received_seconds,0.)
        self.advance(.81)
        self.media['render_evidence']=history(294880,epoch=2,quiet_from=160000)
        self.assertTrue(await self.output.observe())

    async def test_new_lease_cannot_inherit_activity_before_unmute_even_after_turn_reset(self):
        await self.voice()
        await self.output.end_speech()
        self.media['render_evidence']=history(208000,epoch=2,active=True,latest=150000)
        await self.output.begin_speech('owned')
        self.output.quiet_received_seconds=0.
        self.advance(.1,source_rms=1000,target_rms=1000)
        self.media['render_evidence']=history(220000,epoch=3,active=True,latest=200000)
        self.assertFalse(await self.output.observe())
        self.assertFalse(self.output.render_activity_observed)
        self.advance(.9)
        self.media['render_evidence']=history(263200,epoch=3,quiet_from=220000)
        self.assertFalse(await self.output.observe())
        self.assertFalse(self.output.render_activity_observed)
        self.advance(.1,source_rms=1000,target_rms=1000)
        self.media['render_evidence']=history(280000,epoch=4,active=True,latest=270000)
        self.assertFalse(await self.output.observe())
        self.assertTrue(self.output.render_activity_observed)
        self.advance(.9)
        self.media['render_evidence']=history(323200,epoch=4,quiet_from=280000)
        self.assertTrue(await self.output.observe())

    async def test_inflight_observation_cannot_restore_quiet_after_turn_notification(self):
        await self.voice();self.advance(.9)
        self.media['render_evidence']=history(203200,epoch=2,quiet_from=160000)
        entered,release=asyncio.Event(),asyncio.Event()

        async def delayed_observation():
            sample=copy.deepcopy(self.media)
            entered.set();await release.wait()
            return sample

        self.sink.observe.side_effect=delayed_observation
        observing=asyncio.create_task(self.output.observe())
        await entered.wait()
        self.output.quiet_received_seconds=0.
        release.set();self.assertFalse(await observing)
        self.sink.observe.side_effect=lambda:copy.deepcopy(self.media)
        # This poll is the first one actually begun after notification. The
        # previous sample and its source silence were captured before it.
        self.media['render_evidence']=history(246400,epoch=2,quiet_from=160000)
        self.assertFalse(await self.output.observe())
        self.assertEqual(self.output.quiet_received_seconds,0.)
        self.advance(.9)
        self.media['render_evidence']=history(289600,epoch=2,quiet_from=160000)
        self.assertTrue(await self.output.observe())

    async def test_first_post_notification_source_delta_cannot_backfill_old_silence(self):
        await self.voice();self.advance(.9)
        self.output.quiet_received_seconds=0.
        self.media['render_evidence']=history(203200,epoch=2,quiet_from=160000)
        self.assertFalse(await self.output.observe())
        # No source packet followed the notification. An advancing render
        # clock alone must not credit the source silence captured before it.
        self.media['render_evidence']=history(246400,epoch=2,quiet_from=160000)
        self.assertFalse(await self.output.observe())
        self.assertEqual(self.output.quiet_received_seconds,0.)
        self.advance(.81)
        self.media['render_evidence']=history(285280,epoch=2,quiet_from=160000)
        self.assertTrue(await self.output.observe())

    async def test_unmute_clock_excludes_muted_voice_after_the_initial_sample(self):
        await self.output.end_speech()
        self.output.start_output()
        self.sink.set_muted.return_value={'counter_domain':'target_decoded_input_v2','clock_frame':130000}
        self.media['render_evidence']['fresh']=False
        await self.output.begin_speech('owned')
        # The initial sample clock is 100000, but track enable happens later.
        self.advance(.1,source_rms=1000,target_rms=1000)
        self.media['render_evidence']=history(140000,epoch=2,active=True,latest=120000)
        self.assertFalse(await self.output.observe())
        self.assertFalse(self.output.render_activity_observed)
        self.media['render_evidence']=history(150000,epoch=3,active=True,latest=140000)
        self.assertFalse(await self.output.observe())
        self.assertTrue(self.output.render_activity_observed)

    async def test_history_unmute_requires_a_valid_nonregressing_native_clock(self):
        for receipt in (None,{}, {'counter_domain':'legacy_render_cumulative_v1','clock_frame':100000},
                {'counter_domain':'target_decoded_input_v2','clock_frame':True},
                {'counter_domain':'target_decoded_input_v2','clock_frame':99999}):
            with self.subTest(receipt=receipt):
                await self.restart()
                await self.output.end_speech()
                self.media['render_evidence']['fresh']=False
                self.output.start_output();self.sink.set_muted.return_value=receipt
                with self.assertRaisesRegex(RuntimeError,'unmute clock'):
                    await self.output.begin_speech('owned')
                self.assertFalse(self.output.active or self.output.enabled)
                self.sink.pause.assert_awaited()

    async def test_duplicate_clock_preserves_proved_quiet_but_cannot_newly_finish(self):
        await self.voice();self.advance(1.)
        self.media['render_evidence']=history(208000,epoch=2,quiet_from=160000)
        self.assertTrue(await self.output.observe())
        before=self.output.quiet_received_seconds
        self.media['render_evidence']['fresh']=False
        self.assertFalse(await self.output.observe())
        self.assertEqual(self.output.quiet_received_seconds,before)

    async def test_negative_activity_lower_bound_is_unknown_for_the_new_scope_not_invalid(self):
        self.advance(.1,source_rms=1000,target_rms=1000)
        self.media['render_evidence']=history(104800,epoch=2,active=True,latest=-1000)
        self.assertFalse(await self.output.observe())
        self.assertTrue(self.output.active)
        self.assertFalse(self.output.render_activity_observed)

    async def test_cached_advanced_clock_does_not_extend_quiet(self):
        await self.voice();self.advance(.4)
        self.media['render_evidence']=history(179200,epoch=2,quiet_from=160000)
        self.assertFalse(await self.output.observe())
        before=self.output.quiet_received_seconds
        self.advance(.5)
        self.media['render_evidence']=history(203200,epoch=2,quiet_from=160000,fresh=False)
        self.assertFalse(await self.output.observe())
        self.assertEqual(self.output.quiet_received_seconds,before)

    async def test_covered_quiet_after_unobserved_active_epochs_does_not_require_another_answer(self):
        await self.voice();self.advance(.4)
        self.media['render_evidence']=history(179200,epoch=8,quiet_from=170000)
        self.assertFalse(await self.output.observe())
        self.advance(.9)
        self.media['render_evidence']=history(222400,epoch=8,quiet_from=170000)
        self.assertTrue(await self.output.observe())

    async def test_invalid_history_and_regressing_clocks_or_epochs_fail_closed(self):
        mutations=[('kind','other'),('fresh',1),('clock_frame',True),('window_frames',32768.),
            ('coverage_epoch',False),('coverage_valid',0),('peak_rms',float('nan')),
            ('peak_rms',1000/32768),('peak_rms',10**1000),
            ('quiet_seconds',float('inf')),('quiet_seconds',1.),('quiet_through_frame',200000),
            ('latest_active_block_min_frame',True),('graph_checkpoint_observed',1),
            ('clock_frame',99999),('coverage_epoch',0)]
        for key,value in mutations:
            with self.subTest(key=key,value=value):
                await self.restart()
                bad=history(104800);bad[key]=value;self.media['render_evidence']=bad
                with self.assertRaises(RuntimeError):await self.output.observe()
                self.assertFalse(self.output.active or self.output.enabled)
                self.sink.pause.assert_awaited()
        await self.restart()
        del self.media['render_evidence']
        with self.assertRaises(RuntimeError):await self.output.observe()


class RelayPage:
    """Synthetic Playwright boundary, exercising the real public sink methods."""
    def __init__(self,sample):
        self.context=object();self.sample=sample;self.mutes=[]

    def is_closed(self):return False

    async def evaluate(self,script,args=None):
        if script==tx.BIND_TX_JS:return {'owner':args['owner'],'meeting_id':args['meetingId']}
        if script==tx.SOURCE_CHECK_JS:return {'frames':2400,'packets':4,'duration':.05,'energy':.01}
        if script==tx.START_TX_JS:return {'muted':True,'track_id':'synthetic'}
        if script==tx.OBSERVE_TX_JS:return copy.deepcopy(self.sample)
        if script==tx.MUTE_TX_JS:
            self.mutes.append(args['muted'])
            if not args['muted']:
                return {'counter_domain':'target_decoded_input_v2','clock_frame':100000}
        if script==tx.STOP_TX_JS:return {'cleaned':True}
        return 'synthetic-sdp'


class HistorySinkConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.media={'connected':True,'frames':4800,'packets':10,'duration':.1,'energy':.02,
            'counter_domain':'target_decoded_input_v2','render_evidence':history()}
        self.target=RelayPage(self.media);self.source=RelayPage({})
        self.sink=tx.BrowserTXSink(self.target,meeting_id='owned')
        await self.sink.bind();await self.sink.attach_live(self.source);await self.sink.start()

    async def asyncTearDown(self):await self.sink.stop()

    async def test_exact_target_input_counts_domain_and_defensive_history_are_preserved(self):
        result=await self.sink.observe()
        self.assertEqual((result['frames'],result['packets'],result['energy'],result['duration']),
            (4800,10,.02,.1))
        self.assertEqual(result['counter_domain'],'target_decoded_input_v2')
        self.assertEqual(result['source_frames'],2400)
        result['render_evidence']['clock_frame']=0
        cached=self.sink.diagnostics();cached['render_evidence']['clock_frame']=0
        self.assertEqual(self.sink.diagnostics()['render_evidence']['clock_frame'],100000)
        self.media['render_evidence']['fresh']=False
        self.assertEqual((await self.sink.observe())['render_evidence']['clock_frame'],100000)

    async def test_unknown_history_never_falls_back_to_input_as_render(self):
        self.media['render_evidence']['kind']='unknown'
        with self.assertRaises(RuntimeError):await self.sink.observe()
        self.assertIn(True,self.target.mutes)
        self.assertFalse(self.sink.diagnostics()['connected'])

    async def test_public_sink_returns_target_enable_clock_after_source_checks(self):
        await self.sink.stop()
        self.sink=tx.BrowserTXSink(self.target,meeting_id='owned',
            dual_channel_policy=AsyncMock(return_value=True))
        await self.sink.bind();await self.sink.attach_live(self.source);await self.sink.start()
        self.assertEqual(await self.sink.set_muted(False),
            {'counter_domain':'target_decoded_input_v2','clock_frame':100000})

    async def test_domain_switch_and_missing_metadata_fail_closed(self):
        await self.sink.observe()
        self.media.pop('counter_domain');self.media.pop('render_evidence')
        with self.assertRaisesRegex(RuntimeError,'counter domain changed'):await self.sink.observe()
        self.assertIn(True,self.target.mutes)


@unittest.skipUnless(shutil.which('node'),'Node fixture unavailable')
class TargetObservationJavaScriptTests(unittest.TestCase):
    def run_observation(self,sample,script_to_run=None):
        script=r'''const fs=require('fs'),sample=JSON.parse(fs.readFileSync(0,'utf8'));
global.document={};global.location={href:'synthetic'};
const owner='owned',track={readyState:'live'},inputTrack={readyState:'live'},
 sender={track},pc={connectionState:'connected',getSenders:()=>[sender]},
 capture={peers:[pc]},receiver={track:inputTrack},
 relay={connectionState:'connected',getReceivers:()=>[receiver]},
 binding={document,meetingId:'owned',href:location.href,capture,pc,sender,original:track,peerIndex:0},
 decoder={receiver,track,ctx:{currentTime:2.5},valid:()=>true,stats:()=>sample},
 state={owner,binding,track,inputTrack,receiver,relay,decoder,sender,installed:true,
  control:{readyState:'open'},revoke(){}};
global.window={__bTXBindings:new Map([[owner,binding]]),__bTXOwnedStates:new Map([[owner,state]]),
 __bMeetingId:'owned',__meeting:capture,__bTX:state};
const observe='''+(script_to_run or tx.OBSERVE_TX_JS)+r''';
Promise.resolve().then(()=>observe({owner,muted:false})).then(result=>process.stdout.write(JSON.stringify(result)))
 .catch(()=>{process.stderr.write('observation_rejected');process.exitCode=1;});'''
        return subprocess.run(['node','-e',script],input=json.dumps(sample),text=True,capture_output=True,timeout=5)

    def test_history_returns_true_input_domain_and_legacy_keeps_render_counts(self):
        input_counts={'frames':4800,'packets':10,'energy':.02,'duration':.1}
        result=self.run_observation({'input':input_counts,'render':history()})
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual({key:value[key] for key in input_counts},input_counts)
        self.assertEqual(value['counter_domain'],'target_decoded_input_v2')
        self.assertEqual(value['render_evidence'],history())
        result=self.run_observation({'input':input_counts,'render':{'frames':9600,'energy':.04,'duration':.2}})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'frames':9600,'energy':.04,'duration':.2,'packets':10,'connected':True})

    def test_unknown_kind_rejects_instead_of_using_decoded_input_as_render(self):
        result=self.run_observation({'input':{'frames':4800,'packets':10,'energy':.02,'duration':.1},
            'render':{**history(),'kind':'unknown'}})
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(result.stderr,'observation_rejected')

    def test_unmute_reports_native_clock_in_the_target_enable_action(self):
        result=self.run_observation({},tx.MUTE_TX_JS)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),
            {'counter_domain':'target_decoded_input_v2','clock_frame':120000})
