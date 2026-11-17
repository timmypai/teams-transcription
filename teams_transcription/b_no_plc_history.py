"""Explicit policy-compatible target receiver; legacy/source receiver is unchanged."""
from .b_no_plc_startup import NO_PLC_RECEIVER_JS as BASE_RECEIVER_JS
from .b_render_history import HISTORY_METER_JS


def with_render_history(source):
    before = "    setupStep='worklet_url';"
    after = "    mute=own(ctx.createGain());"
    if source.count(before) != 1 or source.count(after) != 1:
        raise ValueError('history_receiver_source_drift')
    start, end = source.index(before), source.index(after)
    source = source[:start] + ("    setupStep='render_meter';\n"
        "    meter=own(createRenderHistory(ctx,own,protect,requireOpen));\n") + source[end:]
    changes = (
        ("typeof AudioWorkletNode!=='function'||", ''),
        ("if(!ctx.audioWorklet||ctx.sampleRate!==48000)", "if(ctx.sampleRate!==48000)"),
        ('startup=createNoPlcStartup(writer,ctx,meter,requireOpen)',
         'startup=createHistoryStartup(writer,ctx,createGeneratorCheckpoint(ctx,source,own,protect,requireOpen),requireOpen,()=>{\n'
         "      if(!valid())throw Error('b_no_plc_graph_mutated');\n"
         '      renderGate.gain.value=1;gateReady=true;meter.markCheckpoint();});'),
        ('  let startup;', '  let startup,renderGate,gateReady=false;'),
        ('    source.connect(destination);',
         '    renderGate=own(ctx.createGain());renderGate.gain.value=0;\n'
         '    source.connect(renderGate);renderGate.connect(destination);'),
        ('state.source===source&&state.destination===destination&&',
         'state.source===source&&state.destination===destination&&\n'
         '      renderGate.gain.value===(gateReady?1:0)&&'),
        ('[source,destination,monitorSource,meter,mute]', '[source,destination,monitorSource,meter,mute,renderGate]'),
        ('closed=true;initialized=false;clearInterval(timer);startup?.close();',
         'closed=true;initialized=false;if(renderGate)renderGate.gain.value=0;clearInterval(timer);startup?.close();'),
        ('render:{...render}', 'render:closed?{...render}:meter.snapshot()'),
        ('stats(){if(!closed)valid();requestMeter();return',
         'stats(){if(!closed)valid();return'),
        ('requestMeter();timer=setInterval(()=>{if(valid())requestMeter();},50);',
         'timer=setInterval(()=>{valid();},50);'),
    )
    for old, new in changes:
        if source.count(old) != 1:
            raise ValueError('history_receiver_source_drift')
        source = source.replace(old, new)
    return HISTORY_METER_JS + source


NO_PLC_RECEIVER_JS = with_render_history(BASE_RECEIVER_JS)
