'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const meta = name => document.querySelector(`meta[name="${name}"]`).content;
  const timezone = meta('calendar-timezone');
  const csrf = meta('csrf-token');
  const dayFormatter = new Intl.DateTimeFormat('en-CA', {timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit'});
  const timestampFormatter = new Intl.DateTimeFormat('zh-TW', {timeZone: timezone, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  function localToday() {
    const parts = Object.fromEntries(dayFormatter.formatToParts(new Date()).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  const state = {date: localToday(), today: localToday(), selected: meta('initial-occurrence') || null,
    expanded: false, allTasks: false, tab: 'transcript', calendar: null, detail: null, mutating: false};
  const names = ['日', '一', '二', '三', '四', '五', '六'];
  let alignSelection = Boolean(state.selected);
  let positionCalendar = true;
  const dateValue = value => new Date(`${value}T12:00:00Z`);
  function addDays(value, count) { const d = dateValue(value); d.setUTCDate(d.getUTCDate() + count); return d.toISOString().slice(0, 10); }
  const monday = value => addDays(value, -((dateValue(value).getUTCDay() + 6) % 7));
  function dateLabel(value) {
    if (!value) return '日期未提供';
    const d = dateValue(value);
    return `${d.getUTCFullYear()} 年 ${d.getUTCMonth() + 1} 月 ${d.getUTCDate()} 日（${names[d.getUTCDay()]}）`;
  }
  function timestamp(value) {
    if (!value) return '時間未記錄';
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? '時間格式未確認' : timestampFormatter.format(d);
  }
  function text(node, value) { const next = String(value ?? ''); if (node.textContent !== next) node.textContent = next; }
  function node(tag, className, value) { const result = document.createElement(tag); if (className) result.className = className; if (value !== undefined) text(result, value); return result; }
  function part(parent, key, tag = 'p', className = '') {
    let child = Array.from(parent.children).find(item => item.dataset.part === key);
    if (!child) { child = node(tag, className); child.dataset.part = key; parent.append(child); }
    return child;
  }
  // Stable keyed nodes retain the reader's selection, keyboard focus and scroll position.
  function list(parent, rows, key, update, tag = 'div', className = '') {
    const existing = new Map(Array.from(parent.children).map(child => [child.dataset.key, child]));
    let cursor = parent.firstElementChild;
    rows.forEach((row, index) => {
      const id = String(key(row, index));
      let child = existing.get(id);
      if (!child) { child = node(tag, className); child.dataset.key = id; }
      existing.delete(id);
      if (child !== cursor) parent.insertBefore(child, cursor);
      update(child, row, index);
      cursor = child.nextElementSibling;
    });
    existing.forEach(child => child.remove());
  }
  function preserveReading(render) {
    const containers = [document.scrollingElement, ...document.querySelectorAll('.reading-panel,.calendar-scroll')];
    const positions = containers.map(item => [item, item.scrollTop, item.scrollLeft]);
    render();
    positions.forEach(([item, top, left]) => { item.scrollTop = top; item.scrollLeft = left; });
  }
  const historyURL = id => `/history/${encodeURIComponent(id)}`;
  function sources(parent, values, session) {
    const links = part(parent, 'sources', 'div', 'source-links');
    list(links, values || [], (value, index) => `${value}:${index}`, (link, value, index) => {
      text(link, values.length === 1 ? '查看來源' : `來源 ${index + 1}`);
      if (session) link.href = `${historyURL(session)}#source-${encodeURIComponent(value)}`;
      else link.removeAttribute('href');
    }, 'a');
  }
  function sourceReferences(parent, item, session) {
    const refs = Array.isArray(item.source_refs) ? item.source_refs : (item.sources || []).map(id => ({source_id: id, session_id: session}));
    const links = part(parent, 'sources', 'div', 'source-links');
    list(links, refs, (ref, index) => `${ref.session_id}:${ref.source_id}:${index}`, (link, ref, index) => {
      text(link, refs.length === 1 ? '查看來源' : `來源 ${index + 1}`);
      if (ref.session_id && ref.source_id) link.href = `${historyURL(ref.session_id)}#source-${encodeURIComponent(ref.source_id)}`;
      else link.removeAttribute('href');
    }, 'a');
  }
  const sourceNames = {provider_final: '來源已定稿', application_segment: '應用程式分段・非來源定稿',
    legacy_provisional: '歷史暫存・未定稿', provider_final_only: '僅含來源定稿', includes_unfinalized: '包含未定稿文字',
    unfinalized: '未定稿', legacy_truncated: '歷史暫存曾截斷', historical_unverified: '歷史資料未驗證', no_text: '尚無文字',
    observed: '已觀察文字・未確認定稿', provisional: '暫定文字', partial: '暫定文字', streaming: '文字接收中',
    delta_unassociated: '暫定文字・未對應問題', final_unassociated: '來源文字已定稿・未對應問題',
    final: '文字已定稿', admitted: '已接收提問', submitted: '已送出提問', pending: '等待處理', failed: '處理失敗',
    interrupted: '已中斷', completed: '記錄已完成', complete: '記錄完整', incomplete: '記錄未完整', unknown: '狀態未確認'};
  const humanState = value => sourceNames[value] || (value ? '來源狀態待確認' : '狀態未記錄');
  function meetingTime(meeting) {
    if (!meeting.start_date) return '日期待確認';
    const start = meeting.start_time || '時間待確認';
    const end = meeting.end_time ? ` – ${meeting.end_date && meeting.end_date !== meeting.start_date ? meeting.end_date + ' ' : ''}${meeting.end_time}` : '';
    return `${meeting.start_date} ${start}${end}`;
  }
  function overlaps(meeting, date) {
    if (!meeting.start_date) return false;
    const end = meeting.end_date || meeting.start_date;
    return meeting.start_date <= date && (end > date || (end === date && (meeting.end_time !== '00:00' || meeting.start_date === date)));
  }
  const hourHeight = 76;
  const minutes = time => time && /^\d{2}:\d{2}$/.test(time) ? Number(time.slice(0, 2)) * 60 + Number(time.slice(3)) : null;
  function dayLayout(meetings, day) {
    const untimed = [], timed = [];
    meetings.filter(meeting => overlaps(meeting, day)).forEach(meeting => {
      const start = meeting.start_date < day ? 0 : minutes(meeting.start_time);
      if (start === null) { untimed.push(meeting); return; }
      const end = meeting.end_date > day ? 1440 : minutes(meeting.end_time);
      const bottom = end === null ? start + 60 : Math.max(start + 1, end);
      timed.push({meeting, start, end: Math.min(1440, bottom), visualEnd: Math.min(1440, Math.max(bottom, start + 60))});
    });
    timed.sort((a, b) => a.start - b.start || a.end - b.end || a.meeting.id.localeCompare(b.meeting.id));
    const lanes = [];
    timed.forEach(item => {
      let lane = lanes.findIndex(end => end <= item.start);
      if (lane < 0) lane = lanes.length;
      lanes[lane] = item.visualEnd; item.lane = lane;
    });
    return {day, timed, untimed, lanes: Math.max(1, lanes.length)};
  }
  function setExpanded(value, focus = false) {
    state.expanded = value;
    $('workspace').classList.toggle('expanded', value);
    text($('page-title'), value ? '會議工作區' : '行事曆');
    text($('inspector-kicker'), value ? '會議閱讀工作區' : '會議預覽');
    text($('expand-meeting'), value ? '← 返回行事曆' : '展開會議 ↗');
    $('expand-meeting').setAttribute('aria-expanded', String(value));
    if (focus) {
      const target = !value && window.matchMedia('(max-width:850px)').matches ? $('mobile-open') : $('expand-meeting');
      if (value) $('inspector').scrollIntoView({block: 'start'});
      else target.scrollIntoView({block:'nearest'});
      target.focus({preventScroll:true});
    }
  }
  function renderCalendar() {
    const data = state.calendar;
    const start = monday(state.date);
    text($('period-title'), `${start} — ${addDays(start, 6)}`);
    if (document.activeElement !== $('selected-date')) $('selected-date').value = state.date;
    text($('today-label'), dateLabel(state.today));
    text($('timezone-label'), `會議時間 · ${timezone}`);
    const days = Array.from({length: 7}, (_, index) => addDays(start, index));
    const layouts = days.map(day => dayLayout(data?.meetings || [], day));
    const floatingHeight = Math.max(0, ...layouts.map(layout => layout.untimed.length)) * 88;
    const widths = layouts.map(layout => Math.max(130, layout.lanes * 106));
    $('week-grid').style.gridTemplateColumns = `54px ${widths.map(width => `minmax(${width}px,1fr)`).join(' ')}`;
    $('week-grid').style.minWidth = `${54 + widths.reduce((sum, width) => sum + width, 0)}px`;
    function eventButton(button, meeting, day) {
      button.type = 'button'; button.dataset.meeting = meeting.id;
      button.classList.toggle('is-live', Boolean(meeting.is_live));
      button.setAttribute('aria-pressed', String(state.selected === meeting.id));
      const startTime = meeting.start_date < day ? '跨日延續' : (meeting.start_time || '時間待確認');
      const endTime = meeting.end_date > day ? '跨日' : (meeting.end_time || '');
      text(part(button, 'time', 'span', 'event-time'), `${startTime}${endTime ? '–' + endTime : ''}`);
      text(part(button, 'title', 'strong'), meeting.title || '未命名會議');
      text(part(button, 'state', 'span', 'event-status'), `${meeting.state_label || '狀態未確認'} · ${meeting.kind === 'series' ? '系列' : '單場'}`);
      const label = `${meeting.title}，${meetingTime(meeting)}，${meeting.state_label || '狀態未確認'}，${meeting.kind === 'series' ? '系列會議' : '單場會議'}`;
      button.setAttribute('aria-label', label); button.title = label;
    }
    list($('week-grid'), [{day: 'axis'}, ...layouts], row => row.day, (column, layout) => {
      const day = layout.day, axis = day === 'axis';
      column.dataset.day = day;
      column.classList.toggle('time-axis', axis);
      column.classList.toggle('is-today', day === state.today);
      column.classList.toggle('is-selected-day', day === state.date);
      const heading = part(column, 'heading', 'button', 'day-heading');
      heading.type = 'button';
      if (axis) {
        heading.disabled = true; text(heading, '時間');
      } else {
        heading.dataset.date = day;
        heading.setAttribute('aria-pressed', String(day === state.date));
        text(part(heading, 'weekday', 'span'), `週${names[dateValue(day).getUTCDay()]}`);
        text(part(heading, 'date', 'span', 'day-number'), dateValue(day).getUTCDate());
        heading.setAttribute('aria-label', `選擇 ${dateLabel(day)}`);
      }
      const floating = part(column, 'floating', 'div', 'day-floating');
      floating.style.height = `${floatingHeight}px`; floating.hidden = !floatingHeight;
      if (axis) text(floating, '時間未定');
      else list(floating, layout.untimed, meeting => meeting.id, (button, meeting) => eventButton(button, meeting, day), 'button', 'event untimed-event');
      const events = part(column, 'events', 'div', 'day-events');
      events.style.height = `${24 * hourHeight}px`;
      if (axis) {
        list(events, Array.from({length: 24}, (_, index) => index), hour => hour, (label, hour) => {
          label.style.top = `${hour * hourHeight}px`; text(label, `${String(hour).padStart(2, '0')}:00`);
        }, 'span', 'hour-label');
      } else {
        list(events, layout.timed, item => item.meeting.id, (button, item) => {
          eventButton(button, item.meeting, day);
          button.style.top = `${item.start / 60 * hourHeight}px`;
          button.style.height = `${Math.max(1, item.end - item.start) / 60 * hourHeight}px`;
          button.style.left = `calc(${item.lane / layout.lanes * 100}% + 3px)`;
          button.style.width = `calc(${100 / layout.lanes}% - 6px)`;
        }, 'button', 'event timed-event');
      }
      const empty = part(column, 'empty', 'p', 'day-empty');
      text(empty, data ? '沒有會議' : '讀取中…');
      empty.hidden = axis || Boolean(layout.timed.length || layout.untimed.length);
    }, 'section', 'day-column');
    text($('calendar-count'), data ? `${data.meetings.length} 場會議 · 七日週視圖 · ${timezone}` : '正在讀取會議…');
    const selected = state.detail?.meeting || data?.meetings.find(meeting => meeting.id === state.selected);
    text($('mobile-title'), selected?.title || '選擇會議查看內容');
    $('mobile-open').disabled = !state.selected;
    if (!data) return;
    const connection = [data.scheduler_paused ? '自動排程已暫停' : '自動排程已啟用',
      data.google?.authorized ? 'Google 已授權' : 'Google 尚未授權'];
    if (data.google?.error_code) connection.push('行事曆連線異常，請查看設定');
    if (data.capture_interlock) connection.push('擷取清理待確認，已阻擋新擷取');
    if (data.active_session_id) connection.push('有執行中的助理工作階段');
    if (data.last_error) connection.push('服務有待處理錯誤，請查看設定');
    if (data.undated_count > 0) connection.push(`${data.undated_count} 場日期待確認，請至排程檢查`);
    text($('connection-status'), connection.join(' · '));
    const schedBtn = $('toggle-scheduler');
    if (schedBtn) {
      schedBtn.hidden = false;
      schedBtn.disabled = state.mutating || (data.scheduler_paused && Boolean(data.capture_interlock));
      schedBtn.classList.toggle('is-active', !data.scheduler_paused);
      text(schedBtn, data.scheduler_paused ? '啟用自動入會' : '暫停自動入會');
    }
  }
  function renderTasks(detail) {
    const series = detail.series_state;
    const all = series ? [...(detail.task_workspace?.tasks || series.open_tasks || []), ...(detail.task_workspace?.closed_tasks || [])] : detail.tasks || [];
    const closed = new Set(['completed', 'cancelled', 'superseded']);
    const tasks = all.filter(task => !closed.has(task.status) || state.allTasks);
    text($('tasks-note'), series ? tasks.length ? '建議需確認承接；待處理不代表正在進行。未提及仍保留，逾期或久未更新只提醒。' : '本版沒有未完成事項。' : tasks.length ? '狀態取自先前系列記憶；更新會實際儲存。' : all.length ? '目前沒有未完成事項。' : '沒有已記錄的系列待辦。');
    $('toggle-tasks').hidden = Boolean(series && !detail.task_workspace?.closed_tasks?.length);
    text($('toggle-tasks'), state.allTasks ? '僅看未完成' : '包含已完成');
    $('toggle-tasks').setAttribute('aria-pressed', String(state.allTasks));
    list($('task-list'), tasks, task => task.id, (row, task) => {
      row.classList.toggle('is-completed', task.status === 'completed');
      text(part(row, 'text', 'p', 'task-text'), task.text);
      text(part(row, 'meta', 'p', 'task-meta'), `${task.owner || '未指定負責人'} · ${task.date ? '期限 ' + task.date : '未設定期限'}`);
      const select = Array.from(row.children).find(item => item.dataset.part === 'status');
      if (series) {
        select?.remove();
        text(part(row, 'state', 'p', 'task-state'), `${task.display_status || '待確認'} · ${task.commitment_label || 'AI 建議／待確認'}`);
        text(part(row, 'reasons', 'p', 'task-meta'), (task.confirmation_reasons || []).join('；'));
        text(part(row, 'updated', 'p', 'task-meta'), `最近確認：${task.last_evidenced_at ? timestamp(task.last_evidenced_at) : '尚無紀錄'}`);
        const edit = part(row, 'edit', 'a', 'task-edit-link');
        edit.href = `/tasks/${encodeURIComponent(series.series_id)}/${encodeURIComponent(task.id)}`;
        text(edit, closed.has(task.status) ? '查看完成依據／歷史' : '確認承接／更新');
      } else {
        select?.remove();
        text(part(row, 'state', 'p', 'task-meta'), '先前保存的待辦紀錄 · 承接與完成依據待核實');
        for (const key of ['edit','updated','reasons']) Array.from(row.children).find(item => item.dataset.part === key)?.remove();
      }
      sourceReferences(row, task, task.origin_session_id);
      const origin = part(row, 'origin', 'a', 'task-meta');
      origin.hidden = !task.origin_session_id;
      if (task.origin_session_id) { origin.href = historyURL(task.origin_session_id); text(origin, ' 查看原始會議 ↗'); }
    }, 'article', 'task-row');
  }
  function renderSeriesState(detail) {
    const series = detail.series_state;
    text($('series-revision'), series ? `第 ${series.revision} 版` : '尚無有效版本');
    const latest = detail.latest_recorded_meeting;
    const pendingNewer = latest && series && new Date(latest.start_at) > new Date(series.occurred_at);
    text($('series-state-note'), series ? `最新結論來自 ${timestamp(series.occurred_at)} 的會議 · ${series.publication_kind === 'human_task_action' ? '含人工待辦確認' : '已整理'}。${pendingNewer ? `最近一場 ${timestamp(latest.start_at)} 尚待整理，以下保留前場有效結論。` : ''}` : '尚無已發佈的系列最新狀態。先前摘要可從下方歷史脈絡查閱。');
    $('series-state-link').hidden = !series?.session_id;
    if (series?.session_id) $('series-state-link').href = historyURL(series.session_id);
    const conclusions = series?.conclusions || [];
    list($('series-conclusions'), conclusions, (item, index) => item.id || index, (row, item) => {
      text(part(row, 'text'), item.text);
      sourceReferences(row, item, series.session_id);
    }, 'article', 'conclusion');
    const changes = detail.task_workspace?.changes || series?.changes || [];
    $('series-changes-section').hidden = !changes.length;
    list($('series-changes'), changes, (item, index) => item.id || index, (row, item) => {
      const labels = {created: '新增', continued: '延續', updated: '更新', completed: '完成', cancelled: '取消', superseded: '已被取代'};
      const action = labels[item.status] || labels[item.action] || labels[item.kind];
      const statusLabel = value => ({open:'未完成',deferred:'延後',completed:'完成',cancelled:'取消',superseded:'被取代',active:'有效'}[value] || value || '未設定');
      const differences = (item.differences || []).map(diff => `${diff.label}：${diff.field === 'status' ? statusLabel(diff.before) : diff.before || '未設定'} → ${diff.field === 'status' ? statusLabel(diff.after) : diff.after || '未設定'}`).join('；');
      text(part(row, 'text'), typeof item === 'string' ? item : differences || `${action ? action + '：' : ''}${item.text || ''}`);
      sourceReferences(row, item, series.session_id);
      const human = part(row,'human-evidence','a','task-edit-link');
      human.hidden = !(series.publication_kind === 'human_task_action' && item.kind === 'task');
      if (!human.hidden) {
        human.href = `/tasks/${encodeURIComponent(series.series_id)}/${encodeURIComponent(item.id)}`;
        text(human,'查看人工確認依據');
      }
    }, 'article', 'conclusion');
    const questions = series?.summary?.['未決問題'] || [];
    $('series-questions-section').hidden = !questions.length;
    list($('series-questions'), questions, (item,index) => index, (row,item) => {
      text(part(row,'text'),item.text);
    }, 'article','conclusion');
    const job = detail.postmeeting?.[0];
    const states = {ready: '等待外部整理', running: '外部整理進行中', published: '本次整理已發佈', notified: '本次整理已發佈，完成通知已保存', failed: '外部整理失敗', superseded: '此來源版本已由新版接續'};
    const unavailable = job?.state === 'ready' && ['external_dispatch_unavailable', 'external_authority_required', 'postmeeting_executor_unavailable'].includes(job.error_code);
    text($('postmeeting-status'), job ? `${states[job.state] || '外部整理狀態待確認'}${unavailable ? ' · 自主交接尚未接通' : job.error_code ? ' · 有待處理錯誤，請查看來源與診斷' : ''}。` : detail.session_id ? '尚無外部整理交接記錄；不能據此視為已完成整理。' : '會議結束並保存來源後，才會建立整理交接。');
  }
  function renderControls(detail) {
    const controls = detail.controls || {};
    const session = detail.session_id;
    const actions = [];
    if (controls.join) actions.push({name: 'join', id: detail.meeting.id, label: '加入會議', className: 'primary'});
    if (controls.rejoin) actions.push({name: 'rejoin', id: detail.meeting.id, label: '重新加入會議', className: 'primary'});
    if (controls.skip) actions.push({name: 'skip', id: detail.meeting.id, label: '略過這場'});
    if (controls.stop && session) actions.push({name: 'stop', id: session, label: '讓助理離開', className: 'danger'});
    if (controls.output && session && detail.response) actions.push({name: 'output', id: session,
      enabled: String(!detail.response.output_enabled), label: detail.response.output_enabled ? '停用語音輸出' : '啟用語音輸出'});
    if (controls.retry_summary && session) actions.push({name: 'retry-summary', id: session,
      label: detail.summary_job ? '重試摘要' : '產生摘要'});
    if (controls.delete) actions.push({name: 'delete', id: detail.meeting.id, label: '刪除會議', className: 'danger'});
    list($('meeting-actions'), actions, action => action.name, (button, action) => {
      button.type = 'button'; button.className = action.className || ''; button.dataset.action = action.name;
      button.dataset.target = action.id;
      if (action.enabled !== undefined) button.dataset.enabled = action.enabled;
      text(button, action.label); button.disabled = state.mutating;
    }, 'button');
    const response = detail.response;
    const labels = {idle: '待命', starting: '啟動中', connecting: '連線中', listening: '聆聽中', responding: '回覆中',
      sleeping: '待命', thinking: '思考中', answering: '回覆中', greeting: '問候中',
      speaking: '回覆中', running: '執行中', ready: '就緒', stopped: '已停止',
      unavailable: '無法啟動', stop_unknown: '停止待確認', failed: '失敗', disabled: '未啟用'};
    const screenCtx = response?.screen_context;
    const screenModeLabels = {off: '停用', on_demand: 'Phase 1 按需', watch_and_cache: 'Phase 2 預解析'};
    const screenInfo = screenCtx && screenCtx.mode && screenCtx.mode !== 'off'
      ? ` · 畫面理解：${screenModeLabels[screenCtx.mode] || screenCtx.mode}（已保存 ${screenCtx.observations_saved ?? 0} 頁${screenCtx.share_active ? '，分享中' : ''}）`
      : '';
    text($('response-status'), response ? `助理：${labels[response.state] || '狀態待確認'} · 語音輸出${response.output_enabled ? '已啟用' : '已停用'}${response.output_muted ? ' · 目前靜音' : ''}${screenInfo}${response.error_code ? ' · 有待處理錯誤，請查看診斷' : ''}。文字狀態不代表遠端已聽見聲音。` : '尚無助理回覆狀態記錄。');
  }
  function renderDetail() {
    const detail = state.detail;
    $('meeting-content').hidden = !detail;
    $('expand-meeting').disabled = !state.selected;
    if (!detail) return;
    const meeting = detail.meeting;
    text($('meeting-title'), meeting.title || '未命名會議');
    text($('meeting-state'), meeting.state_label || '狀態未確認');
    $('meeting-state').classList.toggle('is-live', Boolean(meeting.is_live));
    text($('meeting-kind'), meeting.kind === 'series' ? `系列會議 · ${meeting.series_name || '未命名系列'}` : '單場會議');
    text($('meeting-time'), `${meetingTime(meeting)} · ${timezone}`);
    if ($('meeting-auto-join')) text($('meeting-auto-join'), detail.auto_join?.label || '');
    text($('meeting-updated'), detail.updated_at ? `記錄更新於 ${timestamp(detail.updated_at)}` : '尚無記錄更新時間');
    $('source-detail').hidden = !detail.session_id;
    if (detail.session_id) $('source-detail').href = historyURL(detail.session_id);
    const runsBox = $('meeting-runs');
    if (runsBox) {
      const runs = detail.runs || [];
      runsBox.hidden = runs.length <= 1;
      list(runsBox, runs.length > 1 ? runs : [], row => row.session_id, (link, row, index) => {
        link.href = historyURL(row.session_id);
        text(link, `第 ${runs.length - index} 次入會（${timestamp(row.started_at)} · ${row.state_label || row.state}）`);
      }, 'a');
    }
    $('cleanup-warning').hidden = !detail.cleanup_review_required;
    $('qa-warning').hidden = detail.response?.question_activity?.state !== 'unavailable';
    renderControls(detail);
    renderSeriesState(detail);
    const previous = detail.previous;
    text($('previous-date'), previous?.date || '');
    text($('previous-note'), previous ? `${previous.title || '前次會議'} · 先前已發佈結論` : meeting.kind === 'series' ? '此系列尚無可接續的前次已發佈摘要。' : '這是單場會議，不會自動帶入其他會議內容。');
    $('previous-link').hidden = !previous?.session_id;
    if (previous?.session_id) $('previous-link').href = historyURL(previous.session_id);
    const conclusions = previous?.conclusions || [];
    list($('previous-conclusions'), conclusions, (_, index) => index, (row, conclusion) => {
      text(part(row, 'text'), conclusion.text); sources(row, conclusion.sources, previous.session_id);
    }, 'div', 'conclusion');
    if (previous && !conclusions.length) text($('previous-note'), `${previous.title || '前次會議'} · 摘要沒有已記錄的決策或重點。`);
    renderTasks(detail);
    $('series-section').hidden = meeting.kind !== 'series';
    text($('series-name'), `同系列會議 · ${meeting.series_name || '未命名系列'}`);
    list($('series-list'), detail.series_meetings || [], row => row.id, (button, row) => {
      button.type = 'button'; button.dataset.meeting = row.id;
      button.setAttribute('aria-current', String(row.id === state.selected));
      text(button, `${meetingTime(row)} · ${row.title} · ${row.state_label || '狀態未確認'}`);
    }, 'button', 'series-button');
    const completeness = {complete: '來源記錄完整', partial: '來源記錄不完整', unavailable: '來源記錄不可用'};
    text($('source-version'), `${detail.source_version != null ? '來源版本 ' + detail.source_version : '尚無來源版本'}${detail.completeness ? ' · ' + (completeness[detail.completeness] || '完整度未確認') : ''}`);
    const transcript = detail.transcript || [];
    const paragraphs = Array.isArray(detail.transcript_paragraphs) ? detail.transcript_paragraphs : [];
    text($('transcript-count'), paragraphs.length || transcript.length);
    text($('transcript-note'), transcript.length ? `以段落呈現（共 ${paragraphs.length || transcript.length} 段，${transcript.length} 筆原始來源）；保留原始來源品質，不推測發言者姓名。` : meeting.is_live ? '會議進行中，尚未收到可顯示的逐字稿。' : detail.session_id ? '此工作階段沒有已記錄的逐字稿。' : '尚未建立會議工作階段，沒有逐字稿。');
    if (paragraphs.length) {
      list($('transcript-list'), paragraphs, row => row.id, (item, row) => {
        item.id = `transcript-${row.id}`;
        const quality = row.quality && row.quality !== row.source_state ? ' · ' + humanState(row.quality) : '';
        const startLabel = timestamp(row.start_timestamp);
        const endLabel = row.end_timestamp && row.end_timestamp !== row.start_timestamp ? ' – ' + timestamp(row.end_timestamp) : '';
        text(part(item, 'meta', 'p', 'row-meta'), `${startLabel}${endLabel} · ${humanState(row.source_state)}${quality}${row.segment_count > 1 ? ` · ${row.segment_count} 片段` : ''}`);
        text(part(item, 'text', 'p', 'paragraph-text'), row.text);
        const details = part(item, 'source-details', 'details', 'paragraph-sources');
        text(part(details, 'summary', 'summary'), row.segment_count > 1 ? `查看原始來源（${row.segment_count}）` : '查看原始來源');
        sources(details, row.source_ids || [row.id], detail.session_id);
      }, 'article', 'transcript-paragraph');
    } else {
      list($('transcript-list'), transcript, row => row.id, (item, row) => {
        item.id = `transcript-${row.id}`;
        const quality = row.quality && row.quality !== row.source_state ? ' · ' + humanState(row.quality) : '';
        text(part(item, 'meta', 'p', 'row-meta'), `${timestamp(row.timestamp)} · ${humanState(row.source_state)}${quality} · 修訂 ${row.revision ?? '未提供'}`);
        text(part(item, 'text'), row.text);
        sources(item, [row.id], detail.session_id);
      }, 'article', 'transcript-row');
    }
    const qa = detail.qa || [];
    text($('qa-count'), qa.length);
    text($('qa-note'), qa.length ? '依實際記錄順序呈現；暫定文字可能繼續修訂。' : detail.session_id ? '尚無問答活動記錄；較早的工作階段可能未保存問答日誌。這不代表從未發生問答。' : '尚無問答活動記錄。');
    const questions = new Set(qa.filter(row => row.kind === 'question' && row.request_id).map(row => row.request_id));
    list($('qa-list'), qa, row => row.id, (item, row) => {
      const question = row.kind === 'question';
      const associated = !question && row.request_id && questions.has(row.request_id);
      item.classList.toggle('is-question', question);
      text(part(item, 'kind', 'strong'), question ? '已接收提問' : associated ? '小白回覆文字' : '小白回覆文字（未對應問題）');
      text(part(item, 'text'), row.text || '尚無文字內容');
      text(part(item, 'meta', 'p', 'row-meta'), `${timestamp(row.created_at)} · ${humanState(row.state)}${row.updated_at && row.updated_at !== row.created_at ? ' · 更新 ' + timestamp(row.updated_at) : ''}`);
      const association = part(item, 'association', 'details', 'row-meta');
      association.hidden = !row.request_id;
      if (row.request_id) {
        text(part(association, 'label', 'summary'), question ? '提問記錄識別' : associated ? '已核對提問識別' : '提問識別待核對');
        text(part(association, 'identity', 'code'), row.request_id);
      }
    }, 'article', 'qa-item');
    const screenObs = Array.isArray(detail.screen_observations) ? detail.screen_observations : [];
    if ($('screen-count')) text($('screen-count'), screenObs.length);
    if ($('screen-note')) text($('screen-note'), screenObs.length
      ? `已保存 ${screenObs.length} 頁分享畫面文字觀察（依擷取順序排列）。`
      : detail.session_id ? '此場次尚無分享畫面觀察記錄（可能未啟用畫面理解或未偵測到分享畫面）。' : '尚未建立會議工作階段，沒有分享畫面記錄。');
    if ($('screen-list')) {
      list($('screen-list'), screenObs, row => row.observation_id || row.id, (item, row) => {
        const structured = row.structured && typeof row.structured === 'object' ? row.structured : {};
        text(part(item, 'title', 'strong'), `#${row.capture_seq ?? '?'} · ${row.title || '未命名畫面'}`);
        text(part(item, 'meta', 'p', 'row-meta'), `${timestamp(row.captured_at)} · ${row.screen_kind || 'other'} · ${row.width && row.height ? `${row.width}×${row.height}` : ''}${row.vision_model ? ' · ' + row.vision_model : ''}`);
        text(part(item, 'summary', 'p'), row.summary_text || '');
        const metrics = Array.isArray(structured.metrics_or_numbers) ? structured.metrics_or_numbers.filter(Boolean) : [];
        const metricsEl = part(item, 'metrics', 'p', 'task-meta');
        metricsEl.hidden = !metrics.length;
        text(metricsEl, metrics.length ? `關鍵數據：${metrics.join('；')}` : '');
        const items = Array.isArray(structured.key_items) ? structured.key_items.filter(Boolean) : [];
        const itemsEl = part(item, 'items', 'p', 'task-meta');
        itemsEl.hidden = !items.length;
        text(itemsEl, items.length ? `重點：${items.join('；')}` : '');
        const uncertain = Array.isArray(structured.uncertain_areas) ? structured.uncertain_areas.filter(Boolean) : [];
        const warnEl = part(item, 'uncertain', 'p', 'warning');
        warnEl.hidden = !uncertain.length;
        text(warnEl, uncertain.length ? `不確定區域：${uncertain.join('；')}` : '');
        sources(item, row.id ? [row.id] : [], detail.session_id);
      }, 'article', 'screen-item');
    }
    const job = detail.summary_job || detail.postmeeting?.[0];
    const summaryStates = {ready: '摘要等待外部整理', pending: '摘要等待處理', running: '摘要產生中', processing: '摘要產生中', failed: '摘要產生失敗', completed: '摘要已發佈', published: '摘要已發佈', notified: '摘要已發佈', stale: '來源已有變更，摘要待更新'};
    text($('summary-note'), `${job ? summaryStates[job.state] || '摘要狀態待確認' : detail.summary ? '已儲存的本次摘要' : '尚無已發佈的本次摘要'}${job?.error_code ? ' · 請查看來源與診斷記錄' : ''}。摘要不取代原始來源。`);
    const sections = detail.summary ? ['重點', '決策', '待辦', '未決問題'] : [];
    list($('summary-list'), sections, section => section, (section, name) => {
      text(part(section, 'heading', 'h4'), name);
      const items = Array.isArray(detail.summary[name]) ? detail.summary[name] : [];
      const empty = part(section, 'empty', 'p', 'notice'); text(empty, '沒有已記錄的項目。'); empty.hidden = items.length > 0;
      list(part(section, 'items', 'div'), items, (_, index) => index, (row, item) => {
        text(part(row, 'text'), item.text);
        text(part(row, 'meta', 'p', 'task-meta'), [item.owner, item.date].filter(Boolean).join(' · '));
        sources(row, item.sources, detail.session_id);
      }, 'div', 'summary-item');
    }, 'section', 'summary-section');
  }
  const errorLabels = {admin_csrf_rejected: '安全驗證已失效，請重新整理後再試。', admin_origin_rejected: '此操作來源未通過安全驗證。',
    capture_cleanup_review_required: '請先完成人工擷取清理確認。', revision_conflict: '資料已被其他操作更新，請重新整理。',
    meeting_not_found: '找不到這場會議。', occurrence_not_found: '找不到這場會議。', invalid_task_status: '待辦狀態不被接受。',
    occurrence_not_rejoinable: '此會議目前狀態無法重新加入。', meeting_running_cannot_delete: '進行中的會議不能直接刪除，請先讓助理離開。',
    delete_confirmation_required: '請先確認刪除，再送出操作。',
    summary_source_unavailable: '此場次尚無可摘要的逐字來源。', summary_already_running: '此場次摘要正在產生中，請稍候。',
    postmeeting_active_cannot_delete: '會後整理仍在執行或受阻，暫時無法刪除。',
    native_response_unavailable: '語音回覆通道目前不可用，請查看進階診斷記錄。'};
  async function requestJSON(url, options = {}) {
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options,
      headers: {Accept: 'application/json', ...options.headers}});
    if (response.status === 401 || (response.redirected && new URL(response.url).pathname === '/login')) {
      window.location.assign('/login'); throw new Error('登入已過期，正在返回登入頁。');
    }
    if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('伺服器未傳回可讀取的資料，請稍後重試。');
    const data = await response.json();
    if (!response.ok) throw new Error(errorLabels[data.error] || `操作未完成（${response.status}），請至設定或來源診斷檢查。`);
    return data;
  }
  // Each loop owns one request. Generation changes discard late results even if abort races.
  function polling(load, success, failure, interval) {
    let timer = null, controller = null, generation = 0, restart = false;
    async function run() {
      if (document.hidden || controller) return;
      restart = false;
      const current = generation;
      const request = new AbortController(); controller = request;
      try {
        const value = await load(request.signal);
        if (current === generation && !request.signal.aborted && value !== null) success(value);
      } catch (error) {
        if (current === generation && !request.signal.aborted) failure(error);
      } finally {
        if (controller === request) controller = null;
        if (restart && !document.hidden) run();
        else if (current === generation && !document.hidden) timer = window.setTimeout(run, interval);
      }
    }
    return {
      refresh() { this.pause(); restart = true; if (!document.hidden) run(); },
      pause() { generation += 1; restart = false; clearTimeout(timer); timer = null; if (controller) controller.abort(); }
    };
  }
  const calendarPoll = polling(signal => {
    const start = monday(state.date);
    return requestJSON(`/api/calendar?${new URLSearchParams({start, end: addDays(start, 7)})}`, {signal});
  }, data => {
    preserveReading(() => {
      state.calendar = data; state.today = data.today || localToday(); renderCalendar();
      text($('calendar-status'), `已讀取已保存的會議${data.now ? ' · 服務時間 ' + timestamp(data.now) : ''}`);
      $('calendar-status').classList.remove('error');
      if (!state.selected) {
        const meetings = data.meetings.filter(meeting => overlaps(meeting, state.date));
        const preferred = meetings.find(meeting => meeting.is_live) || meetings[0];
        if (preferred) selectMeeting(preferred.id, false);
        else text($('detail-status'), `${dateLabel(state.date)} 沒有會議。可選擇其他日期，或至排程新增會議。`);
      }
    });
    if (positionCalendar) {
      positionCalendar = false;
      const scroll = document.querySelector('.calendar-scroll');
      const column = Array.from($('week-grid').children).find(item => item.dataset.day === state.date);
      const layout = dayLayout(data.meetings, state.date);
      const first = layout.timed.find(item => item.meeting.id === state.selected) || layout.timed[0];
      const floating = column?.querySelector('.day-floating');
      scroll.scrollTop = layout.untimed.length ? 0 : (floating?.offsetHeight || 0) + Math.max(0, (first ? first.start : 480) - 30) / 60 * hourHeight;
      if (column) scroll.scrollLeft += column.getBoundingClientRect().left - scroll.getBoundingClientRect().left - 54;
    }
  }, error => {
    text($('calendar-status'), `${state.calendar ? '重新整理失敗，保留上次資料（可能已過期）' : '行事曆載入失敗'}。${error.message}`);
    $('calendar-status').classList.add('error');
  }, 15000);
  const detailPoll = polling(signal => state.selected ? requestJSON(`/api/meetings/${encodeURIComponent(state.selected)}`, {signal}) : Promise.resolve(null), data => {
    preserveReading(() => {
      if (alignSelection) {
        alignSelection = false;
        const date = data.meeting.start_date;
        if (date && /^\d{4}-\d{2}-\d{2}$/.test(date)) {
          if (monday(date) !== monday(state.date)) changeDate(date, true);
          else state.date = date;
        }
      }
      state.detail = data; renderDetail(); renderCalendar();
      text($('detail-status'), '已讀取會議記錄 · 自動更新不會捲動閱讀位置');
      $('detail-status').classList.remove('error');
    });
  }, error => {
    text($('detail-status'), `${state.detail ? '更新失敗，保留上次記錄（可能已過期）' : '會議詳情載入失敗'}。${error.message}`);
    $('detail-status').classList.add('error');
  }, 5000);
  function selectMeeting(id, openMobile = true) {
    if (openMobile) alignSelection = true;
    if (id !== state.selected) {
      detailPoll.pause(); state.selected = id; state.detail = null;
      text($('action-status'), ''); $('action-status').removeAttribute('data-error');
      ['series-conclusions', 'series-changes', 'previous-conclusions', 'task-list', 'series-list', 'transcript-list', 'qa-list', 'screen-list', 'summary-list'].forEach(name => $(name)?.replaceChildren());
      $('previous-section').open = false;
      document.querySelectorAll('.reading-panel').forEach(panel => { panel.scrollTop = 0; });
    }
    renderDetail(); renderCalendar();
    text($('detail-status'), '正在載入會議詳情…'); $('detail-status').classList.remove('error');
    detailPoll.refresh();
    if (openMobile && window.matchMedia('(max-width:850px)').matches) setExpanded(true, true);
  }
  function changeDate(value, preserveSelection = false) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(dateValue(value).getTime())) return;
    positionCalendar = true;
    if (!preserveSelection) {
      detailPoll.pause(); alignSelection = false;
      state.selected = null; state.detail = null; setExpanded(false);
      text($('detail-status'), '正在讀取所選日期的會議…');
      $('detail-status').classList.remove('error');
      text($('action-status'), ''); $('action-status').removeAttribute('data-error');
      renderDetail();
    }
    state.date = value; state.calendar = null; renderCalendar();
    text($('calendar-status'), '正在載入所選週次…'); $('calendar-status').classList.remove('error');
    calendarPoll.refresh();
  }
  function setTab(name, focus = false) {
    state.tab = name;
    ['transcript', 'qa', 'screen', 'summary'].forEach(tab => {
      const active = name === tab;
      if ($('tab-' + tab)) {
        $('tab-' + tab).setAttribute('aria-selected', String(active));
        $('tab-' + tab).tabIndex = active ? 0 : -1;
      }
      if ($('panel-' + tab)) $('panel-' + tab).hidden = !active;
    });
    if (focus && $('tab-' + name)) $('tab-' + name).focus();
  }
  async function mutate(action, target, extras = {}) {
    if (state.mutating) return;
    if (action === 'stop' && !window.confirm('讓助理離開這場會議並停止擷取？這只會讓助理離開，不會結束其他人的會議。')) return;
    if (action === 'skip' && !window.confirm('略過這場排程？助理將不會自動加入這一場會議。')) return;
    if (action === 'delete') {
      if (!window.confirm('確定刪除此會議？手動場次將連同逐字稿與摘要紀錄一併移除；Google 行事曆場次會在本機隱藏且不再顯示。')) return;
      extras = {...extras, confirmed: 'true'};
    }
    const selected = state.selected;
    state.mutating = true;
    text($('action-status'), '正在送出操作…'); $('action-status').removeAttribute('data-error');
    if (state.detail) { renderControls(state.detail); renderTasks(state.detail); }
    renderCalendar();
    try {
      const result = await requestJSON(`/actions/${encodeURIComponent(action)}`, {method: 'POST',
        body: new URLSearchParams({csrf, id: target, ...extras})});
      if (result.ok !== true) throw new Error('伺服器未確認操作完成，請重新整理檢查。');
      if (action === 'delete' && selected === state.selected) {
        detailPoll.pause();
        state.selected = null; state.detail = null; setExpanded(false);
        renderDetail();
        text($('detail-status'), '會議已刪除。');
        calendarPoll.refresh();
      } else {
        if (selected === state.selected) text($('action-status'), '操作已受理，正在重新讀取實際狀態。');
        calendarPoll.refresh(); detailPoll.refresh();
      }
    } catch (error) {
      if (selected === state.selected) { text($('action-status'), error.message); $('action-status').dataset.error = 'true'; }
    } finally {
      state.mutating = false;
      if (state.detail) { renderControls(state.detail); renderTasks(state.detail); }
      renderCalendar();
    }
  }
  $('toggle-scheduler')?.addEventListener('click', () => {
    if (!state.calendar) return;
    mutate('scheduler', 'settings', {paused: state.calendar.scheduler_paused ? 'false' : 'true'});
  });
  $('prev-week').addEventListener('click', () => changeDate(addDays(state.date, -7)));
  $('next-week').addEventListener('click', () => changeDate(addDays(state.date, 7)));
  $('today').addEventListener('click', () => changeDate(localToday()));
  $('selected-date').addEventListener('change', event => changeDate(event.target.value));
  $('refresh').addEventListener('click', () => { calendarPoll.refresh(); detailPoll.refresh(); });
  $('expand-meeting').addEventListener('click', () => setExpanded(!state.expanded, true));
  $('mobile-open').addEventListener('click', () => setExpanded(true, true));
  $('toggle-tasks').addEventListener('click', () => { state.allTasks = !state.allTasks; if (state.detail) renderTasks(state.detail); });
  document.addEventListener('click', event => {
    const meeting = event.target.closest('button[data-meeting]');
    if (meeting) { selectMeeting(meeting.dataset.meeting); return; }
    const date = event.target.closest('button[data-date]');
    if (date) { changeDate(date.dataset.date); return; }
    const action = event.target.closest('button[data-action]');
    if (action) { mutate(action.dataset.action, action.dataset.target, action.dataset.enabled !== undefined ? {enabled: action.dataset.enabled} : {}); return; }
    const tab = event.target.closest('[role="tab"]'); if (tab) setTab(tab.id.slice(4));
  });
  document.addEventListener('change', event => {
    if (!event.target.matches('select[data-task]')) return;
    const task = state.detail?.tasks.find(item => item.id === event.target.dataset.task);
    const status = event.target.value;
    if (task) event.target.value = task.status; // Wait for persisted API state; never optimistic success.
    mutate('task', event.target.dataset.task, {status});
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && state.expanded) {
      event.preventDefault(); setExpanded(false,true); return;
    }
    if (!event.target.matches('[role="tab"]') || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = ['transcript', 'qa', 'screen', 'summary'];
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (tabs.indexOf(state.tab) + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length;
    setTab(tabs[index], true);
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { calendarPoll.pause(); detailPoll.pause(); }
    else { calendarPoll.refresh(); detailPoll.refresh(); }
  });
  window.addEventListener('pagehide', () => { calendarPoll.pause(); detailPoll.pause(); });
  renderCalendar(); renderDetail(); calendarPoll.refresh(); if (state.selected) selectMeeting(state.selected, false);
})();
