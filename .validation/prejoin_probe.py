"""Real Teams prejoin only. URL remains private; Join and Live are prohibited."""
import argparse,asyncio,datetime,json,os,pathlib,time,urllib.parse
from teams_transcription.browser import Browser,MeetingBrowser
from teams_transcription.diagnostics import failure_diagnostic

class JoinBoundaryReached(RuntimeError):pass
class JoinBoundaryNotReady(RuntimeError):pass

class ObservedRadio:
    """Diagnostic proxy: return original values; never change selection."""
    def __init__(self,locator,meeting,frame,roster,deadline,stopped):
        self.locator,self.meeting,self.frame,self.roster=locator,meeting,frame,roster
        self.deadline,self.stopped=deadline,stopped
    def __getattr__(self,name):return getattr(self.locator,name)
    async def sample(self,stage):
        self.meeting._response_read_guard(self.deadline,self.stopped,frame=self.frame,roster=self.roster,stage='radio_readback')
        value=await self.locator.evaluate("""el => ({
            tag: ['INPUT','DIV','BUTTON','LABEL'].includes(el.tagName)?el.tagName:'other',
            type: el.type==='radio'?'radio':'other',
            role: el.getAttribute('role')==='radio'?'radio':'other',
            aria_checked: el.getAttribute('aria-checked')===null?'absent':
                ['true','false','mixed'].includes(el.getAttribute('aria-checked'))?el.getAttribute('aria-checked'):'other',
            native_checked: typeof el.checked==='boolean'?el.checked:null,
            native_disabled: typeof el.disabled==='boolean'?el.disabled:null,
            native_radio: el.matches('input[type=radio]'),
            connected: el.isConnected
        })""")
        self.meeting._response_read_guard(self.deadline,self.stopped,frame=self.frame,roster=self.roster,stage='radio_readback')
        if len(self.meeting.radio_observations)<16:
            self.meeting.radio_observations.append({'stage':stage,**value})
    async def get_attribute(self,key):
        value=await self.locator.get_attribute(key)
        if key=='aria-checked':await self.sample('aria_readback')
        return value
    async def click(self,**kwargs):
        await self.sample('before_click')
        await self.locator.click(**kwargs)
        await self.sample('after_click')

class PrejoinOnlyBrowser(MeetingBrowser):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.radio_observations=[]
        self.boundary={"reached":False,"visible_join_candidates":0,"enabled_join_candidates":0,"join_click_called":False}
    async def _response_radio_scan(self,deadline,stopped,roster):
        visible,exact=await super()._response_radio_scan(deadline,stopped,roster)
        if len(visible)==1 and hasattr(visible[0][0],'evaluate'):
            locator,frame=visible[0]
            visible=[(ObservedRadio(locator,self,frame,roster,deadline,stopped),frame)]
        return visible,exact
    async def _button(self,pattern,timeout=3000,*,join_action=None,action_guard=None):
        if join_action!="join":
            return await super()._button(pattern,timeout,join_action=join_action,action_guard=action_guard)
        # The actual source has selected and reverified unique computer audio.
        # Inspect the fixed Join candidates only; never delegate/call click.
        self._join_action("join","inspecting")
        for frame in tuple(self.page.frames):
            loc=frame.get_by_role("button",name=__import__("re").compile(pattern,__import__("re").I))
            count=await loc.count()
            if count>16:raise JoinBoundaryNotReady("bounded_join_candidate_count")
            for index in range(count):
                item=loc.nth(index)
                visible=await item.is_visible()
                if action_guard:action_guard(frame)
                if not visible:continue
                self.boundary["visible_join_candidates"]+=1
                enabled=await item.is_enabled()
                if action_guard:action_guard(frame)
                self.boundary["enabled_join_candidates"]+=int(enabled)
        verified=self._response_computer_audio_verified is True
        if verified and self.boundary["visible_join_candidates"]==1 and self.boundary["enabled_join_candidates"]==1:
            self.boundary["reached"]=True
            raise JoinBoundaryReached("verified_prejoin_stopped_before_join")
        raise JoinBoundaryNotReady("join_boundary_not_ready")

