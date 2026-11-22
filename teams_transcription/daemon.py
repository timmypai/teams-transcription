"""One autonomous owner; browser requests never own capture or summary tasks."""
import asyncio
import contextlib
import fcntl
import functools
import hashlib
import inspect
import os
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

from .assistant_store import AssistantStore, AssistantStoreError
from .calendar_client import GoogleCalendarClient, CalendarError, validate_teams_url
from .storage import Session
from .rx_contract import confirm_prepared, RxContractError
from .provider_state import CodexProviderState
from .summary import generate, SummaryGenerationError
from .worker import run_session


def error_code(error, fallback):
    if isinstance(error,(AssistantStoreError,CalendarError,RxContractError,SummaryGenerationError)):
        code = getattr(error,'code',None)
        if isinstance(code,str) and re.fullmatch('[a-z0-9_]{1,80}',code):
            return code
    return fallback


class AssistantDaemon:
    def __init__(self, settings, *, store=None, calendar=None, runner=None, summarizer=None, clock=None):
        self.settings = settings
        self.store = store or AssistantStore(settings.data_root)
        self.calendar = calendar or GoogleCalendarClient(settings.google_credentials_file,
            client_config_path=settings.google_client_config_file)
        self.runner = runner or functools.partial(run_session,install_signals=False)
        self.summarizer = summarizer or generate
        self._injected_runner = runner is not None
        self.clock = clock or time.time
        self.owner = 'daemon-'+uuid.uuid4().hex
        self.closing = asyncio.Event()
        self._poll_lock, self._join_lock, self._output_lock = asyncio.Lock(),asyncio.Lock(),asyncio.Lock()
        self._tasks = set()
        self._capture = self._session = self._lease = None
        self._stop_reasons = {}
        self._attempted = set()
        self._lock_fd = None
        self._started = False
        self._capture_interlock = False
        self.last_error = None
        self.last_poll_at = None
        self.calendar_errors = {}
        self._provider = None
        self.auth_operation_in_progress = False

    def _spawn(self, awaitable):
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        def finished(value):
            self._tasks.discard(value)
            if not value.cancelled():
                value.exception()
        task.add_done_callback(finished)
        return task

    def status(self):
        controller = getattr(self._session, 'response_controller', None)
        return {'started':self._started,'closing':self.closing.is_set(),'active_session_id':self._session.ident if self._session else None,
            'background_task_count':len(self._tasks),'last_error':self.last_error,'last_poll_at':self.last_poll_at,
            'calendar_errors':dict(self.calendar_errors),'capture_interlock':self._capture_interlock,
            'google':self.calendar.status(),'provider':self._provider.describe() if self._provider else {'provider_status':'unchecked'},
            'response':controller.status() if controller is not None else None}

    async def start(self):
        if self._started:
            return
        if self.closing.is_set():
            raise AssistantStoreError('daemon_closed')
        fd = os.open(Path(self.settings.data_root)/'.assistant-daemon.lock',os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise AssistantStoreError('daemon_already_running') from None
        self._lock_fd = fd
        try:
            await asyncio.to_thread(self.store.recover_expired)
            await self._refresh_capture_interlock()
            self._started = True
            self._spawn(self._calendar_loop())
            self._spawn(self._schedule_loop())
            # Server-side summary generation was canceled by the operator.
            # Do not consume historical pending outbox rows on startup/restart.
        except BaseException:
            os.close(fd)
            self._lock_fd = None
            raise

    async def _refresh_capture_interlock(self):
        if not self._capture_interlock and await asyncio.to_thread(self.store.capture_interlocks):
            self._capture_interlock = True
            await asyncio.to_thread(self.store.update_settings,{'scheduler_paused':True},'runtime')
            self.last_error = 'capture_cleanup_review_required'

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self.closing.wait(),seconds)
        except TimeoutError:
            pass

    async def _calendar_loop(self):
        while not self.closing.is_set():
            try:
                await self.poll_calendar()
            except Exception as error:
                self.last_error = error_code(error,'calendar_poll_failed')
            await self._sleep(self.settings.calendar_poll_seconds)

    async def poll_calendar(self):
        async with self._poll_lock:
            now = datetime.fromtimestamp(self.clock(),timezone.utc)
            calendars = await asyncio.to_thread(self.store.list_calendars)
            result = {'updated_count':0,'failed_count':0}
            read = dict(attempted=0,succeeded=0,failed=0,event_count=0,completed=False)
            enabled = sum(bool(row['enabled']) for row in calendars)
            codes = set()
            for calendar in calendars:
                if not calendar['enabled'] or self.closing.is_set():
                    continue
                try:
                    read['attempted'] += 1
                    events = await self.calendar.list_events(calendar['calendar_id'],
                        (now-timedelta(seconds=self.settings.lookback_seconds)).isoformat(),
                        (now+timedelta(hours=self.settings.lookahead_hours)).isoformat())
                    read['succeeded'] += 1
                    read['event_count'] += len(events)
                    rows = await asyncio.to_thread(self.store.sync_occurrences,calendar['id'],events,now.isoformat())
                    result['updated_count'] += len(rows)
                    self.calendar_errors.pop(calendar['id'],None)
                    if self._session:
                        run = await asyncio.to_thread(self.store.get_run,self._session.ident)
                        occurrence = await asyncio.to_thread(self.store.get_occurrence,run['occurrence_id'])
                        if occurrence['state']=='cancelled':
                            await self.stop(self._session.ident,actor='calendar-policy',reason='calendar_cancelled')
                except Exception as error:
                    if read['attempted'] > read['succeeded']+read['failed']:
                        read['failed'] += 1
                    codes.add(error_code(error,'calendar_poll_failed'))
                    self.calendar_errors[calendar['id']] = error_code(error,'calendar_poll_failed')
                    result['failed_count'] += 1
            self.last_poll_at = now.isoformat()
            read['completed'] = read['attempted'] == enabled and not self.closing.is_set()
            from .calendar_readback import poll_readback
            result['readback'] = poll_readback(self.calendar.status(), enabled, read, codes)
            return result

    async def _schedule_loop(self):
        while not self.closing.is_set():
            try:
                await asyncio.to_thread(self.store.recover_expired)
                await self._refresh_capture_interlock()
                policy = await asyncio.to_thread(self.store.settings)
                if (not policy['scheduler_paused'] and not self._capture_interlock and self._capture is None
                        and not self.auth_operation_in_progress):
                    for occurrence in await asyncio.to_thread(self.store.due_occurrences,self.clock()):
                        attempt_key = (occurrence['id'],occurrence['revision'])
                        if attempt_key in self._attempted:
                            continue
                        self._attempted.add(attempt_key)
                        try:
                            await self.join(occurrence['id'],actor='calendar-policy')
                        except AssistantStoreError as error:
                            if error.code == 'codex_auth_operation_in_progress':
                                self._attempted.discard(attempt_key)
                            raise
                        break
            except Exception as error:
                self.last_error = error_code(error,'scheduler_failed')
            await self._sleep(1)

    async def _admission(self):
        if self._injected_runner:
            return  # Only trusted constructor-injected fixtures; no runtime switch.
        if self._provider is None:
            root = Path('/dev/shm')/('teams-assistant-'+str(os.getuid())+'-'+hashlib.sha256(
                self.settings.data_root.encode()).hexdigest()[:16])
            root.mkdir(mode=0o700,exist_ok=True)
            if root.is_symlink() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:
                raise AssistantStoreError('private_runtime_required')
            policy = SimpleNamespace(runtime_dir=str(root),codex_bin=self.settings.codex_bin,
                live_provider=self.settings.live_provider,live_model=self.settings.live_model,
                live_cost_policy=self.settings.live_cost_policy)
            self._provider = CodexProviderState(policy)
        if not self._provider.admissible(self._provider.settings):
            await self._provider.refresh()
        if not self._provider.admissible(self._provider.settings):
            raise AssistantStoreError('provider_account_not_admissible')

    async def join(self, occurrence_id, actor='admin', *, acceptance=None, target_control=False):
        from .teams_acceptance import validate_policy,is_owner_immediate
        if acceptance is not None:
            acceptance=validate_policy(acceptance)
            if acceptance['maximum_seconds'] is not None:
                raise AssistantStoreError('teams_owner_stop_policy_required')
            if self.settings.max_duration_seconds is not None:
                raise AssistantStoreError('teams_owner_stop_configuration_required')
        async with self._join_lock:
            if self.auth_operation_in_progress:
                raise AssistantStoreError('codex_auth_operation_in_progress')
            await self._refresh_capture_interlock()
            if not self._started or self.closing.is_set() or self._capture_interlock:
                raise AssistantStoreError('capture_unavailable')
            if self._capture is not None:
                raise AssistantStoreError('meeting_slot_busy')
            if (Path(self.settings.data_root) / '.deployment-maintenance').exists():
                raise AssistantStoreError('deployment_maintenance')
            occurrence = await asyncio.to_thread(self.store.get_occurrence,occurrence_id)
            if occurrence['calendar_ref']:
                calendar = await asyncio.to_thread(self.store.get_calendar,occurrence['calendar_ref'])
                fresh = await self.calendar.get_event(calendar['calendar_id'],occurrence['event_id'])
                await asyncio.to_thread(self.store.sync_occurrences,calendar['id'],[fresh])
                occurrence = await asyncio.to_thread(self.store.get_occurrence,occurrence_id)
            now = self.clock()
            immediate=is_owner_immediate(occurrence)
            if immediate and acceptance is None:
                raise AssistantStoreError('teams_preparation_acceptance_required')
            from .occurrence_window import in_join_window
            if not in_join_window(occurrence,now):
                raise AssistantStoreError('occurrence_outside_join_window')
            validate_teams_url(occurrence.get('join_url'))
            if (Path(self.settings.data_root) / '.deployment-maintenance').exists():
                raise AssistantStoreError('deployment_maintenance')
            if target_control:
                await asyncio.to_thread(self.store.validate_target_consent,occurrence_id,acceptance)
            await self._admission()
            lease = await asyncio.to_thread(self.store.claim_run,occurrence_id,self.owner,self.settings.lease_seconds,
                **({'acceptance':acceptance} if acceptance is not None else {}),
                **({'target_control':True} if target_control else {}))
            if acceptance is not None:occurrence=lease.pop('acceptance_occurrence')
            session = Session(self.settings.data_root,lease['session_id'],lease=lease)
            await asyncio.to_thread(self.store.audit_control,actor,'meeting_join_requested',session.ident)
            self._session, self._lease = session,lease
            self._capture = self._spawn(self._execute(session,lease,occurrence))
            return {'session_id':session.ident,'occurrence_id':occurrence_id,'state':'starting',
                **({'acceptance_request':acceptance['request_sha256']} if acceptance is not None else {})}

    async def review_capture_cleanup(self, session_id, expected_stamp, *, resources_stopped, assistant_left, actor='admin'):
        async with self._join_lock:
            if self.closing.is_set():
                raise AssistantStoreError('capture_unavailable')
            if self._capture is not None or self._session is not None or self._lease is not None:
                raise AssistantStoreError('meeting_slot_busy')
            result = await asyncio.to_thread(self.store.review_capture_cleanup,session_id,expected_stamp,
                resources_stopped=resources_stopped,assistant_left=assistant_left,actor=actor)
            # Stay fail-closed if reloading the persisted guard fails. Review never
            # starts capture, enables speech, or resumes the paused scheduler.
            self._capture_interlock = True
            self._capture_interlock = bool(await asyncio.to_thread(self.store.capture_interlocks))
            if not self._capture_interlock and self.last_error=='capture_cleanup_review_required':
                self.last_error = None
            return result

    def _config(self, run, occurrence):
        settings = self.settings
        runtime = Path('/dev/shm')/('teams-assistant-'+str(os.getuid())+'-'+hashlib.sha256(settings.data_root.encode()).hexdigest()[:16])
        runtime.mkdir(exist_ok=True,mode=0o700)
        directory = runtime/run['session_id']
        if runtime.is_symlink() or directory.is_symlink() or runtime.stat().st_mode&0o077:
            raise AssistantStoreError('private_runtime_required')
        directory.mkdir(mode=0o700,exist_ok=False)
        config = dict(provider='codex',source='meeting',meeting_url=occurrence['join_url'],
            name=settings.display_name,duration=settings.max_duration_seconds,join_timeout=settings.join_timeout_seconds,
            audio_timeout=settings.audio_timeout_seconds,drain_seconds=settings.drain_seconds,save_audio=False,
            codex_bin=settings.codex_bin,chromium=settings.chromium,storage_state=settings.storage_state,
            runtime_dir=str(directory),response_enabled=True,response_output_enabled=False,rx_session_contract=True,
            rx_owner_principal=self.owner,allowed_egress_hosts=list(settings.allowed_egress_hosts),
            prior_context=run['prior_context'],glossary_snapshot=run['glossary_snapshot'])
        if run.get('acceptance_policy') is not None:
            policy=run['acceptance_policy']
            config['teams_acceptance']=policy
        return config

    async def _speaking_policy(self, session_id):
        run = await asyncio.to_thread(self.store.get_run,session_id)
        occurrence = run['occurrence']
        # Only the explicit per-occurrence current-target denial overrides the
        # Calendar authority. Other Calendar consent is re-read live below, not
        # taken from an older occurrence snapshot at the previous poll.
        if occurrence.get('target_speaking_revoked') is True:
            return False
        if occurrence['calendar_ref']:
            calendar = await asyncio.to_thread(self.store.get_calendar,occurrence['calendar_ref'])
            return calendar['enabled'] and calendar['recording_authorized'] and calendar['speaking_authorized']
        return occurrence.get('recording_authorized') and occurrence.get('speaking_authorized')

    async def _monitor(self, session, lease, task, occurrence):
        from .teams_acceptance import is_owner_immediate
        approved = output_approved = False
        deadline = None if is_owner_immediate(occurrence) else datetime.fromisoformat(occurrence['end_at']).timestamp()
        if deadline is not None and self.settings.max_duration_seconds is not None:
            deadline = min(deadline, self.clock()+self.settings.max_duration_seconds)
        while not task.done():
            await asyncio.to_thread(self.store.heartbeat_run,lease,self.settings.lease_seconds)
            state = await asyncio.to_thread(session.status)
            if self.closing.is_set() or deadline is not None and self.clock()>=deadline:
                await self.stop(session.ident,actor='runtime',reason='service_shutdown' if self.closing.is_set() else 'scheduled_end')
            if occurrence['calendar_ref']:
                policy = await asyncio.to_thread(self.store.get_calendar,occurrence['calendar_ref'])
                if not policy['enabled'] or not policy['recording_authorized']:
                    await self.stop(session.ident,actor='calendar-policy',reason='authorization_revoked')
            if state.get('state')=='prepared' and not approved and not await asyncio.to_thread(session.stopped):
                calendar = await asyncio.to_thread(self.store.get_occurrence,occurrence['id'])
                if calendar['state']=='cancelled' or not calendar.get('recording_authorized'):
                    await self.stop(session.ident,actor='calendar-policy',reason='authorization_revoked')
                else:
                    await asyncio.to_thread(confirm_prepared,session,state['rx_prepared']['generation'],self.owner)
                    approved = True
            controller = getattr(session,'response_controller',None)
            if controller is not None and controller.status().get('state') not in {'starting','unavailable','stopped','stop_unknown'}:
                permitted = await self._speaking_policy(session.ident)
                if permitted and not output_approved and not await asyncio.to_thread(session.stopped):
                    await self.set_output(session.ident,True,actor='calendar-policy')
                    output_approved = True
                elif not permitted and output_approved:
                    await self.set_output(session.ident,False,actor='calendar-policy')
                    output_approved = False
            await asyncio.wait({task},timeout=.5)

    async def _execute(self, session, lease, occurrence):
        task = monitor = None
        code = None
        try:
            run = await asyncio.to_thread(self.store.get_run,session.ident)
            await asyncio.to_thread(session.create,self._config(run,occurrence))
            task = self._spawn(self.runner(session))
            monitor = self._spawn(self._monitor(session,lease,task,occurrence))
            done,_ = await asyncio.wait({task,monitor},return_when=asyncio.FIRST_COMPLETED)
            if monitor in done and not task.done():
                exception = monitor.exception()
                if exception:
                    raise exception
                raise AssistantStoreError('capture_monitor_lost')
            await task
        except asyncio.CancelledError:
            code = 'service_shutdown'
            with contextlib.suppress(Exception):
                await asyncio.to_thread(session.stop)
        except Exception as error:
            code = error_code(error,'meeting_execution_failed')
            self.last_error = code
            with contextlib.suppress(Exception):
                await asyncio.to_thread(session.stop)
        finally:
            if task and not task.done():
                done,_ = await asyncio.wait({task},timeout=self.settings.shutdown_seconds)
                if not done:
                    task.cancel()
                    code = 'capture_cleanup_unknown'
                    self._capture_interlock = True
            if monitor and not monitor.done():
                monitor.cancel()
            try:
                state = await asyncio.to_thread(session.status)
            except Exception:
                state = {'state':'failed','cleanup_status':'unknown'}
            if state.get('cleanup_status')=='unknown':
                self._capture_interlock = True
                code = code or 'capture_cleanup_unknown'
            if self._capture_interlock:
                await asyncio.to_thread(self.store.update_settings,{'scheduler_paused':True},'runtime')
            terminal = 'failed' if code or state['state'] in {'failed','recorded_without_transcript'} else 'stopped' if state['state']=='stopped' else 'completed'
            reason = self._stop_reasons.pop(session.ident,None) or state.get('end_reason') or ('capture_failed' if terminal=='failed' else 'capture_complete')
            try:
                await asyncio.to_thread(self.store.finish_run,lease,terminal,reason,code)
            except Exception as error:
                self.last_error = error_code(error,'meeting_finalization_failed')
                self._capture_interlock = True
            self._session = self._lease = self._capture = None

    async def stop(self, session_id, actor='admin', reason='operator_stop'):
        async with self._output_lock:
            session = self._session
            if session is None or session.ident!=session_id:
                raise AssistantStoreError('meeting_not_running')
            await asyncio.to_thread(self.store.request_stop,session_id,actor)
            self._stop_reasons.setdefault(session_id,reason)
            controller = getattr(session,'response_controller',None)
            if controller is not None:
                await controller.disable_output()
            return {'session_id':session_id,'stop_requested':True}

    async def set_output(self, session_id, enabled, actor='admin'):
        if type(enabled) is not bool:
            raise AssistantStoreError('invalid_output_policy')
        async with self._output_lock:
            session, lease = self._session,self._lease
            if session is None or session.ident!=session_id:
                raise AssistantStoreError('meeting_output_unavailable')
            controller = getattr(session,'response_controller',None)
            if controller is None:
                raise AssistantStoreError('meeting_output_unavailable')
            try:
                if self.closing.is_set() or await asyncio.to_thread(self.store.stop_requested,lease):
                    raise AssistantStoreError('meeting_stopping')
                if enabled and not await self._speaking_policy(session_id):
                    raise AssistantStoreError('speaking_authorization_required')
                operation = controller.enable_output() if enabled else controller.disable_output()
                if inspect.isawaitable(operation):
                    await operation
                if self.closing.is_set() or await asyncio.to_thread(self.store.stop_requested,lease):
                    raise AssistantStoreError('meeting_stopping')
                if enabled and not await self._speaking_policy(session_id):
                    raise AssistantStoreError('speaking_authorization_revoked')
                await asyncio.to_thread(session.event,'response_output_authorized',{'enabled':enabled})
                await asyncio.to_thread(self.store.audit_control,actor,'speech_enabled' if enabled else 'speech_muted',session_id)
                return controller.status()
            except BaseException:
                with contextlib.suppress(Exception):
                    await asyncio.shield(controller.disable_output())
                raise

    async def _summary_loop(self):
        while not self.closing.is_set():
            lease = None
            try:
                lease = await asyncio.to_thread(self.store.claim_summary,self.owner,self.settings.summary_lease_seconds)
                if lease:
                    publisher = Session(self.settings.data_root,lease['session_id'],lease=lease)
                    metadata = await asyncio.to_thread(publisher.store.summary_state,publisher.ident)
                    if not metadata or metadata['source_sha256']!=lease['source_sha256'] or not metadata['provider_requirement_met']:
                        runtime = Path(self._config_summary_runtime(publisher.ident))
                        await self._summarize_owned(publisher,runtime,lease)
                    await asyncio.to_thread(self.store.finish_summary,lease)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = error_code(error,'summary_generation_failed')
                self.last_error = code
                if lease:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self.store.finish_summary,lease,code)
            await self._sleep(.5)

    async def _summarize_owned(self, publisher, runtime, lease):
        """Renew the existing fenced lease while generation and cleanup progress."""
        await asyncio.to_thread(self.store.heartbeat_summary,lease,self.settings.summary_lease_seconds)
        task = self._spawn(self.summarizer(publisher,runtime,self.settings.codex_bin,self.settings.summary_model))
        try:
            while not task.done():
                done,_ = await asyncio.wait({task},timeout=self.settings.summary_lease_seconds/3)
                if not done:
                    await asyncio.to_thread(self.store.heartbeat_summary,lease,self.settings.summary_lease_seconds)
            return await task
        finally:
            if not task.done():
                task.cancel()
                done,_ = await asyncio.wait({task},timeout=self.settings.shutdown_seconds)
                if not done:
                    raise SummaryGenerationError('codex_summary_cleanup_unknown')

    def _config_summary_runtime(self, ident):
        root = Path('/dev/shm')/('teams-summary-'+str(os.getuid())+'-'+hashlib.sha256(self.settings.data_root.encode()).hexdigest()[:16])
        if root.is_symlink():
            raise AssistantStoreError('private_runtime_required')
        root.mkdir(mode=0o700,exist_ok=True)
        if root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:
            raise AssistantStoreError('private_runtime_required')
        return root/ident

    async def retry_summary(self, session_id, actor='admin'):
        await asyncio.to_thread(self.store.retry_summary,session_id,actor)
        return {'session_id':session_id,'summary_state':'pending'}

    def google_authorization_url(self, redirect_uri, state):
        return self.calendar.authorization_url(redirect_uri,state)

    async def google_exchange_code(self, code, redirect_uri):
        return await self.calendar.exchange_code(code,redirect_uri)

    async def close(self):
        self.closing.set()
        if self._session:
            with contextlib.suppress(Exception):
                await self.stop(self._session.ident,actor='runtime',reason='service_shutdown')
        tasks = set(self._tasks)
        if tasks:
            done,pending = await asyncio.wait(tasks,timeout=self.settings.shutdown_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending,timeout=self.settings.drain_seconds)
        await self.calendar.close()
        if self._provider:
            await self._provider.close()
        self._started = False
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
