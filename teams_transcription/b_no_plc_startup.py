"""Versioned startup initialization for the two owned no-PLC receiver paths.

Builds on the separately verified legacy receiver. It never changes legacy pins.
"""

STARTUP_HELPER_JS = r"""
function createNoPlcStartup(writer,ctx,meter,requireOpen) {
  let complete=false,closed=false,timer=null,abortReject=null,pullResolve=null;
  const receipt={version:'format-primer-v1',status:'not_started'};
  function check(){if(closed)throw Error('b_no_plc_startup_closed');requireOpen();}
  function accept(data){
    if(data?.kind!=='b-no-plc-startup-pull')return false;
    pullResolve?.(data.frame);pullResolve=null;return true;
  }
  function close(){
    closed=true;clearTimeout(timer);timer=null;
    abortReject?.(Error('b_no_plc_startup_closed'));pullResolve=null;
  }
  async function prepare(audio){
    check();if(complete)return;
    const frames=audio.numberOfFrames,channels=audio.numberOfChannels;
    if(audio.sampleRate!==48000||!Number.isInteger(frames)||frames<1||frames>5760||
      ![1,2].includes(channels)||!Number.isSafeInteger(audio.timestamp))
      throw Error('b_no_plc_startup_format');
    const timestamp=audio.timestamp-Math.ceil(frames*1000000/48000);
    const silence=new AudioData({format:'f32-planar',sampleRate:48000,
      numberOfFrames:frames,numberOfChannels:channels,timestamp,
      data:new Float32Array(frames*channels)});
    Object.assign(receipt,{status:'pending',primer_frames:frames,primer_channels:channels,
      primer_timestamp_us:timestamp,first_real_timestamp_us:audio.timestamp,
      context_before_seconds:ctx.currentTime,write_accepted:false});
    const abort=new Promise((_,reject)=>{abortReject=reject;});
    timer=setTimeout(()=>abortReject?.(Error('b_no_plc_startup_timeout')),500);
    try{
      await Promise.race([writer.ready,abort]);check();
      await Promise.race([writer.write(silence),abort]);check();
      receipt.write_accepted=true;
      const target=Math.ceil(ctx.currentTime*48000)+256;
      const pull=new Promise(resolve=>{pullResolve=resolve;
        meter.port.postMessage({kind:'b-no-plc-startup-pull',target});});
      const frame=await Promise.race([pull,abort]);check();
      if(!Number.isSafeInteger(frame)||frame<target)throw Error('b_no_plc_startup_checkpoint');
      Object.assign(receipt,{status:'render_checkpoint_observed',pull_checkpoint_frame:frame,
        context_after_seconds:ctx.currentTime});complete=true;
    }catch(error){receipt.status='failed';throw error;}
    finally{silence.close();clearTimeout(timer);timer=null;abortReject=null;pullResolve=null;}
  }
  return {prepare,accept,close,receipt};
}
"""


def with_startup_primer(source):
    """Apply the one explicit version to a verified receiver, refusing drift."""
    changes = (
        ('  const pending=[],disconnectors=[],clock=createNoPlcRtpClock();',
         '  let startup;\n  const pending=[],disconnectors=[],clock=createNoPlcRtpClock();'),
        ('    closed=true;initialized=false;clearInterval(timer);',
         '    closed=true;initialized=false;clearInterval(timer);startup?.close();'),
        ('          this.port.onmessage=()=>this.port.postMessage({frames:this.frames,',
         "          this.startupTarget=null;\n          this.port.onmessage=e=>{\n            if(e.data?.kind==='b-no-plc-startup-pull'){this.startupTarget=e.data.target;return;}\n            this.port.postMessage({frames:this.frames,"),
        ('            energy:this.energy,duration:this.frames/sampleRate});}',
         '            energy:this.energy,duration:this.frames/sampleRate});};}'),
        ('        process(inputs){',
         "        process(inputs){\n          if(this.startupTarget!==null&&currentFrame>=this.startupTarget){\n            this.port.postMessage({kind:'b-no-plc-startup-pull',frame:currentFrame});\n            this.startupTarget=null;\n          }"),
        ('    meter.port.onmessage=event=>{\n      meterPending=false;',
         '    meter.port.onmessage=event=>{\n      if(startup?.accept(event.data))return;\n      meterPending=false;'),
        ("    meter.onprocessorerror=()=>fail('b_no_plc_meter_failed');",
         "    meter.onprocessorerror=()=>fail('b_no_plc_meter_failed');\n    startup=createNoPlcStartup(writer,ctx,meter,requireOpen);state.startup=startup.receipt;"),
        ('            await writer.ready;requireOpen();',
         '            await startup.prepare(audio);requireOpen();\n            await writer.ready;requireOpen();'),
    )
    for before, after in changes:
        if source.count(before) != 1:
            raise ValueError('startup_receiver_source_drift')
        source = source.replace(before, after)
    return STARTUP_HELPER_JS + source


# Explicit current receiver identity; legacy literal remains separately available.
from .b_no_plc import NO_PLC_RECEIVER_JS as LEGACY_NO_PLC_RECEIVER_JS
from .b_no_plc_pacing import with_clock_pacing

NO_PLC_RECEIVER_JS = with_clock_pacing(with_startup_primer(LEGACY_NO_PLC_RECEIVER_JS))