async def run(check_id,timeout,observe_only=False):
    root=pathlib.Path("/home/timmypai/apps/teams-transcription-candidate-26fb0ac")
    base=pathlib.Path("/run/user/1000/teams-transcription-candidate-26fb0ac")
    private=base/("prejoin-"+check_id)
    private.mkdir(mode=0o700)
    data=json.loads((base/"trial57/input.json").read_text())
    assert data["owner_principal"]=="local:1000" and os.getuid()==1000
    safe_events=[];network_status=[]
    async def event(kind,value):
        if kind=="meeting_join_diagnostics":
            safe_events.append({"type":kind,"join":value.get("join")})
        elif kind in {"joining","joined","waiting_for_admission"}:
            safe_events.append({"type":kind})
    async def no_audio(packet):
        raise RuntimeError("prejoin_capture_forbidden")
    result={"check_id":check_id,"kind":"PREJOIN_ONLY","started_at_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "join_allowed":False,"live_started":False,"models_called":0,"output_enabled":False,
        "counts":{"actual_attempts":"57/100","models":"23/50","prejoin_checks":int(check_id)},
        "raw_url_html_text_names_tokens_saved":False,"result":"FAILED"}
    meeting=None;driver_pid=None;chromium_pids=set()
    try:
        executable=str(pathlib.Path.home()/".cache/ms-playwright/chromium-1243/chrome-linux64/chrome")
        async with Browser(executable) as runtime:
            driver_pid=runtime.playwright._impl_obj._connection._transport._proc.pid
            try:
                cdp=await runtime.browser.new_browser_cdp_session()
                info=await cdp.send("SystemInfo.getProcessInfo")
                chromium_pids.update(int(x["id"]) for x in info["processInfo"])
                await cdp.detach()
            except Exception:pass
            meeting=PrejoinOnlyBrowser(runtime.browser,no_audio,event,capture_audio=True,defer_capture=True,response_mode=True)
            try:
                def response_status(response):
                    parsed=urllib.parse.urlsplit(response.url)
                    if parsed.hostname in {'teams.microsoft.com','teams.cloud.microsoft'} and parsed.path=='/api/authsvc/v1.0/authz/visitor':
                        request=response.request
                        if len(network_status)<16:
                            network_status.append({'endpoint_kind':'teams_visitor_auth',
                                'method':request.method if request.method in {'GET','POST'} else 'other',
                                'status':response.status,'empty_post_body':not bool(request.post_data_buffer)})
                original_new_context=runtime.browser.new_context
                async def observed_context(**kwargs):
                    context=await original_new_context(**kwargs)
                    context.on('response',response_status)
                    return context
                runtime.browser.new_context=observed_context
                await meeting.open(data["meeting_url"])
                meeting._response_join_started_at=time.monotonic()
                receipt={}
                meeting._join_state["computer_audio_wait"]=receipt
                try:
                    await meeting._response_context_counts(receipt,time.monotonic()+10,lambda:False)
                except Exception as exc:
                    result["initial_observation_error_type"]=type(exc).__name__
                result["initial_ui"]=meeting.join_diagnostic()
                if observe_only:
                    until=time.monotonic()+min(timeout,15)
                    while time.monotonic()<until:
                        try:
                            await meeting._response_context_counts(receipt,until,lambda:False)
                        except Exception:pass
                        ui=(meeting.join_diagnostic().get('computer_audio_wait') or {}).get('ui_snapshot') or {}
                        names=ui.get('safe_name_counts',{})
                        if names.get('continue_without_av_button')==1 and names.get('device_preview_dialog_message',0)>0:
                            result["result"]="OBSERVED_DEVICE_PREVIEW_DIALOG"
                            break
                        await asyncio.sleep(.25)
                    result["observation_only"]=True
                else:
                    try:
                        await meeting.join("teams","Meeting transcription recorder",timeout,lambda:False)
                    except JoinBoundaryReached:
                        result["result"]="PASS_JOIN_BOUNDARY"
                    except Exception as exc:
                        result["failure"]=failure_diagnostic(exc,"meeting_join")
                result["computer_audio_verified"]=meeting._response_computer_audio_verified is True
                result["boundary"]=meeting.boundary
                result["final_diagnostic"]=meeting.join_diagnostic()
                result["radio_observations"]=meeting.radio_observations
            finally:
                await meeting.close()
    except Exception as exc:
        result["failure"]=failure_diagnostic(exc,"browser_open")
    finally:
        result["finished_at_utc"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
        result["safe_events"]=safe_events
        result["known_endpoint_status"]=network_status
        result["cleanup"]={"driver_alive":driver_pid is not None and pathlib.Path("/proc",str(driver_pid)).exists(),
            "recorded_chromium_pids_remaining":[pid for pid in chromium_pids if pathlib.Path("/proc",str(pid)).exists()]}
        result["join_dispatched"]=bool((result.get("final_diagnostic") or {}).get("join_dispatch_started"))
        assert result["join_dispatched"] is False
        private.joinpath("result.json").write_text(json.dumps(result,indent=2))
        root.joinpath(".validation/trial57/prejoin-"+check_id+"-result.json").write_text(json.dumps(result,indent=2))
        print(json.dumps(result,indent=2),flush=True)
    return 0 if result["result"] in {"PASS_JOIN_BOUNDARY","OBSERVED_DEVICE_PREVIEW_DIALOG"} else 1

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--check-id",required=True)
    parser.add_argument("--timeout",type=float,default=40)
    parser.add_argument("--observe-only",action="store_true")
    args=parser.parse_args()
    if not args.check_id.isdecimal() or not 1<=int(args.check_id)<=10 or not 1<=args.timeout<=180:
        raise SystemExit("bounded_prejoin_arguments_required")
    return asyncio.run(run(args.check_id,args.timeout,args.observe_only))
if __name__=="__main__":raise SystemExit(main())
