/* MGlogWH Control Room — dependency-free browser client. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    session: null, overview: null, runs: [], activeView: 'overview',
    feedRun: '', fileRun: '', cursor: null, events: [], visibleEvents: [],
    selectedRowid: null, priority: 'all', query: '', paused: false,
    eventsLoading: false, overviewLoading: false, runsLoading: false, filesLoading: false,
    feedGeneration: 0, eventIndex: null, eventMessage: '', lastEventUpdate: null,
    feedSkipped: false,
    rules: [], savedRules: [], revision: '', rulesDirty: false,
    ruleSchema: 'legacy', editorSchema: 'legacy', upgradePending: false,
    pendingApply: null, ruleDemo: false, rulesBusy: false, files: [], filePath: '',
    pollCount: 0, started: false
  };
  const viewNames = {overview: '관제 현황', rules: '이벤트 규칙', runs: '실행 기록', files: '저장 파일'};
  const priorityNames = {0: 'BULK', 1: 'NORMAL', 2: 'CRITICAL'};
  const kindNames = {1: 'exec', 2: 'openat'};
  const numberFormat = new Intl.NumberFormat('ko-KR');
  const timeFormat = new Intl.DateTimeFormat('ko-KR', {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const dateFormat = new Intl.DateTimeFormat('ko-KR', {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});

  function text(id, value) { $(id).textContent = value == null ? '—' : String(value); }
  function node(tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value != null) item.textContent = String(value);
    return item;
  }
  function icon(id) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${id}`);
    svg.append(use);
    return svg;
  }
  function count(value) { return value != null && value !== '' && Number.isFinite(Number(value)) ? numberFormat.format(Number(value)) : '—'; }
  function shortId(value) { return value ? String(value).slice(0, 8) : '없음'; }
  function bytes(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n < 0) return '—';
    if (n < 1024) return `${n} B`;
    const unit = Math.floor(Math.log(n) / Math.log(1024));
    return `${(n / 1024 ** unit).toFixed(unit > 1 ? 1 : 0)} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][unit] || 'B'}`;
  }
  function dateLabel(value, options = {}) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return (options.approx ? '약 ' : '') + (options.short ? timeFormat : dateFormat).format(date);
  }
  function setClassText(id, value, tone) {
    const item = $(id);
    item.textContent = value;
    item.classList.remove('good', 'warn', 'bad');
    if (tone) item.classList.add(tone);
  }
  function notification(message, error = false) {
    const toast = node('div', `toast${error ? ' error' : ''}`, message);
    $('toast-region').append(toast);
    window.setTimeout(() => toast.remove(), 5200);
  }
  function showNotice(message, error = false) {
    const notice = $('global-notice');
    notice.textContent = message;
    notice.classList.toggle('error', error);
    notice.hidden = !message;
  }
  function showLogin() {
    $('login-screen').hidden = false;
    state.session = null;
    window.setTimeout(() => $('login-token').focus(), 50);
  }
  function hideLogin() { $('login-screen').hidden = true; }
  async function api(path, options = {}) {
    const headers = {'Accept': 'application/json'};
    const method = options.method || 'GET';
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      if (state.session && state.session.csrf_token) headers['X-CSRF-Token'] = state.session.csrf_token;
    }
    let response;
    try {
      response = await fetch(path, {method, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), credentials: 'same-origin', cache: 'no-store'});
    } catch (error) {
      throw new Error('서버에 연결할 수 없습니다. 네트워크와 관제 서버를 확인하세요.');
    }
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* Non-JSON error responses are handled below. */ }
    if (response.status === 401 && path !== '/api/session' && path !== '/api/login') showLogin();
    if (!response.ok) throw new Error(payload.error || `요청이 실패했습니다 (HTTP ${response.status}).`);
    return payload;
  }

  function currentRunId() { return state.overview && state.overview.run ? state.overview.run.id : ''; }
  function feedRunId() { return state.feedRun || currentRunId(); }
  function fileRunId() { return state.fileRun || currentRunId(); }
  function priorityValue(value) {
    if (typeof value === 'number') return value;
    const normalized = String(value == null ? '' : value).toLowerCase();
    if (normalized === 'critical' || normalized === '2') return 2;
    if (normalized === 'normal' || normalized === '1') return 1;
    if (normalized === 'bulk' || normalized === '0') return 0;
    return -1;
  }
  function kindLabel(value) {
    if (typeof value === 'number') return kindNames[value] || `kind ${value}`;
    return String(value || 'unknown');
  }
  function policyName(value) {
    if (value == null || value === '') return null;
    const names = {0: 'NORMAL_COLLECTION', 1: 'QUARANTINE', 2: 'QUARANTINE_EXEMPT'};
    const key = String(value);
    return Object.hasOwn(names, key) ? names[key] : key.toUpperCase();
  }
  function policyLabel(value) {
    const policy = policyName(value);
    if (!policy) return 'UNKNOWN';
    if (policy === 'QUARANTINE_EXEMPT' || policy === 'PROTECTED_CRITICAL') return '격리 면제';
    if (policy === 'QUARANTINE') return '격리';
    if (policy === 'NORMAL_COLLECTION') return '일반 수집';
    if (policy === 'CRITICAL') return 'Critical 기본 수집';
    if (policy === 'NORMAL') return 'Normal 기본 수집';
    if (policy === 'BULK') return 'Bulk 기본 수집';
    if (policy.includes('SAMPLE')) return '샘플';
    return policy.replaceAll('_', ' ');
  }
  function policyClass(value) {
    const policy = policyName(value);
    return policy === 'QUARANTINE' ? 'quarantine' :
      policy === 'QUARANTINE_EXEMPT' || policy === 'PROTECTED_CRITICAL' ? 'protected' : '';
  }
  function priorityBadge(event) {
    const priority = priorityValue(event.priority);
    const badge = node('span', `priority-badge ${priority === 2 ? 'critical' : priority === 1 ? 'normal' : priority === 0 ? 'bulk' : 'unknown'}`, priorityNames[priority] || 'UNKNOWN');
    badge.title = '수집 규칙에 따른 우선순위입니다. 위협 판정이 아닙니다.';
    return badge;
  }
  function eventTime(event) {
    if (event.wall_time) return dateLabel(event.wall_time, {approx: event.time_approximate !== false, short: true});
    if (event.monotonic_ns != null && Number.isFinite(Number(event.monotonic_ns))) return `mono ${(Number(event.monotonic_ns) / 1e9).toFixed(2)}s`;
    return '—';
  }

  async function boot() {
    try {
      const session = await api('/api/session');
      if (!session.authenticated) { showLogin(); return; }
      state.session = session;
      hideLogin();
      state.started = true;
      renderMode();
      await Promise.allSettled([refreshOverview(), refreshRuns(), refreshRules()]);
      await refreshEvents();
      if (state.activeView === 'files') await refreshFiles();
      if (!state.pollTimer) state.pollTimer = window.setInterval(poll, 1000);
    } catch (error) {
      showNotice(error.message, true);
      renderOffline();
    }
  }
  async function login(event) {
    event.preventDefault();
    const token = $('login-token').value.trim();
    if (!token) return;
    $('login-submit').disabled = true;
    $('login-error').hidden = true;
    try {
      const session = await api('/api/login', {method: 'POST', body: {token}});
      if (!session.authenticated) throw new Error('인증에 실패했습니다.');
      $('login-token').value = '';
      state.session = session;
      hideLogin();
      await boot();
    } catch (error) {
      $('login-error').textContent = error.message;
      $('login-error').hidden = false;
    } finally { $('login-submit').disabled = false; }
  }
  function poll() {
    if (!state.session || document.hidden) return;
    state.pollCount += 1;
    if (!state.paused) refreshEvents().catch((error) => handlePollError(error));
    if (state.pollCount % 5 === 0) refreshOverview().catch((error) => handlePollError(error));
    if (state.pollCount % 10 === 0) {
      refreshRuns().catch((error) => handlePollError(error));
      if (state.activeView === 'files') refreshFiles().catch((error) => handlePollError(error));
    }
  }
  function handlePollError(error) {
    if (!state.session) return;
    showNotice(error.message, true);
    text('event-footnote', '데이터 갱신 실패 · 마지막으로 수신한 이벤트를 표시 중');
  }
  function renderOffline() {
    const badge = $('mode-badge');
    badge.textContent = '연결 끊김';
    badge.className = 'mode-badge offline';
    text('side-status', '서버 연결 실패');
    $('side-signal').className = 'signal bad';
  }
  function renderMode() {
    const demo = !!state.session?.demo;
    const mode = String(state.session?.mode || state.overview?.mode || 'linux').toUpperCase();
    const badge = $('mode-badge');
    badge.textContent = demo ? 'DEMO · 샘플 데이터' : `${mode} · 로컬 관제`;
    badge.className = `mode-badge${demo ? ' demo' : ''}`;
    if (demo) {
      showNotice('데모 모드: 이 화면의 이벤트와 상태는 샘플 데이터입니다. 실제 MGlogWH 수집 결과가 아닙니다.');
    } else if ($('global-notice').textContent.startsWith('데모 모드')) {
      showNotice('');
    }
  }

  async function refreshOverview() {
    if (state.overviewLoading || !state.session) return;
    state.overviewLoading = true;
    try {
      const previousRun = currentRunId();
      const data = await api('/api/overview');
      state.overview = data;
      renderMode();
      renderOverview();
      if (previousRun !== currentRunId() && !state.feedRun) resetFeed();
      renderRunSelectors();
      if (state.activeView === 'files' && !state.fileRun && previousRun !== currentRunId()) refreshFiles().catch(handlePollError);
    } finally { state.overviewLoading = false; }
  }
  function collectorTone(value) {
    const stateName = String(value || '').toUpperCase();
    if (['RUNNING', 'ACTIVE', 'OK'].includes(stateName)) return 'good';
    if (['FAILED', 'ERROR', 'UNAVAILABLE'].includes(stateName)) return 'bad';
    return 'warn';
  }
  function translatedState(value, type) {
    const key = String(value || '').toUpperCase();
    const names = {
      RUNNING: '실행 중', ACTIVE: '실행 중', STOPPED: '중지됨', NOT_STARTED: '시작 전',
      UNCONFIGURED: '설정 필요', UNAVAILABLE: '사용 불가', FAILED: '실패', ERROR: '오류',
      OK: type === 'index' ? '정상' : '실행 중', DEGRADED: '성능 저하', CURRENT: '최신',
      PARTIAL: '부분 반영', COMPLETE: '완료', UNKNOWN: '확인 불가',
      DEMO_SIMULATED: '샘플 데이터', SIMULATED: '샘플 데이터',
      INDEX_PARTIAL: '부분 색인', INDEX_PAUSED_STORAGE_PRESSURE: '저장 공간 부족',
      BOUNDARY_UNKNOWN: '경계 확인 불가'
    };
    return names[key] || (key ? key.replaceAll('_', ' ') : '정보 없음');
  }
  function lossDetails(loss) {
    if (!loss || typeof loss !== 'object') return [];
    const values = [
      ['일반 용량', loss.general_quota_drop],
      ['격리 저장', loss.quarantine_storage_drop],
      ['메타데이터', loss.metadata_dropped],
      ['메타 넘침', loss.metadata_overflow]
    ];
    if (Array.isArray(loss.queue_overflow)) {
      for (const [index, label] of ['Critical 큐', '일반 큐', '격리 큐'].entries()) values.push([label, loss.queue_overflow[index]]);
    } else values.push(['큐 초과', loss.queue_overflow]);
    return values.map(([label, value]) => {
      if (!Number.isFinite(Number(value))) return null;
      if (value == null || value === '') return null;
      return {label, value: Number(value)};
    }).filter(Boolean);
  }
  function observedValue(value) {
    return value == null || !Number.isFinite(Number(value)) ? 'UNKNOWN' : count(value);
  }
  function observedLanes(value) {
    if (!Array.isArray(value) || value.length !== 3) return 'UNKNOWN';
    return ['Critical', '일반', '격리'].map((label, index) => `${label} ${observedValue(value[index])}`).join(' · ');
  }
  function renderRuleStats() {
    const data = state.overview || {};
    const list = $('rule-stats-list');
    list.replaceChildren();
    const versions = [data.rule_engine_schema_version == null ? null : `엔진 스키마 v${data.rule_engine_schema_version}`,
      data.rule_version == null ? null : `규칙 v${data.rule_version}`,
      data.policy_version == null ? null : `정책 v${data.policy_version}`].filter(Boolean);
    text('rule-stats-version', versions.join(' · '));
    const stats = data.rule_stats;
    if (data.demo) {
      text('rule-stats-note', '데모 통계는 실제 수집 증거가 아닙니다.');
      return;
    }
    if (!Array.isArray(stats) || !stats.length) {
      text('rule-stats-note', '현재 실행의 규칙별 통계가 없습니다. 미집계는 0건의 증거가 아닙니다.');
      return;
    }
    const scope = data.rule_stats_scope;
    const scopeLabel = scope == null ? '집계 범위 미상' : typeof scope === 'string' ? scope : JSON.stringify(scope);
    text('rule-stats-note', `현재 실행 기준 · ${data.rule_stats_snapshot_complete === true ? '스냅샷 완료' : '스냅샷 완전성 UNKNOWN'} · ${scopeLabel}. 평가 전 손실은 규칙별로 귀속되지 않을 수 있습니다.`);
    for (const stat of stats) {
      if (!stat || typeof stat !== 'object') continue;
      const item = node('article', 'rule-stat-item');
      const title = node('div', 'rule-stat-title');
      title.append(node('strong', '', `RULE ${stat.rule_id ?? '—'}`),
        node('span', '', priorityNames[priorityValue(stat.priority)] || '우선순위 UNKNOWN'),
        node('span', '', policyLabel(stat.configured_policy)),
        node('small', '', `선택 순위 ${stat.precedence ?? 'UNKNOWN'}`));
      const metrics = node('dl', 'rule-stat-metrics');
      const values = [
        ['평가', stat.evaluated], ['일치', stat.matched], ['조건 UNKNOWN', stat.unknown],
        ['최종 선택', stat.selected], ['Ring 제출', stat.ring_submitted],
        ['격리 요약', stat.quarantine_summarized], ['Ring 실패', stat.ring_failed],
        ['Bulk 예산 제외', stat.bulk_budget_suppressed], ['Bulk 예산 경쟁', stat.bulk_budget_contention],
        ['수집기 관측', observedLanes(stat.collector_observed)],
        ['수집기 저장', observedLanes(stat.collector_appended)],
        ['내구 기록', observedLanes(stat.collector_durable)],
        ['큐 거절', observedLanes(stat.collector_queue_rejected)],
        ['용량 거절', observedLanes(stat.collector_quota_rejected)]
      ];
      for (const [label, value] of values) {
        const field = node('div', 'rule-stat-metric');
        field.append(node('dt', '', label), node('dd', '', typeof value === 'string' ? value : observedValue(value)));
        metrics.append(field);
      }
      item.append(title, metrics);
      list.append(item);
    }
  }
  function renderOverview() {
    const data = state.overview || {};
    const collector = data.collector || {};
    const index = data.index || {};
    const collectorState = collector.state || (data.demo ? 'DEMO' : 'UNCONFIGURED');
    const tone = collectorTone(collectorState);
    setClassText('collector-state', data.demo ? '샘플 수집기' : translatedState(collectorState), data.demo ? 'warn' : tone);
    text('collector-sub', data.demo ? '실제 수집기가 연결되지 않았습니다.' : collector.alive === false ? '수집기 프로세스 응답 없음' : collectorState === 'UNCONFIGURED' ? 'MGlogWH 경로와 서비스를 확인하세요.' : 'MGlogWH 상태');
    setClassText('index-state', translatedState(index.state, 'index'), collectorTone(index.state));
    const lag = index.lag_records == null ? NaN : Number(index.lag_records);
    const coverage = index.coverage_state ? ` · ${translatedState(index.coverage_state, 'index')}` : '';
    text('index-sub', Number.isFinite(lag) ? `인덱스 지연 ${count(lag)}건${coverage}` : `지연 정보 없음${coverage}`);
    const details = lossDetails(data.loss || collector);
    const positive = details.filter((part) => part.value > 0);
    if (data.demo) {
      setClassText('loss-state', '샘플 지표', 'warn');
      text('loss-sub', '실제 수집 손실을 나타내지 않습니다.');
    } else if (!details.length) {
      setClassText('loss-state', '정보 없음', 'warn');
      text('loss-sub', '전체 손실량은 계산할 수 없습니다.');
    } else if (positive.length) {
      setClassText('loss-state', `${positive.length}개 지표 증가`, 'bad');
      const label = positive.map((part) => `${part.label} ${count(part.value)}`).join(' · ');
      text('loss-sub', label);
      $('loss-sub').title = `${label} · 서로 범위가 달라 합계로 계산하지 않습니다.`;
    } else {
      setClassText('loss-state', '기록된 증가 없음', 'good');
      text('loss-sub', '부분 계수 0 · 전체 손실 없음의 증거는 아닙니다.');
    }
    setClassText('current-run', shortId(data.run?.id), data.run ? '' : 'warn');
    $('current-run').title = data.run?.id || '';
    text('run-sub', data.run ? (data.run.path || '실행 중인 수집 기록') : '현재 실행이 없습니다.');
    const heading = document.querySelector('.heading-status');
    heading.classList.remove('good', 'warn', 'bad');
    heading.classList.add(data.demo ? 'warn' : tone);
    text('heading-status', data.demo ? '데모 데이터' : collectorState === 'RUNNING' ? '수집 정상 동작' : translatedState(collectorState));
    $('side-signal').className = `signal ${data.demo ? 'warn' : tone === 'good' ? 'ok' : tone}`;
    text('side-status', data.demo ? '데모 모드' : `수집기 ${translatedState(collectorState)}`);
    text('side-context', data.demo ? '샘플 데이터를 표시합니다.' : data.run ? `실행 ${shortId(data.run.id)} · ${String(data.mode || '').toUpperCase()}` : 'MGlogWH 실행을 기다립니다.');
    if (!data.demo && collectorState === 'UNCONFIGURED') showNotice('MGlogWH 수집기를 찾지 못했습니다. 설치 위치와 데이터 경로를 확인하세요.');
    else if (!data.demo && $('global-notice').textContent.startsWith('MGlogWH 수집기를')) showNotice('');
    renderCounts();
    renderFeedStatus();
    renderRuleStats();
    if (state.activeView === 'files' && !state.filePath) renderFileLocation();
  }
  function renderCounts() {
    const historical = !!state.feedRun && state.feedRun !== currentRunId();
    let values;
    if (historical) {
      values = {total: state.events.length, critical: 0, normal: 0, bulk: 0};
      for (const event of state.events) {
        const value = priorityValue(event.priority);
        if (value === 2) values.critical++;
        else if (value === 1) values.normal++;
        else if (value === 0) values.bulk++;
      }
      text('counts-note', '선택한 실행 · 화면 수신분 (최대 1,000건)');
    } else {
      values = state.overview?.counts || {};
      text('counts-note', '현재 실행의 인덱스 누적 집계');
    }
    for (const key of ['total', 'critical', 'normal', 'bulk']) text(`count-${key}`, count(values[key]));
  }
  function renderFeedStatus() {
    text('feed-live-label', state.paused ? '갱신 일시정지' : '1초 간격 갱신');
    let note = '검색 인덱스에 반영된 이벤트를 표시합니다. Critical은 규칙에 따른 분류입니다.';
    const index = state.eventIndex || state.overview?.index || {};
    const lag = index.lag_records == null ? NaN : Number(index.lag_records);
    if (Number.isFinite(lag) && lag > 0) note = `인덱스 지연 ${count(lag)}건 · 수집 이벤트가 늦게 표시될 수 있습니다.`;
    if (state.eventMessage) note = String(state.eventMessage);
    if (state.paused) note = '화면 갱신 일시정지 · 수집기는 계속 동작할 수 있습니다.';
    text('event-footnote', note);
    text('event-update-time', state.lastEventUpdate ? `마지막 갱신 ${timeFormat.format(state.lastEventUpdate)}` : '갱신 전');
  }

  async function refreshRuns() {
    if (state.runsLoading || !state.session) return;
    state.runsLoading = true;
    try {
      const data = await api('/api/runs');
      state.runs = Array.isArray(data.runs) ? data.runs : [];
      renderRunSelectors();
      renderRuns();
    } finally { state.runsLoading = false; }
  }
  function renderRunSelectors() {
    for (const [id, selected] of [['feed-run-select', state.feedRun], ['file-run-select', state.fileRun]]) {
      const select = $(id);
      select.replaceChildren();
      const current = node('option', '', `현재 실행${currentRunId() ? ` · ${shortId(currentRunId())}` : ''}`);
      current.value = '';
      select.append(current);
      for (const run of state.runs) {
        const option = node('option', '', `${shortId(run.id)} · ${dateLabel(run.started_at)}`);
        option.value = run.id;
        select.append(option);
      }
      select.value = selected;
      if (select.value !== selected) {
        select.value = '';
        if (id === 'feed-run-select') { state.feedRun = ''; resetFeed(); }
        else state.fileRun = '';
      }
    }
  }
  function renderRuns() {
    const container = $('runs-list');
    container.replaceChildren();
    text('run-counter', `${count(state.runs.length)}개 실행`);
    $('runs-empty').hidden = state.runs.length > 0;
    for (const run of state.runs) {
      const row = node('div', 'run-row');
      const main = node('div');
      const name = node('strong', 'run-name', run.id || 'ID 없음');
      name.title = String(run.id || '');
      main.append(name, node('small', 'run-path', run.path || '경로 정보 없음'));
      const schemaInfo = [run.raw_schema == null ? null : `Raw schema ${run.raw_schema}`,
        run.policy_version == null ? null : `정책 v${run.policy_version}`].filter(Boolean).join(' · ');
      if (schemaInfo) main.append(node('small', 'run-path', schemaInfo));
      const start = node('span', 'run-meta', dateLabel(run.started_at));
      const rawState = String(run.state || 'UNKNOWN').toUpperCase();
      const badge = node('span', `run-state-badge ${rawState === 'RUNNING' ? 'running' : rawState === 'FAILED' ? 'failed' : 'stopped'}`, translatedState(rawState));
      const actions = node('div', 'run-actions');
      const view = node('button', 'button button-secondary', '이벤트');
      view.type = 'button';
      view.addEventListener('click', () => {
        state.feedRun = run.id;
        renderRunSelectors();
        resetFeed();
        showView('overview');
        refreshEvents().catch((error) => notification(error.message, true));
      });
      const files = node('button', 'button button-secondary', '파일');
      files.type = 'button';
      files.addEventListener('click', () => {
        state.fileRun = run.id;
        renderRunSelectors();
        showView('files');
      });
      actions.append(view, files);
      row.append(main, start, badge, actions);
      container.append(row);
    }
  }

  function resetFeed() {
    state.feedGeneration += 1;
    state.cursor = null;
    state.events = [];
    state.visibleEvents = [];
    state.selectedRowid = null;
    state.eventIndex = null;
    state.eventMessage = '';
    state.feedSkipped = false;
    $('feed-gap').hidden = true;
    $('inspect-empty').hidden = false;
    $('inspect-content').hidden = true;
    renderEvents();
    renderCounts();
  }
  async function refreshEvents() {
    if (state.eventsLoading || !state.session || state.paused) return;
    state.eventsLoading = true;
    const generation = state.feedGeneration;
    const runId = feedRunId();
    try {
      let added = 0;
      let data = null;
      const append = (incoming) => {
        const existing = new Set(state.events.map((event) => String(event.rowid)));
        for (const event of incoming) {
          if (!event || existing.has(String(event.rowid))) continue;
          state.events.push(event);
          existing.add(String(event.rowid));
          added++;
        }
        if (state.events.length > 1000) state.events.splice(0, state.events.length - 1000);
      };
      // Drain at most two 500-record pages per tick. If the live view still trails,
      // re-enter at the latest page and tell the operator that the display has a gap.
      for (let batch = 0; batch < 2; batch++) {
        const params = new URLSearchParams({limit: '500'});
        if (runId) params.set('run', runId);
        if (state.cursor !== null) params.set('cursor', String(state.cursor));
        data = await api(`/api/events?${params}`);
        if (generation !== state.feedGeneration || runId !== feedRunId()) return;
        append(Array.isArray(data.events) ? data.events : []);
        if (data.cursor != null) state.cursor = data.cursor;
        if (!data.has_more) break;
        if (batch === 1) {
          const latestParams = new URLSearchParams({limit: '500'});
          if (runId) latestParams.set('run', runId);
          const latest = await api(`/api/events?${latestParams}`);
          if (generation !== state.feedGeneration || runId !== feedRunId()) return;
          state.events = [];
          state.selectedRowid = null;
          $('inspect-empty').hidden = false;
          $('inspect-content').hidden = true;
          append(Array.isArray(latest.events) ? latest.events : []);
          if (latest.cursor != null) state.cursor = latest.cursor;
          state.feedSkipped = true;
          $('feed-gap').hidden = false;
          data = latest;
        }
      }
      state.eventIndex = data?.index || null;
      state.eventMessage = data?.message || '';
      state.lastEventUpdate = new Date();
      if (added || state.cursor === null) renderEvents();
      else renderFeedStatus();
      if (added && state.feedRun) renderCounts();
    } finally { state.eventsLoading = false; }
  }
  function filteredEvents() {
    const query = state.query.toLocaleLowerCase();
    return state.events.filter((event) => {
      if (state.priority !== 'all' && String(priorityValue(event.priority)) !== state.priority) return false;
      if (!query) return true;
      return [event.comm, event.path, event.pid, event.uid, kindLabel(event.kind), event.rule_id,
        event.effective_policy, event.rule_version, event.policy_version]
        .some((value) => String(value == null ? '' : value).toLocaleLowerCase().includes(query));
    }).slice(-250).reverse();
  }
  function renderEvents() {
    const entries = filteredEvents();
    state.visibleEvents = entries;
    const body = $('event-rows');
    const fragment = document.createDocumentFragment();
    for (const event of entries) {
      const row = node('tr', String(event.rowid) === String(state.selectedRowid) ? 'is-selected' : '');
      row.tabIndex = 0;
      row.setAttribute('aria-label', `${priorityNames[priorityValue(event.priority)] || 'UNKNOWN'} ${kindLabel(event.kind)} ${event.comm || ''} ${event.path || ''}`);
      row.addEventListener('click', () => selectEvent(event));
      row.addEventListener('keydown', (keyEvent) => {
        if (keyEvent.key === 'Enter' || keyEvent.key === ' ') { keyEvent.preventDefault(); selectEvent(event); }
      });
      const time = node('td');
      const timeValue = node('span', 'event-time', eventTime(event));
      timeValue.title = event.time_approximate !== false ? '수집기 시계 앵커 기준 추정 시각입니다.' : '이벤트 시각';
      time.append(timeValue);
      const priority = node('td'); priority.append(priorityBadge(event));
      const main = node('td');
      const mainInner = node('div', 'event-main');
      mainInner.append(node('strong', '', `${kindLabel(event.kind)} · ${event.comm || '프로세스 미상'}`), node('small', '', `PID ${event.pid ?? '—'}${Number.isInteger(event.rule_id) && event.rule_id > 0 ? ` · RULE ${event.rule_id}` : ''}`));
      main.append(mainInner);
      const path = node('td');
      const pathText = node('span', 'truncate', event.path || '—');
      pathText.title = String(event.path || '');
      path.append(pathText);
      const policy = node('td'); policy.append(node('span', `policy-badge ${policyClass(event.effective_policy)}`, policyLabel(event.effective_policy)));
      const arrow = node('td'); arrow.append(icon('arrow'));
      arrow.firstChild.classList.add('row-chevron');
      row.append(time, priority, main, path, policy, arrow);
      fragment.append(row);
    }
    body.replaceChildren(fragment);
    $('events-empty').hidden = entries.length > 0;
    if (!entries.length) {
      const empty = $('events-empty');
      empty.querySelector('strong').textContent = state.query || state.priority !== 'all' ? '조건에 맞는 이벤트가 없습니다' : '이벤트를 기다리고 있습니다';
      empty.querySelector('p').textContent = state.query || state.priority !== 'all' ? '검색어나 우선순위 필터를 바꿔 보세요.' : state.overview?.demo ? '데모 이벤트가 준비되면 이곳에 표시됩니다.' : '수집기가 실행되면 로그가 이곳에 표시됩니다.';
    }
    text('feed-counter', `${count(entries.length)}건 표시${state.events.length > 250 ? ` · 최근 ${count(state.events.length)}건 보관` : ''}`);
    renderFeedStatus();
  }
  function selectEvent(event) {
    state.selectedRowid = event.rowid;
    const rows = $('event-rows').children;
    const index = state.visibleEvents.findIndex((item) => item === event);
    for (let i = 0; i < rows.length; i++) rows[i].classList.toggle('is-selected', i === index);
    $('inspect-empty').hidden = true;
    const content = $('inspect-content');
    content.hidden = false;
    content.replaceChildren();
    const badgeWrap = node('div', 'inspect-priority'); badgeWrap.append(priorityBadge(event));
    content.append(badgeWrap, node('div', 'inspect-title', `${kindLabel(event.kind)} · ${event.comm || '프로세스 미상'}`));
    const list = node('dl', 'inspect-fields');
    const fields = [
      ['추정 시각', eventTime(event)], ['프로세스', event.comm], ['PID / UID', `${event.pid ?? '—'} / ${event.uid ?? '—'}`],
      ['경로', event.path], ['결과', event.result],
      ['최종 규칙 ID', event.rule_id === 0 ? '규칙 미선택' : event.rule_id],
      ['원래 중요도', event.original_priority == null ? null :
        priorityNames[priorityValue(event.original_priority)] || event.original_priority],
      ['설정된 폭주 정책', event.configured_overload_policy == null ? null :
        policyLabel(event.configured_overload_policy)],
      ['실제 처리 상태', event.effective_policy == null ? null : policyLabel(event.effective_policy)],
      ['규칙 버전', event.rule_version], ['정책 버전', event.policy_version],
      ['실행 ID', event.run_id || feedRunId()], ['행 ID', event.rowid], ['모노토닉 ns', event.monotonic_ns]
    ];
    for (const [label, value] of fields) {
      if (value == null || value === '') continue;
      const field = node('div', 'inspect-field');
      field.append(node('dt', '', label), node('dd', '', value));
      list.append(field);
    }
    content.append(list, node('pre', 'inspect-json', JSON.stringify(event, null, 2)));
  }

  async function refreshRules() {
    if (!state.session || state.rulesBusy || state.rulesDirty) return;
    try {
      const data = await api('/api/rules');
      state.rules = Array.isArray(data.rules) ? structuredClone(data.rules) : [];
      state.savedRules = structuredClone(state.rules);
      state.ruleSchema = data.schema === 'event_rules_v1' ? 'event_rules_v1' : 'legacy';
      state.editorSchema = state.ruleSchema;
      state.upgradePending = false;
      state.revision = data.revision || '';
      state.pendingApply = data.pending_apply === true ? true : data.pending_apply === false ? false : null;
      state.ruleDemo = data.validation === 'DEMO_SHAPE_ONLY';
      state.rulesDirty = false;
      renderRules();
    } catch (error) {
      text('rules-state', error.message);
      $('rules-state').className = 'rules-state error';
    }
  }
  function collectRules() {
    return Array.from($('rule-list').children).map((row) => ({
      id: Number(row.querySelector('[data-field="id"]').value),
      event_type: row.querySelector('[data-field="event_type"]').value,
      path: row.querySelector('[data-field="path"]').value.trim(),
      protected: row.querySelector('[data-field="protected"]').checked
    }));
  }
  function markRulesDirty() {
    state.rulesDirty = true;
    text('rules-state', '저장되지 않은 변경사항');
    $('rules-state').className = 'rules-state dirty';
    updateRuleActions();
  }
  function ruleApplyLabel() {
    if (state.ruleDemo) return '데모 규칙 저장됨';
    if (state.pendingApply === true) return '저장됨 · 다음 수집 실행에 적용';
    if (state.pendingApply === false) return '최근 실행 설정과 일치';
    return '저장된 설정 · 최근 실행 적용 여부 확인 불가';
  }
  function renderRules() {
    const advanced = state.editorSchema === 'event_rules_v1';
    $('rule-upgrade').hidden = advanced;
    $('advanced-rules-panel').hidden = !advanced;
    document.querySelector('.rules-layout').classList.toggle('is-advanced', advanced);
    document.querySelector('.rules-panel').hidden = advanced;
    document.querySelector('.explainer').hidden = advanced;
    if (advanced) {
      $('advanced-rules-json').value = JSON.stringify(state.rules, null, 2);
      text('advanced-rule-count', `${state.rules.length} / 8`);
      text('advanced-rules-revision', state.revision ? `revision ${state.revision.slice(0, 12)}` : '');
      text('advanced-rules-state', state.upgradePending ? '고급 규칙 전환 준비 · 저장 전 JSON을 검토하세요.' :
        ruleApplyLabel());
      $('advanced-rules-state').className = `rules-state ${state.rulesDirty || state.pendingApply !== false ? 'dirty' : 'saved'}`;
      renderAdvancedSummary();
      updateRuleActions();
      return;
    }
    const list = $('rule-list');
    const fragment = document.createDocumentFragment();
    for (const [index, rule] of state.rules.entries()) {
      const row = node('div', 'rule-row');
      const id = node('input', 'rule-id');
      id.type = 'number'; id.min = '1'; id.max = '255'; id.value = rule.id ?? '';
      id.setAttribute('data-field', 'id'); id.setAttribute('aria-label', `규칙 ${index + 1} ID`);
      const kind = node('select', 'rule-kind');
      kind.setAttribute('data-field', 'event_type'); kind.setAttribute('aria-label', `규칙 ${index + 1} 이벤트 종류`);
      for (const value of ['openat', 'exec']) {
        const option = node('option', '', value); option.value = value; kind.append(option);
      }
      kind.value = rule.event_type || 'openat';
      const path = node('input', 'rule-path');
      path.type = 'text'; path.value = rule.path || ''; path.placeholder = '/var/log/example.log'; path.spellcheck = false;
      path.maxLength = 126; path.setAttribute('data-field', 'path'); path.setAttribute('aria-label', `규칙 ${index + 1} Linux 절대 경로`);
      const protectedWrap = node('label', 'checkbox-wrap');
      const protectedInput = node('input'); protectedInput.type = 'checkbox'; protectedInput.checked = !!rule.protected;
      protectedInput.setAttribute('data-field', 'protected'); protectedInput.setAttribute('aria-label', `규칙 ${index + 1} 격리 제외`);
      protectedWrap.append(protectedInput);
      const remove = node('button', 'rule-remove'); remove.type = 'button'; remove.title = '규칙 삭제';
      remove.setAttribute('aria-label', `규칙 ${index + 1} 삭제`); remove.append(icon('trash'));
      remove.disabled = state.rules.length <= 1;
      remove.addEventListener('click', () => {
        state.rules = collectRules().filter((_, itemIndex) => itemIndex !== index);
        renderRules(); markRulesDirty();
      });
      for (const input of [id, kind, path, protectedInput]) input.addEventListener('input', markRulesDirty);
      row.append(id, kind, path, protectedWrap, remove);
      fragment.append(row);
    }
    list.replaceChildren(fragment);
    text('rule-count', `${state.rules.length} / 8`);
    text('rules-revision', state.revision ? `revision ${state.revision.slice(0, 12)}` : '');
    if (!state.rulesDirty) {
      text('rules-state', ruleApplyLabel());
      $('rules-state').className = `rules-state ${state.pendingApply !== false ? 'dirty' : 'saved'}`;
    }
    updateRuleActions();
  }
  function updateRuleActions() {
    const advanced = state.editorSchema === 'event_rules_v1';
    $('add-rule').disabled = state.rules.length >= 8 || state.rulesBusy;
    $('reset-rules').disabled = !state.rulesDirty || state.rulesBusy;
    $('save-rules').disabled = !state.rulesDirty || state.rulesBusy;
    $('advanced-reset-rules').disabled = !state.rulesDirty || state.rulesBusy;
    $('advanced-save-rules').disabled = !state.rulesDirty || state.rulesBusy;
    $('apply-rules').disabled = state.rulesDirty || !state.revision || state.rulesBusy;
    text('apply-hint', state.rulesDirty ? '변경사항을 먼저 저장하세요.' :
      state.pendingApply === true ? '저장된 설정은 다음 수집 실행에 적용됩니다.' :
      state.pendingApply === null ? '현재 실행 적용 여부를 확인할 수 없습니다. 적용하면 새 수집 실행이 시작됩니다.' :
      '적용하면 새 수집 실행이 시작됩니다.');
    $('upgrade-rules').disabled = state.rulesBusy || state.ruleSchema !== 'legacy' || advanced;
  }
  function readAdvancedRules() {
    let rules;
    try { rules = JSON.parse($('advanced-rules-json').value); }
    catch (error) { throw new Error(`JSON 문법 오류: ${error.message}`); }
    if (!Array.isArray(rules) || rules.length < 1 || rules.length > 8) {
      throw new Error('event_rules는 1~8개 규칙의 JSON 배열이어야 합니다.');
    }
    return rules;
  }
  function renderAdvancedSummary(rules = state.rules) {
    const summary = $('advanced-rule-summary');
    summary.replaceChildren();
    if (!Array.isArray(rules)) return;
    for (const rule of rules) {
      const item = node('div', 'advanced-rule-item');
      item.append(node('strong', '', `RULE ${rule?.id ?? '—'}`),
        node('span', '', String(rule?.priority || '우선순위 미지정')),
        node('span', '', policyLabel(rule?.overload_policy)),
        node('small', '', `선택 순위 ${rule?.precedence ?? '기본'}${rule?.enabled === false ? ' · 비활성' : ''}`));
      summary.append(item);
    }
  }
  function markAdvancedDirty() {
    state.rulesDirty = true;
    try {
      const parsed = readAdvancedRules();
      text('advanced-rule-count', `${parsed.length} / 8`);
      renderAdvancedSummary(parsed);
      text('advanced-rules-state', '저장되지 않은 변경사항');
      $('advanced-rules-state').className = 'rules-state dirty';
    } catch (error) {
      text('advanced-rules-state', error.message);
      $('advanced-rules-state').className = 'rules-state error';
      $('advanced-rule-summary').replaceChildren();
    }
    updateRuleActions();
  }
  function upgradeRules() {
    if (state.ruleSchema !== 'legacy' || state.rulesBusy) return;
    const legacy = collectRules();
    const invalid = validateRules(legacy);
    if (invalid) { notification(invalid, true); return; }
    state.rules = legacy.map((rule) => ({
      id: rule.id,
      when: {all: [
        {field: 'event_type', op: 'eq', value: rule.event_type},
        {field: 'path', op: 'eq', value: rule.path}
      ]},
      priority: 'CRITICAL',
      overload_policy: rule.protected ? 'QUARANTINE_EXEMPT' : 'QUARANTINE'
    }));
    state.editorSchema = 'event_rules_v1';
    state.upgradePending = true;
    state.rulesDirty = true;
    renderRules();
    $('advanced-rules-json').focus();
  }
  function validateRules(rules) {
    if (rules.length < 1 || rules.length > 8) return '규칙은 1개 이상 8개 이하로 설정하세요.';
    const ids = new Set(); const targets = new Set();
    for (const [index, rule] of rules.entries()) {
      const label = `${index + 1}번 규칙`;
      if (!Number.isInteger(rule.id) || rule.id < 1 || rule.id > 255 || ids.has(rule.id)) return `${label}: 중복 없는 ID 1~255가 필요합니다.`;
      if (!['openat', 'exec'].includes(rule.event_type)) return `${label}: 이벤트 종류를 선택하세요.`;
      if (!rule.path.startsWith('/') || new TextEncoder().encode(rule.path).length > 126 || /[\x00-\x1f]/.test(rule.path) || rule.path.split('/').includes('..')) return `${label}: 126바이트 이하의 Linux 절대 경로를 입력하세요.`;
      const target = `${rule.event_type}\u0000${rule.path}`;
      if (targets.has(target)) return `${label}: 같은 종류와 경로의 규칙이 이미 있습니다.`;
      ids.add(rule.id); targets.add(target);
    }
    return '';
  }
  function addRule() {
    if (state.rules.length >= 8) return;
    state.rules = collectRules();
    const used = new Set(state.rules.map((rule) => rule.id));
    let id = 1; while (used.has(id)) id++;
    state.rules.push({id, event_type: 'openat', path: '', protected: false});
    renderRules(); markRulesDirty();
    $('rule-list').lastElementChild?.querySelector('[data-field="path"]')?.focus();
  }
  async function saveRules() {
    const advanced = state.editorSchema === 'event_rules_v1';
    let rules;
    try { rules = advanced ? readAdvancedRules() : collectRules(); }
    catch (error) { notification(error.message, true); return; }
    if (!advanced) {
      const invalid = validateRules(rules);
      if (invalid) { notification(invalid, true); text('rules-state', invalid); $('rules-state').className = 'rules-state error'; return; }
    }
    state.rulesBusy = true; updateRuleActions();
    try {
      const data = await api('/api/rules', {method: 'POST', body: {
        revision: state.revision, schema: state.editorSchema,
        rules_json: advanced ? $('advanced-rules-json').value : JSON.stringify(rules)
      }});
      state.rules = structuredClone(data.rules || rules);
      state.savedRules = structuredClone(state.rules);
      state.revision = data.revision || state.revision;
      state.ruleSchema = data.schema || state.editorSchema;
      state.editorSchema = state.ruleSchema;
      state.upgradePending = false;
      state.rulesDirty = false;
      state.pendingApply = data.pending_apply === true ? true : data.pending_apply === false ? false : null;
      state.ruleDemo = data.validation === 'DEMO_SHAPE_ONLY';
      notification('규칙을 저장했습니다. 적용하려면 수집기를 재시작하세요.');
      renderRules();
      await refreshOverview();
    } catch (error) {
      const id = advanced ? 'advanced-rules-state' : 'rules-state';
      text(id, error.message);
      $(id).className = 'rules-state error';
      notification(error.message, true);
    } finally { state.rulesBusy = false; updateRuleActions(); }
  }
  function confirmApply() {
    return new Promise((resolve) => {
      const dialog = $('apply-dialog');
      dialog.returnValue = 'cancel';
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {once: true});
      dialog.showModal();
    });
  }
  async function applyRules() {
    if (state.rulesDirty) { notification('변경한 규칙을 먼저 저장하세요.', true); return; }
    if (!state.revision || !(await confirmApply())) return;
    state.rulesBusy = true; updateRuleActions();
    try {
      const result = await api('/api/apply', {method: 'POST', body: {confirm: 'restart', revision: state.revision}});
      state.pendingApply = false;
      notification(result.message || (result.demo ? '데모 규칙 적용을 시뮬레이션했습니다.' : '수집기를 재시작하고 규칙을 적용했습니다.'));
      renderRules();
      resetFeed();
      await Promise.allSettled([refreshOverview(), refreshRuns(), refreshRules()]);
      await refreshEvents();
    } catch (error) { notification(error.message, true); }
    finally { state.rulesBusy = false; updateRuleActions(); }
  }

  async function refreshFiles() {
    if (state.filesLoading || !state.session) return;
    state.filesLoading = true;
    const run = fileRunId();
    try {
      const params = new URLSearchParams();
      if (run) params.set('run', run);
      const data = await api(`/api/files${params.size ? `?${params}` : ''}`);
      if (run !== fileRunId()) return;
      state.files = Array.isArray(data.files) ? data.files : [];
      state.filePath = data.path || state.overview?.data_root || '';
      renderFileLocation(); renderFiles();
    } catch (error) {
      state.files = []; state.filePath = state.overview?.data_root || '';
      renderFileLocation(); renderFiles();
      notification(error.message, true);
    } finally { state.filesLoading = false; }
  }
  function renderFileLocation() {
    text('file-path', state.filePath || state.overview?.data_root || '저장 경로 정보 없음');
    $('copy-file-path').disabled = !state.filePath;
  }
  function renderFiles() {
    const list = $('file-list');
    list.replaceChildren();
    $('files-empty').hidden = state.files.length > 0;
    for (const file of state.files) {
      const row = node('div', 'file-row');
      const name = node('div', 'file-name');
      const filename = node('strong', '', file.name || '이름 없음');
      filename.title = String(file.name || '');
      name.append(icon('folder'), filename);
      const size = node('span', 'file-meta', bytes(file.size));
      const modified = node('span', 'file-meta', dateLabel(file.modified_at));
      if (file.downloadable) {
        const link = node('a', 'button button-secondary');
        link.href = `/api/download?${new URLSearchParams({run: fileRunId(), file: file.name})}`;
        link.download = file.name;
        link.append(icon('download'), document.createTextNode('다운로드'));
        row.append(name, size, modified, link);
      } else {
        const unavailable = node('span', 'file-meta', '다운로드 제한');
        row.append(name, size, modified, unavailable);
      }
      list.append(row);
    }
  }
  async function copyFilePath() {
    if (!state.filePath) return;
    try {
      await navigator.clipboard.writeText(state.filePath);
      notification('서버의 저장 폴더 경로를 복사했습니다.');
    } catch (_) { notification(`클립보드 복사 실패: ${state.filePath}`, true); }
  }

  function csvCell(value) {
    let string = String(value == null ? '' : value);
    if (/^[\t\r\n]/.test(string) || /^[\s]*[=+\-@]/.test(string)) string = `'${string}`;
    return `"${string.replaceAll('"', '""')}"`;
  }
  function exportCsv() {
    const rows = state.visibleEvents;
    if (!rows.length) { notification('CSV로 저장할 표시 이벤트가 없습니다.', true); return; }
    const columns = ['wall_time', 'time_approximate', 'priority', 'original_priority', 'kind', 'comm',
      'pid', 'uid', 'path', 'result', 'effective_policy', 'rule_id', 'rule_version', 'policy_version', 'run_id', 'rowid'];
    const lines = [columns.map(csvCell).join(',')];
    for (const event of rows) lines.push(columns.map((column) => csvCell(event[column])).join(','));
    const blob = new Blob([`\ufeff${lines.join('\r\n')}\r\n`], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `mglogwh-events-${new Date().toISOString().replaceAll(':', '-')}.csv`;
    document.body.append(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    notification(`${count(rows.length)}건의 표시 이벤트를 CSV로 저장했습니다.`);
  }
  function setPaused(value) {
    state.paused = value;
    const button = $('pause-button');
    button.setAttribute('aria-pressed', String(value));
    button.replaceChildren(icon(value ? 'play' : 'pause'), document.createTextNode(value ? '다시 시작' : '일시정지'));
    renderFeedStatus();
    if (!value) refreshEvents().catch((error) => notification(error.message, true));
  }
  function showView(view) {
    if (!viewNames[view]) view = 'overview';
    state.activeView = view;
    for (const name of Object.keys(viewNames)) {
      const element = $(`view-${name}`);
      element.hidden = name !== view;
      element.classList.toggle('is-active', name === view);
    }
    for (const button of document.querySelectorAll('.nav-item')) button.classList.toggle('is-active', button.dataset.view === view);
    text('breadcrumb-view', viewNames[view]);
    if (location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
    if (view === 'runs') refreshRuns().catch((error) => notification(error.message, true));
    if (view === 'rules') refreshRules().catch((error) => notification(error.message, true));
    if (view === 'files') refreshFiles().catch((error) => notification(error.message, true));
    window.scrollTo({top: 0, behavior: 'smooth'});
  }
  function bind() {
    for (const button of document.querySelectorAll('.nav-item')) button.addEventListener('click', () => showView(button.dataset.view));
    for (const button of document.querySelectorAll('[data-go]')) button.addEventListener('click', () => showView(button.dataset.go));
    $('login-form').addEventListener('submit', login);
    $('refresh-button').addEventListener('click', () => Promise.allSettled([refreshOverview(), refreshRuns(), refreshEvents(), state.activeView === 'files' ? refreshFiles() : Promise.resolve()]).then(() => notification('화면을 새로고침했습니다.')));
    $('runs-refresh').addEventListener('click', () => refreshRuns().catch((error) => notification(error.message, true)));
    $('event-search').addEventListener('input', (event) => { state.query = event.target.value.trim(); renderEvents(); });
    for (const chip of document.querySelectorAll('.filter-chip')) chip.addEventListener('click', () => {
      state.priority = chip.dataset.priority;
      for (const item of document.querySelectorAll('.filter-chip')) item.classList.toggle('is-active', item === chip);
      renderEvents();
    });
    $('feed-run-select').addEventListener('change', (event) => {
      state.feedRun = event.target.value;
      resetFeed();
      refreshEvents().catch((error) => notification(error.message, true));
    });
    $('file-run-select').addEventListener('change', (event) => {
      state.fileRun = event.target.value;
      state.files = []; state.filePath = '';
      renderFileLocation(); renderFiles();
      refreshFiles().catch((error) => notification(error.message, true));
    });
    $('pause-button').addEventListener('click', () => setPaused(!state.paused));
    $('csv-button').addEventListener('click', exportCsv);
    $('add-rule').addEventListener('click', addRule);
    $('upgrade-rules').addEventListener('click', upgradeRules);
    $('advanced-rules-json').addEventListener('input', markAdvancedDirty);
    $('advanced-save-rules').addEventListener('click', saveRules);
    $('advanced-reset-rules').addEventListener('click', () => {
      state.rules = structuredClone(state.savedRules);
      state.editorSchema = state.ruleSchema;
      state.upgradePending = false;
      state.rulesDirty = false;
      renderRules();
    });
    $('reset-rules').addEventListener('click', () => {
      state.rules = structuredClone(state.savedRules);
      state.rulesDirty = false;
      renderRules();
    });
    $('save-rules').addEventListener('click', saveRules);
    $('apply-rules').addEventListener('click', applyRules);
    $('copy-file-path').addEventListener('click', copyFilePath);
    window.addEventListener('hashchange', () => showView(location.hash.slice(1)));
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.session) Promise.allSettled([refreshOverview(), refreshEvents()]);
    });
    const tickClock = () => text('top-clock', timeFormat.format(new Date()));
    tickClock(); window.setInterval(tickClock, 1000);
  }
  bind();
  showView(location.hash.slice(1) || 'overview');
  boot();
})();
