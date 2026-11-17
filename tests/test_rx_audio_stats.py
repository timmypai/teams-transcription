"""Count-only RX evidence with controlled peers; no browser/model/meeting calls."""
import asyncio
import contextlib
import json
import struct
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from teams_transcription.rx_audio_stats import LIVE_EVENT_KEYS, RxAudioSnapshot, nonzero_samples
from teams_transcription.rx_contract import RxContractError, RxRuntime, ScopedSession, confirm_prepared, options
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.storage import Session
from teams_transcription.worker import run_session
from test_rx_contract import Fixture, config, native_transport
from lifecycle_probe import session_events


def browser_report():
    return [{'packets':2, 'frames':64, 'nonzero_samples':3, 'tracks':1, 'dropped':0,
             'playout_errors':0, 'capture_readiness':{'live_attached_tracks':1,
             'muted_live_tracks':1, 'context_state':'running', 'attach_errors':[]},
             'raw_audio':'private', 'SDP':'private', 'url':'private'}]


def transport_report():
    return {'connection':'connected', 'data_channel':'open', 'sent_frames':32,
            'reports':[{'packetsSent':2, 'bytesSent':128, 'ssrc':'private'}], 'SDP':'private'}


class RxSnapshotTests(unittest.TestCase):
    def test_nonzero_is_exact_pcm16_samples_not_nonzero_packet_frames(self):
        self.assertEqual(nonzero_samples(struct.pack('<6h',0,1,0,-1,256,0)),3)
        self.assertEqual(nonzero_samples(b'\x00\x00'*32),0)

    def test_stage_counters_merge_without_changing_error_state_or_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(config())
            session.update(state='failed',error='live_session_expired',diagnostic={'reason':'expired'},rx_ready=False)
            stats=RxAudioSnapshot(session)
            zero=b'\x00\x00'*4
            mixed=struct.pack('<4h',0,-1,256,0)
            for data in (zero,mixed):
                stats.incoming(data)
                stats.queued(data)
                nonzero=stats.consumed(data)
                stats.push_attempted(data)
                if data==zero: stats.push_returned(data,nonzero)
            stats.final_callbacks_completed=1
            stats.observe_browser(browser_report())
            stats.observe_transport(transport_report())
            provider=SimpleNamespace(received_events=dict.fromkeys(LIVE_EVENT_KEYS,0),source_count=2)
            snapshot=stats.persist(phase='rx_capture',queue_depth=0,provider=provider)
            self.assertEqual(snapshot['python']['incoming_frames'],8)
            self.assertEqual(snapshot['python']['queued_nonzero_samples'],2)
            self.assertEqual(snapshot['python']['consumed_nonzero_samples'],2)
            self.assertFalse(snapshot['python']['all_zero'])
            self.assertEqual(snapshot['live_push']['attempted_packets'],2)
            self.assertEqual(snapshot['live_push']['returned_packets'],1)
            self.assertEqual(snapshot['server_events']['normalized_finals'],2)
            self.assertEqual(snapshot['server_events']['persisted_final_callbacks'],1)
            status=session.status()
            self.assertEqual((status['state'],status['error'],status['diagnostic']),('failed','live_session_expired',{'reason':'expired'}))
            self.assertFalse(status['rx_ready'])
            self.assertNotIn('private',json.dumps(snapshot))

    def test_empty_polls_preserve_original_observation_and_unknown_is_not_zero(self):
        with tempfile.TemporaryDirectory() as root:
            stats=RxAudioSnapshot(Session(root).create(config()))
            stats.observe_browser(browser_report())
            stats.observe_transport(transport_report())
            original_browser=dict(stats.browser)
            original_transport=dict(stats.transport)
            stats.observe_browser([])
            stats.observe_transport({})
            self.assertEqual(stats.browser,original_browser)
            self.assertEqual(stats.transport,original_transport)
            stats.observe_browser([{'packets':True,'frames':-1,'capture_readiness':{'context_state':'private'}}])
            stats.observe_transport({'connection':'private','sent_frames':True})
            snapshot=stats.persist(phase='private',queue_depth=0,provider=None)
            self.assertIsNone(snapshot['browser']['packets'])
            self.assertIsNone(snapshot['browser']['frames'])
            self.assertEqual(snapshot['browser']['context_states'],['unknown'])
            self.assertIsNone(snapshot['live_transport']['scheduled_frames'])
            self.assertIsNone(snapshot['live_transport']['outbound_packets'])
            self.assertIsNone(snapshot['python']['all_zero'])
            self.assertFalse(snapshot['server_events']['available'])
            self.assertIsNone(snapshot['server_events']['counts'])
            self.assertNotIn('private',json.dumps(snapshot))

    def test_valid_incoming_can_be_distinguished_from_failed_queue_insertion(self):
        with tempfile.TemporaryDirectory() as root:
            stats=RxAudioSnapshot(Session(root).create(config()))
            stats.incoming(b'\x01\x00')
            stats.python['queue_overflows']+=1
            snapshot=stats.persist(phase='rx_capture',queue_depth=256,provider=None)
            self.assertEqual(snapshot['python']['incoming_frames'],1)
            self.assertEqual(snapshot['python']['queued_frames'],0)
            self.assertEqual(snapshot['python']['queue_overflows'],1)

    def test_native_snapshot_never_promotes_observed_PCM_to_sent_audio(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            stats = RxAudioSnapshot(session, input_mode='native_media')
            stats.incoming(b'\x01\x00' * 32)
            report = native_transport()
            report.update(sent_frames=999, scheduled_queue_ms=999, buffer_gap_count=999,
                raw_audio='private', SDP='private')
            report['native_source'].update(address='private', raw_audio='private')
            stats.observe_transport(report)
            snapshot = stats.persist(phase='rx_capture', queue_depth=None, provider=None)
            self.assertEqual(snapshot['schema_version'], 2)
            self.assertEqual(snapshot['input_mode'], 'native_media')
            self.assertEqual(snapshot['python']['incoming_frames'], 32)
            self.assertEqual(snapshot['python']['consumed_frames'], 0)
            self.assertIsNone(snapshot['python']['queue_depth'])
            self.assertIsNone(snapshot['live_push'])
            self.assertIsNone(snapshot['live_transport']['scheduled_frames'])
            self.assertIsNone(snapshot['live_transport']['scheduled_queue_ms'])
            self.assertIsNone(snapshot['live_transport']['buffer_gap_count'])
            self.assertEqual(snapshot['live_transport']['outbound_packets'], 2)
            self.assertEqual(snapshot['live_transport']['native_source']['packets_received'], 4)
            self.assertTrue(snapshot['live_transport']['native_source']['available'])
            self.assertEqual(snapshot['live_transport']['native_source']['failure_code'], 'none')
            self.assertNotIn('private', json.dumps(snapshot))

    def test_native_unknown_health_remains_unknown_not_connected_zero(self):
        with tempfile.TemporaryDirectory() as root:
            stats = RxAudioSnapshot(Session(root).create(config()), input_mode='native_media')
            stats.observe_transport({'input_mode':'native_media', 'native_source':{
                'ready':'private', 'failed':None, 'live_tracks':True,
                'cleanup_status':['private'], 'packets_received':-1}})
            snapshot = stats.persist(phase='rx_capture', queue_depth=None, provider=None)
            source = snapshot['live_transport']['native_source']
            self.assertIsNone(source['ready'])
            self.assertIsNone(source['failed'])
            self.assertIsNone(source['live_tracks'])
            self.assertIsNone(source['packets_received'])
            self.assertEqual(source['cleanup_status'], 'unknown')
            self.assertNotIn('private', json.dumps(snapshot))

    def test_unavailable_native_observation_does_not_report_zero_transport_or_failure(self):
        with tempfile.TemporaryDirectory() as root:
            stats = RxAudioSnapshot(Session(root).create(config()), input_mode='native_media')
            report = native_transport()
            report['native_source'].update(available=False, ready=False, frame_count=0,
                connected_frames=0, live_tracks=0, packets_received=0, bytes_received=0)
            stats.observe_transport(report)
            snapshot = stats.persist(phase='rx_capture', queue_depth=None, provider=None)
            source = snapshot['live_transport']['native_source']
            self.assertFalse(source['available'])
            self.assertFalse(source['failed'])
            self.assertIsNone(source['live_tracks'])
            self.assertIsNone(source['connected_frames'])
            self.assertIsNone(source['packets_received'])
            self.assertIsNone(source['bytes_received'])
            self.assertEqual(snapshot['live_transport']['outbound_packets'], 2)


    def test_native_cleanup_status_preserves_canonical_resource_receipts(self):
        with tempfile.TemporaryDirectory() as root:
            stats = RxAudioSnapshot(Session(root).create(config()), input_mode='native_media')
            for state in ('idle', 'pending', 'cleaned', 'unknown'):
                with self.subTest(state=state):
                    report = native_transport()
                    report['native_source']['cleanup_status'] = state
                    stats.observe_transport(report)
                    snapshot = stats.persist(phase='cleanup', queue_depth=None, provider=None)
                    self.assertEqual(snapshot['live_transport']['native_source']['cleanup_status'], state)

class RxAudioLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, predicate, task):
        deadline=time.monotonic()+2
        while not predicate():
            self.assertFalse(task.done(), 'controlled worker stopped before observation')
            self.assertLess(time.monotonic(),deadline)
            await asyncio.sleep(.003)

    async def start_confirmed(self,session):
        task=asyncio.create_task(run_session(session,install_signals=False))
        await self.wait_for(lambda: session.status()['state']=='prepared',task)
        generation=session.status()['rx_prepared']['generation']
        confirm_prepared(session,generation,session.config()['rx_owner_principal'])
        return task

    def formal(self,root):
        return Session(root).create(config(duration=None,rx_test_contract=False,rx_session_contract=True))

    async def test_owner_stop_keeps_observed_pcm_separate_from_native_transport(self):
        for zero in (True,False):
            with self.subTest(zero=zero),tempfile.TemporaryDirectory() as root:
                session=self.formal(root)
                pcm=(b'\x00\x00'*32) if zero else struct.pack('<32h',256,*([0]*31))
                report=browser_report()
                report[0].update(packets=1,frames=32,nonzero_samples=0 if zero else 1)
                fixture=Fixture(initial_pcm=pcm,stats_report=report,emit_final=False)
                with fixture.install() as summary,patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL',.005):
                    task=await self.start_confirmed(session)
                    await self.wait_for(lambda: fixture.captures > 0,task)
                    await self.wait_for(lambda: session.status()['rx_audio']['live_transport'] is not None,task)
                    before=session.status()['rx_audio']
                    self.assertTrue(session.status()['rx_ready'])
                    started=time.monotonic()
                    session.stop()
                    self.assertEqual(await asyncio.wait_for(task,1),0)
                    self.assertLess(time.monotonic()-started,10)
                    summary.assert_not_called()
                status=session.status()
                snapshot=status['rx_audio']
                self.assertEqual(snapshot['browser'],before['browser'])
                self.assertEqual(snapshot['browser']['live_tracks'],1)
                self.assertEqual(snapshot['browser']['muted_tracks'],1)
                self.assertEqual(snapshot['python']['incoming_frames'],32)
                self.assertEqual(snapshot['python']['queued_frames'],0)
                self.assertEqual(snapshot['python']['incoming_nonzero_samples'],0 if zero else 1)
                self.assertIsNone(snapshot['python']['all_zero'])
                self.assertIsNone(snapshot['python']['queue_depth'])
                self.assertIsNone(snapshot['live_push'])
                self.assertEqual(snapshot['input_mode'],'native_media')
                self.assertIsNone(snapshot['live_transport']['scheduled_frames'])
                self.assertEqual(snapshot['live_transport'],before['live_transport'])
                self.assertEqual(fixture.pushes,0)
                self.assertEqual(snapshot['live_transport']['outbound_packets'],2)
                self.assertEqual(snapshot['server_events']['persisted_final_callbacks'],0)
                self.assertEqual(status['state'],'stopped')
                self.assertFalse(status['rx_ready'])
                self.assertIsNone(options(session.config())['capture_seconds'])
                self.assertIsNone(options(session.config())['total_seconds'])
                self.assertEqual((fixture.joins,fixture.live_starts),(1,1))
                self.assertFalse((session.path/'audio.wav').exists())
                self.assertNotIn('private',json.dumps(session.status(),ensure_ascii=False))
                self.assertNotIn('capture_complete',json.dumps(session_events(session),ensure_ascii=False))




    async def test_supervisor_failure_keeps_snapshot_and_first_live_error(self):
        with tempfile.TemporaryDirectory() as root:
            session=self.formal(root)
            fixture=Fixture(stats_report=browser_report(),emit_final=False)
            with fixture.install(),patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL',.005):
                task=await self.start_confirmed(session)
                await self.wait_for(lambda: session.status()['rx_audio']['browser'] is not None,task)
                previous=session.status()['rx_audio']['browser']
                fixture.provider.failure=dict(error_code='live_session_expired',channel='rpc',
                    type='thread/realtime/closed',reason='expired',stage='connected')
                fixture.provider.error='live_session_expired'
                self.assertEqual(await asyncio.wait_for(task,1),1)
            self.assertEqual(session.status()['rx_audio']['browser'],previous)
            self.assertEqual(session.status()['error'],'live_session_expired')
            self.assertEqual(session.status()['diagnostic']['reason'],'expired')
            self.assertEqual(session.status()['cleanup_status'],'cleaned')

    async def test_persistence_completion_is_counted_after_atomic_final_write(self):
        with tempfile.TemporaryDirectory() as root:
            session=self.formal(root)
            fixture=Fixture()
            with fixture.install(),patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL',.005):
                task=await self.start_confirmed(session)
                await self.wait_for(lambda: session.status()['rx_audio']['server_events']['persisted_final_callbacks']==1,task)
                session.stop()
                self.assertEqual(await asyncio.wait_for(task,1),0)
            self.assertEqual(len(session.entries()),1)
            self.assertEqual(session.status()['rx_audio']['server_events']['persisted_final_callbacks'],1)

    async def test_hanging_cleanup_retains_snapshot_when_existing_reaper_reports_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            limits=dict(capture_seconds=.2,total_seconds=.8,cleanup_seconds=.15,ready_ttl_seconds=.3)
            session=Session(root).create(config(rx_test_contract=limits))
            fixture=Fixture(hang_close=True,stats_report=browser_report(),emit_final=False)
            with fixture.install(),patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL',.005):
                task=await self.start_confirmed(session)
                await self.wait_for(lambda: session.status()['rx_audio']['browser'] is not None,task)
                started=time.monotonic()
                session.stop()
                self.assertEqual(await asyncio.wait_for(task,1),1)
                self.assertLess(time.monotonic()-started,limits['cleanup_seconds']+.2)
            self.assertEqual(session.status()['rx_audio']['python']['queued_frames'],0)
            self.assertEqual(session.status()['cleanup_status'],'unknown')
            self.assertEqual(session.status()['error'],'rx_cleanup_unknown')
            self.assertIsNotNone(fixture.process.returncode)

    async def fault_cleanup(self,action,*,supervisor=False,marker_failure=False,live_error=False):
        original_init,original_update=RxRuntime.__init__,Session.update
        runtimes=[]
        def capture_runtime(runtime,*args,**kwargs):
            original_init(runtime,*args,**kwargs)
            runtimes.append(runtime)
        def broken_writer(*args,**kwargs):
            raise OSError('private snapshot error')
        def reject_marker(session,**values):
            if set(values)=={'rx_audio_snapshot_status'}:
                raise OSError('private marker error')
            return original_update(session,**values)
        with tempfile.TemporaryDirectory() as root:
            session=self.formal(root)
            fixture=Fixture(emit_final=False)
            with fixture.install(),patch.object(RxRuntime,'__init__',capture_runtime),patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL',.005):
                task=await self.start_confirmed(session)
                await self.wait_for(lambda: session.status().get('rx_ready') is True,task)
                previous=session.status()['rx_audio']
                if supervisor: runtimes[0].audio_snapshot_writer=broken_writer
                with contextlib.ExitStack() as faults:
                    if not supervisor: faults.enter_context(patch.object(RxAudioSnapshot,'persist',broken_writer))
                    if marker_failure: faults.enter_context(patch.object(Session,'update',reject_marker))
                    if live_error:
                        fixture.provider.failure=dict(error_code='live_session_expired',channel='rpc',
                            type='thread/realtime/closed',reason='expired',stage='connected')
                        fixture.provider.error='live_session_expired'
                    started=time.monotonic()
                    if action=='stop':
                        session.stop()
                        self.assertEqual(await asyncio.wait_for(task,1),1 if live_error else 0)
                    else:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task,1)
                    self.assertLess(time.monotonic()-started,10)
            status=session.status()
            self.assertEqual(status['rx_audio_snapshot_status'],'missing')
            self.assertEqual(status['cleanup_status'],'cleaned')
            self.assertEqual(status['state'],'failed' if live_error else 'stopped')
            self.assertEqual(status['error'],'live_session_expired' if live_error else 'rx_stopped' if action=='stop' else 'rx_cancelled')
            if live_error: self.assertEqual(status['diagnostic']['reason'],'expired')
            self.assertTrue({'meeting','live','browser'}.issubset(fixture.closed))
            self.assertFalse(runtimes[0].writes_open)
            with self.assertRaisesRegex(RxContractError,'rx_attempt_closed'):
                ScopedSession(session,runtimes[0]).update(state='recording')
            result=MeetingStore(root).read(session.ident)
            self.assertTrue(result['terminal'])
            self.assertEqual(result['status'],'failed' if live_error else 'stopped')
            if not supervisor: self.assertEqual(status['rx_audio'],previous)
            self.assertNotIn('private',json.dumps(session.status(),ensure_ascii=False))

    async def test_worker_snapshot_oserror_does_not_skip_stop_or_cancel_cleanup(self):
        for action in ('stop','cancel'):
            with self.subTest(action=action): await self.fault_cleanup(action)

    async def test_supervisor_snapshot_oserror_still_closes_writes_and_finalizes(self):
        for action in ('stop','cancel'):
            with self.subTest(action=action): await self.fault_cleanup(action,supervisor=True)

    async def test_failed_best_effort_missing_marker_cannot_abort_cleanup(self):
        await self.fault_cleanup('stop',marker_failure=True)

    async def test_snapshot_failure_cannot_replace_first_live_failure(self):
        await self.fault_cleanup('stop',live_error=True)
