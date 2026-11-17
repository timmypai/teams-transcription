// Offline object fixture for the actual project JS. No network or media device.
const vm=require('vm'), readline=require('readline');
let c;
const track=(kind='audio')=>({kind,enabled:false,readyState:'live',id:'fixture-track',
  stop(){this.readyState='ended';}});
function initialize(specs){
  c={document:{},location:{href:'local-fixture'},setInterval,clearInterval,setTimeout,
    addEventListener(){},removeEventListener(){}};c.window=c;
  c.__meeting={peers:specs.map(spec=>{
    const senders=(spec.senders||[null]).map(value=>({track:value?Object.assign(track(value.kind),value):null,
      async replaceTrack(t){this.track=t;}}));
    const pc={connectionState:spec.closed?'closed':'connected',getSenders:()=>senders};
    const transceivers=(spec.transceivers||[]).map(t=>({sender:t.outside_sender?{track:null}:senders[t.sender||0],
      receiver:{track:track(t.kind||'audio')},stopped:t.stopped??false,
      direction:t.direction??'sendrecv',currentDirection:t.currentDirection??null}));
    pc.getTransceivers=()=>transceivers;return pc;
  })};
  vm.createContext(c);
}
readline.createInterface({input:process.stdin}).on('line',async line=>{
  try{
    const m=JSON.parse(line);
    if(m.initialize){initialize(m.initialize);process.stdout.write('{"ok":true,"value":true}\n');return;}
    const fn=vm.runInContext('('+m.script+')',c), value=await fn(m.args);
    process.stdout.write(JSON.stringify({ok:true,value:value??null})+'\n');
  }catch(e){process.stdout.write(JSON.stringify({ok:false,exception_type:e.name})+'\n');}
});
