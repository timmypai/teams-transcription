"""Server-rendered admin shell; calendar content comes only from authenticated APIs."""

from html import escape

from starlette.responses import HTMLResponse


def _shell(title, body, session, active, *, calendar=False, timezone='', occurrence_id=None):
    h = lambda value: escape(str(value or ''), quote=True)
    authenticated = bool(session and session.get('authenticated'))
    navigation = ''
    if authenticated:
        links = [('/', '行事曆', 'calendar'), ('/schedule', '排程', 'schedule'),
                 ('/history', '歷史', 'history'), ('/settings', '設定', 'settings')]
        navigation = '<nav class="topnav" aria-label="主要導覽">' + ''.join(
            '<a href="' + url + '"' + (' aria-current="page"' if key == active else '') + '>' + label + '</a>'
            for url, label, key in links) + '</nav>'
        navigation += ('<form class="logout" method="post" action="/logout"><input type="hidden" name="csrf" value="' +
                       h(session['csrf']) + '"><button class="quiet" type="submit">登出</button></form>')
    metadata = ('<meta name="csrf-token" content="' + h((session or {}).get('csrf')) + '">'
                '<meta name="calendar-timezone" content="' + h(timezone) + '">'
                '<meta name="initial-occurrence" content="' + h(occurrence_id) + '">') if calendar else ''
    script = '<script src="/assets/calendar.js" defer></script>' if calendar else ''
    return HTMLResponse(
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + h(title) + ' · 小白會議</title>' + metadata +
        '<link rel="stylesheet" href="/assets/calendar.css">' + script + '</head><body>'
        '<a class="skip-link" href="#main-content">跳至主要內容</a>'
        '<header class="topbar"><a class="brand" href="/"><span class="brand-symbol" aria-hidden="true">白</span>'
        '小白會議</a>' + navigation + '</header>' + body + '</body></html>',
        headers={'Cache-Control': 'no-store'},
    )


def page(title, body, session=None, active='settings'):
    """Wrap trusted, server-rendered admin markup in the shared navigation shell."""
    secondary = ('<nav class="settings-nav" aria-label="管理導覽"><a href="/settings">連線與設定</a>'
                 '<a href="/glossary">詞庫</a><a href="/audit">稽核記錄</a></nav>') if active == 'settings' and session and session.get('authenticated') else ''
    content = '<main class="admin-content" id="main-content"><h1>' + escape(str(title)) + '</h1>' + secondary + body + '</main>'
    return _shell(title, content, session, active)


