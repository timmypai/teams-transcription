"""Explicit native-clock estimate for the two owned no-PLC write paths.

This limits estimated write lead, not the browser's hidden FIFO or consumption.
It never changes RTP/AudioData timestamps, holds an answer queue, or adds PLC.
Apply after with_startup_primer; the history transform remains compatible.
"""

PACING_HELPER_JS = r"""
function createNoPlcWritePacer(writer,ctx,requireOpen) {
  let closed=false,failure=null,busy=false,head=null,lastClock=null,timer=null,abortReject=null;
  const closedError=Error('b_no_plc_pacing_closed');
  function check(){
    if(failure)throw failure;
    if(closed)throw closedError;
    requireOpen();
    if(closed)throw closedError;
    const now=ctx.currentTime;
    if(ctx.state!=='running'||ctx.sampleRate!==48000||!Number.isFinite(now)||now<0||
        (lastClock!==null&&now<lastClock))throw Error('b_no_plc_pacing_clock');
    lastClock=now;return now;
  }
  function close(){
    if(closed)return;
    closed=true;clearTimeout(timer);timer=null;
    abortReject?.(failure||closedError);abortReject=null;
  }
  function cancellable(value){
    return new Promise((resolve,reject)=>{
      let settled=false;
      const finish=(fn,value)=>{
        if(settled)return;settled=true;
        if(abortReject===abort)abortReject=null;
        fn(value);
      };
      const abort=error=>finish(reject,error);
      abortReject=abort;
      Promise.resolve(value).then(value=>finish(resolve,value),error=>finish(reject,error));
      if(closed)abort(failure||closedError);
    });
  }
  async function run(operation){
    if(busy)throw failure||(closed?closedError:Error('b_no_plc_pacing_concurrent'));
    busy=true;
    try{check();return await operation();}
    catch(error){if(!closed&&!failure)failure=error;throw failure||error;}
    finally{clearTimeout(timer);timer=null;abortReject=null;busy=false;}
  }
  async function ready(){
    return run(async()=>{await cancellable(writer.ready);check();});
  }
  async function write(audio){
    return run(async()=>{
      const frames=audio?.numberOfFrames,channels=audio?.numberOfChannels;
      if(audio?.sampleRate!==48000||!Number.isInteger(frames)||frames<1||frames>5760||
          ![1,2].includes(channels)||!Number.isSafeInteger(audio.timestamp))
        throw Error('b_no_plc_pacing_format');
      const duration=frames/48000,lead=Math.max(.04,duration);
      await cancellable(writer.ready);check();
      while(true){
        const now=check();
        if(Math.max(head??now,now)+duration<=now+lead+1e-9)break;
        await cancellable(new Promise(resolve=>{timer=setTimeout(()=>{timer=null;resolve();},5);}));
        check();
      }
      await cancellable(writer.write(audio));
      const now=check();
      // A stalled write reanchors here: never race to recover an old schedule.
      head=Math.max(head??now,now)+duration;
    });
  }
  return {ready,write,close};
}
"""


def with_clock_pacing(source):
    """Transform a startup-enabled receiver exactly, refusing source drift."""
    changes = (
        ('  const pending=[],disconnectors=[],clock=createNoPlcRtpClock();',
         '  let writePacer;\n  const pending=[],disconnectors=[],clock=createNoPlcRtpClock();'),
        ('    closed=true;initialized=false;clearInterval(timer);startup?.close();',
         '    closed=true;initialized=false;clearInterval(timer);startup?.close();writePacer?.close();'),
        ('    writer=generator.writable.getWriter();',
         '    writer=generator.writable.getWriter();\n    writePacer=createNoPlcWritePacer(writer,ctx,requireOpen);'),
        ('            await writer.ready;requireOpen();',
         '            await writePacer.ready();requireOpen();'),
        ('            await writer.write(audio);requireOpen();',
         '            await writePacer.write(audio);requireOpen();'),
    )
    for before, after in changes:
        if source.count(before) != 1:
            raise ValueError('pacing_receiver_source_drift')
        source = source.replace(before, after)
    return PACING_HELPER_JS + source
