/* UI PROTOTYPE ONLY. Synthetic meetings; no persistence, network, model or production controls. */
(() => {
  'use strict';
  const meetings = [
    {id:'product-old',title:'產品協作週會',date:'2026-10-14',start:'14:00',end:'15:00',kind:'series',seriesId:'product',seriesName:'產品協作',status:'ended',topic:'確認企業試用範圍與負責窗口。'},
    {id:'product-prev',title:'產品協作週會',date:'2026-10-21',start:'14:00',end:'15:00',kind:'series',seriesId:'product',seriesName:'產品協作',status:'ended',topic:'企業試用安排、公開版時程與支援責任。'},
    {id:'design-ended',title:'設計評審',date:'2026-10-27',start:'10:30',end:'11:30',kind:'series',seriesId:'design',seriesName:'設計評審',status:'ended',topic:'檢視新手引導與帳號建立流程。'},
    {id:'product-live',title:'產品協作週會',date:'2026-10-28',start:'14:00',end:'15:00',kind:'series',seriesId:'product',seriesName:'產品協作',status:'live',topic:'確認三家客戶的試用準備與本週交付。'},
    {id:'launch-single',title:'官網上線確認',date:'2026-10-29',start:'10:30',end:'11:15',kind:'single',seriesId:null,seriesName:null,status:'upcoming',topic:'確認頁面文案、發布清單與回復方式。'},
    {id:'vendor-single',title:'供應商交接',date:'2026-10-30',start:'15:00',end:'16:00',kind:'single',seriesId:null,seriesName:null,status:'upcoming',topic:'整理交付文件與後續聯繫窗口。'},
    {id:'design-next',title:'設計評審',date:'2026-11-03',start:'10:30',end:'11:30',kind:'series',seriesId:'design',seriesName:'設計評審',status:'upcoming',topic:'確認修訂後的新手引導與錯誤狀態。'}
  ].map(m => ({...m,location:'Microsoft Teams'}));
  const summaries = {
    'product-old': {
      conclusions:['企業試用以既有客戶為優先，不開放公開申請。','由產品與客服共同確認試用支援窗口。'],
      tasks:[{id:'p-old-1',text:'確認三家試用客戶名單',owner:'產品',due:'2026-10-23',status:'completed'},{id:'p-old-2',text:'整理試用流程草案',owner:'設計',due:'2026-10-28',status:'open'}]
    },
    'product-prev': {
      conclusions:['企業試用先開放三家既有客戶；公開版延後至驗收完成。','保留目前報價方案，先確認試用流程與支援責任。'],
      tasks:[{id:'p-1',text:'完成試用帳號建立流程',owner:'產品',due:'2026-10-30',status:'open'},{id:'p-2',text:'交付客服操作文件',owner:'客服',due:'2026-11-02',status:'open'},{id:'p-3',text:'確認三家試用客戶名單',owner:'業務',due:'2026-10-23',status:'completed'}]
    },
    'design-ended': {
      conclusions:['新手引導縮短為三個步驟，保留清楚的返回入口。','錯誤訊息同時說明原因與可採取的下一步。'],
      tasks:[{id:'d-1',text:'交付三步驟引導稿',owner:'設計',due:'2026-10-30',status:'open'},{id:'d-2',text:'補齊帳號重複與連線中斷畫面',owner:'設計',due:'2026-11-02',status:'open'}]
    }
  };
  const initialTranscript = [
    {time:'14:18:06',text:'今天先確認三家客戶的試用準備。目標仍然是十一月六日開始，不調整目前的報價方案。'},
    {time:'14:20:42',text:'帳號建立流程會在這週五，也就是十月三十日交付。這次先處理邀請與首次登入，不把其他功能一起放進來。'},
    {time:'14:23:15',text:'客服操作文件預計十一月二日完成，內容要包含帳號開通、問題回報以及負責窗口。'},
    {time:'14:25:38',text:'公開版還是等驗收完成後再決定時間。先把這三家客戶的使用流程走順，試用遇到的問題逐項記錄。'}
  ];
  const initialQA = [{time:'14:24:10',question:'小白小白，上次決定先讓哪些客戶試用？',answer:'上次決定先開放三家既有客戶試用，公開版延後至驗收完成。目前報價方案維持不變。',source:'10 月 21 日・產品協作週會／前次結論'}];
  const steps = [
    {time:'14:26:12',text:'試用期間的問題由客服統一收集，產品每天確認一次，需要修改的項目會再排優先順序。'},
    {time:'14:26:24',text:'補充一下，帳號流程與操作文件都完成之後，我們會先安排一次內部演練，再請客戶加入。'},
    {time:'14:26:35',qa:{time:'14:26:35',question:'小白小白，帳號流程和客服文件各是什麼時候完成？',answer:'依本場目前記錄，試用帳號建立流程在十月三十日交付，客服操作文件在十一月二日完成。',source:'本場記錄・14:20、14:23'}},
    {time:'14:26:48',text:'內部演練先安排在十一月四日。請產品和客服都參加，確認問題回報能順利接到負責的人。'},
    {time:'14:27:02',text:'如果演練發現阻擋使用的問題，就先處理問題，不為了趕日期讓客戶遇到相同狀況。'},
    {time:'14:27:16',qa:{time:'14:27:16',question:'小白小白，剛才補充的內部演練安排是什麼？',answer:'內部演練暫定十一月四日，由產品與客服參加，確認問題回報與負責窗口。若出現阻擋使用的問題，先處理問題再安排客戶試用。',source:'本場記錄・14:26、14:27'}}
  ];
  const copy = value => JSON.parse(JSON.stringify(value));
  const getMeeting = id => meetings.find(m => m.id === id) || null;
  const getSummary = id => summaries[id] ? copy(summaries[id]) : null;
  function getSeries(id) {
    const m = getMeeting(id);
    return m ? meetings.filter(x => m.seriesId ? x.seriesId === m.seriesId : x.id === id).slice().sort((a,b) => a.date.localeCompare(b.date)) : [];
  }
  function getPrevious(id) {
    const m = getMeeting(id);
    if (!m || !m.seriesId) return null;
    const previous = getSeries(id).filter(x => x.date < m.date && summaries[x.id]).at(-1);
    return previous ? {meetingId:previous.id,date:previous.date,title:previous.title,...getSummary(previous.id)} : null;
  }
  function createFeed(onChange) {
    let timer = null, cursor = 0;
    let state = {transcript:copy(initialTranscript),qa:copy(initialQA),playing:false,finished:false,lastUpdate:'14:26:00'};
    const snapshot = () => copy(state);
    const emit = () => onChange(snapshot());
    const pause = () => {clearInterval(timer);timer=null;state.playing=false;emit();};
    const reset = () => {clearInterval(timer);timer=null;cursor=0;state={transcript:copy(initialTranscript),qa:copy(initialQA),playing:false,finished:false,lastUpdate:'14:26:00'};emit();};
    const play = () => {
      if (state.playing || state.finished) return;
      state.playing=true;emit();
      timer=setInterval(() => {
        const step=steps[cursor++];
        if(step.qa) state.qa.push(copy(step.qa));
        else state.transcript.push({time:step.time,text:step.text});
        state.lastUpdate=step.time;
        if(cursor===steps.length){clearInterval(timer);timer=null;state.playing=false;state.finished=true;}
        emit();
      },2000);
    };
    window.addEventListener('pagehide',() => {clearInterval(timer);},{once:true});
    return {snapshot,play,pause,reset};
  }
  function escape(text) {return String(text ?? '').replace(/[&<>"']/g,c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  window.MeetingPrototype = {today:'2026-10-28',nowLabel:'2026/10/28 14:26',meetings,getMeeting,getPrevious,getSummary,getSeries,initialTranscript:copy(initialTranscript),initialQA:copy(initialQA),createFeed,escape};
  document.addEventListener('keydown',e => {
    if (window.parent===window || !['ArrowLeft','ArrowRight'].includes(e.key) || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if(e.target.closest('input,textarea,select,[contenteditable="true"],[role="tablist"],[role="grid"]')) return;
    window.parent.postMessage({type:'prototype-key',key:e.key},location.protocol==='file:'?'*':location.origin);
  });
})();