def calendar_page(session, timezone, *, initial_occurrence_id=None):
    """Return the production week calendar; no meeting data is embedded in HTML."""
    body = '''<main id="main-content">
<section class="page-intro"><div><p class="eyebrow">會議與記錄</p><h1 id="page-title">行事曆</h1></div>
<div class="intro-note"><p id="today-label"></p><p id="timezone-label"></p></div></section>
<section class="connection-strip" aria-label="服務狀態"><span id="connection-status">正在讀取服務狀態…</span><a href="/settings">連線與設定</a></section>
<div class="refresh-strip"><p id="calendar-status" role="status">正在載入行事曆…</p><button id="refresh" class="text-button" type="button">重新整理</button></div>
<div class="workspace" id="workspace">
<section class="calendar-pane" aria-label="週行事曆">
<div class="calendar-toolbar"><h2 id="period-title">本週</h2><div class="period-controls"><button id="prev-week" type="button" aria-label="上一週">←</button><button id="today" type="button">今天</button><button id="next-week" type="button" aria-label="下一週">→</button></div></div>
<div class="calendar-meta"><label for="selected-date">前往日期 <input type="date" id="selected-date"></label><a href="/schedule">新增與管理排程</a></div>
<div class="legend"><span class="live-key">進行中</span><span>系列會議</span><span>單場會議</span><span>包含週末</span></div>
<div class="mobile-selection"><span id="mobile-title">選擇會議查看內容</span><button id="mobile-open" type="button" disabled>查看會議 ↗</button></div>
<div class="calendar-scroll" tabindex="0" aria-label="七日行事曆，窄螢幕可在此左右捲動"><div class="week-grid" id="week-grid"></div></div>
<p class="calendar-footer" id="calendar-count">正在讀取會議…</p>
</section>
<aside class="inspector" id="inspector" aria-label="會議詳情">
<div class="inspector-label"><span id="inspector-kicker">會議預覽</span><button id="expand-meeting" type="button" disabled aria-expanded="false" aria-controls="detail-body">展開會議 ↗</button></div>
<p class="detail-status" id="detail-status" role="status">選擇一場會議，閱讀會議脈絡與本次記錄。</p>
<div id="meeting-content" hidden>
<header class="detail-header"><div class="meeting-badges"><span class="badge" id="meeting-state"></span><span class="badge" id="meeting-kind"></span></div><h2 id="meeting-title"></h2><p id="meeting-time" class="meta-line"></p><p id="meeting-updated" class="meta-line"></p><div class="meeting-actions" id="meeting-actions"></div><p id="response-status" class="notice"></p><p id="action-status" role="status"></p><a id="source-detail" hidden>來源與診斷記錄 ↗</a><p class="warning" id="cleanup-warning" hidden>擷取清理仍待人工確認；請至來源與診斷記錄檢查。此狀態不表示助理已安全離開。</p></header>
<p class="warning" id="qa-warning" hidden>問答記錄目前無法保存；下方內容可能不完整，不能據此判定是否有人提問或助理是否回覆。請查看來源與診斷記錄。</p>
<div class="detail-body" id="detail-body">
<section class="context-panel" aria-label="系列最新結論與待辦"><div class="section-heading"><h3>系列最新狀態</h3><span id="series-revision" class="small-date"></span></div><p id="series-state-note" class="notice"></p><a id="series-state-link" hidden>查看本版來源 ↗</a><div id="series-conclusions"></div><div id="series-changes-section" hidden><h4>本次必要變更</h4><div id="series-changes"></div></div>
<div id="series-questions-section" hidden><h4>待確認</h4><div id="series-questions"></div></div>
<details id="previous-section"><summary>先前會議脈絡</summary><div class="section-heading"><h4>從上次接續</h4><span id="previous-date" class="small-date"></span></div><p id="previous-note" class="notice"></p><a id="previous-link" hidden>查看前次來源 ↗</a><div id="previous-conclusions"></div></details>
<div class="section-heading task-heading"><h3>系列待辦</h3><button id="toggle-tasks" class="text-button" type="button" aria-pressed="false">包含已完成</button></div><p id="tasks-note" class="notice"></p><div id="task-list"></div>
<details id="series-section" class="series" hidden><summary id="series-name">同系列會議</summary><div id="series-list"></div></details></section>
<section class="current-panel" aria-label="本次會議內容"><div class="current-head"><h3>本次會議</h3><span id="source-version" class="small-date"></span></div><p id="postmeeting-status" class="notice" role="status"></p><p class="notice">本次記錄與系列最新狀態分開呈現；逐字稿不代表問答或音訊已成功送達。</p>
<div class="tabs" role="tablist" aria-label="本次會議閱讀內容"><button id="tab-transcript" class="tab" role="tab" aria-selected="true" aria-controls="panel-transcript" type="button">逐字稿 <span id="transcript-count">0</span></button><button id="tab-qa" class="tab" role="tab" aria-selected="false" aria-controls="panel-qa" tabindex="-1" type="button">問答 <span id="qa-count">0</span></button><button id="tab-summary" class="tab" role="tab" aria-selected="false" aria-controls="panel-summary" tabindex="-1" type="button">本次摘要</button></div>
<div class="reading-panel" id="panel-transcript" role="tabpanel" aria-labelledby="tab-transcript" tabindex="0"><p id="transcript-note" class="notice"></p><div id="transcript-list"></div></div>
<div class="reading-panel" id="panel-qa" role="tabpanel" aria-labelledby="tab-qa" tabindex="0" hidden><p class="notice">僅列實際記錄的提問與助理回覆。文字回覆不證明音訊已播出；未確認歸屬的回覆不配對到最近提問。</p><p id="qa-note" class="notice"></p><div id="qa-list"></div></div>
<div class="reading-panel" id="panel-summary" role="tabpanel" aria-labelledby="tab-summary" tabindex="0" hidden><p id="summary-note" class="notice"></p><div id="summary-list"></div></div>
</section></div></div></aside></div>
<noscript><p class="warning">此行事曆需要 JavaScript 才能載入資料。您仍可使用<a href="/schedule">排程</a>與<a href="/history">歷史記錄</a>。</p></noscript>
</main>'''
    return _shell('行事曆', body, session, 'calendar', calendar=True, timezone=timezone,
                  occurrence_id=initial_occurrence_id)
