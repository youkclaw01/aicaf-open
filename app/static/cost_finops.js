// 비용 v2 화면. 금융 계산은 서버 IR만 표시하며 실제 비용·사용량·추정을 분리한다.
(function () {
  'use strict';

  const state = {
    aid: null, result: null, base: null, draft: {}, seq: 0,
    loading: false, saving: false, dirty: false, building: false,
    error: '', notice: '', job: null, history: [], selectedHistory: null,
    rowPage: 0, rowSearch: '', nodes: new Map(), controllers: new Set(), pollTimer: null
  };
  let root = null;
  const clone = value => JSON.parse(JSON.stringify(value == null ? {} : value));
  const escape = value => String(value == null ? '' : value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const array = value => Array.isArray(value) ? value : [];
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const statusLabels = { known: '확인됨', collected: '수집됨', partial: '일부 수집', missing: '미수집', not_collected: '미수집', collection_failed: '수집 실패', no_records: '조회 결과 없음', not_estimated: '미산정', scenario_estimate: '사용자 확인 시나리오', no_confirmed_actions: '확인된 조치 없음', confirmation_required: '가정 확인 필요' };
  const statusText = value => statusLabels[value] || value || '미확인';
  const basisText = value => ({ ActualCost: '실제 청구 · Actual', AmortizedCost: '상각 비용 · Amortized' }[value] || value || '기준 미확인');
  const basePath = () => '/api/aicaf/assessments/' + encodeURIComponent(state.aid) + '/cost/v2';
  const periodText = context => {
    const period = (context || {}).period || {};
    return (period.start || '시작 미확인') + ' ~ ' + (period.end || '종료 미확인');
  };

  function format(value, unit) {
    if (!finite(value)) return '미산정';
    if (unit === '비율') return new Intl.NumberFormat('ko-KR', { style: 'percent', maximumFractionDigits: 2 }).format(value);
    const number = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: Math.abs(value) > 0 && Math.abs(value) < 0.01 ? 6 : 2 }).format(value);
    return unit ? unit + ' ' + number : number;
  }

  function traceSources(id) {
    const found = new Set(), visited = new Set();
    function visit(key) {
      if (visited.has(key) || visited.size > 512 || found.size >= 6) return;
      visited.add(key);
      const node = state.nodes.get(key);
      if (!node) return;
      if (node.expression) array(node.expression.args).forEach(visit);
      else if (node.source) found.add(node.source);
    }
    if (id) visit(id);
    return Array.from(found).join(' · ');
  }

  function nodeContext(id) {
    const data = state.base || {};
    const context = data.context || {};
    const key = String(id || '');
    if (key.startsWith('token_')) return { ...context, basis: '관측 토큰·요청 또는 선택 소매가격 추정(청구 실적과 독립)' };
    if (key.startsWith('new_')) return { basis: '신규 도입 확인 가정·선택 소매가격 추정', period: { start: data.new && data.new.baseline_month, end: data.new && data.new.baseline_month } };
    if (key.startsWith('projection_') || key === 'transition_cost') return { ...context, basis: basisText(context.basis) + ' · 동일 일평균 사용량 환산 시나리오(공식 예측·실적 아님)' };
    const budget = /^budget_(\d+)_/.exec(key);
    if (budget) {
      const row = array(data.budgets && (data.budgets.rows || data.budgets.records))[Number(budget[1])] || {};
      const period = row.period || {};
      return { basis: '예산 원문 자체 범위', period: { start: period.startDate || period.start, end: period.endDate || period.end } };
    }
    if (key === 'display_fx' || key === 'fx_rate') return { ...context, basis: basisText(context.basis) + ' · 출처 확인 표시 환산(청구 통화와 분리)' };
    return context;
  }

  function metricInfo(metric, context) {
    const item = metric || {};
    const node = state.nodes.get(item.node) || {};
    const source = node.source || item.source || item.reason || '출처 미확인';
    const expression = node.expression;
    const formula = expression ? expression.op + '(' + array(expression.args).join(', ') + ')' : '입력 원문';
    const basis = context || nodeContext(item.node);
    return '계산 참조: ' + (item.node || '원문·계산 참조 없음') + '\n출처: ' + source + '\n원본 입력 추적(최대 6개 출처): ' + (traceSources(item.node) || source) + '\n식: ' + formula + '\n원장 기준: ' + basisText(basis.basis) + '\n기간: ' + periodText(basis) + '\n상태: ' + statusText(item.status || node.status) + '\n원장 값: ' + (finite(item.value) ? String(item.value) : '미산정');
  }

  function metric(metricValue, context) {
    const item = metricValue || {};
    const title = metricInfo(item, context);
    return '<span class="cf-value' + (!finite(item.value) ? ' cf-na' : '') + '" data-node="' + escape(item.node || '') + '" tabindex="0" title="' + escape(title) + '">' + escape(finite(item.value) ? format(item.value, item.unit) : statusText(item.status) === '미수집' && (!item.node || state.nodes.get(item.node) && state.nodes.get(item.node).kind === 'input') ? '미수집' : '미산정') + '<span class="cf-info" aria-hidden="true"> ⓘ</span></span>';
  }

  function abortWork() {
    state.seq += 1;
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.controllers.forEach(controller => controller.abort());
    state.controllers.clear();
    return state.seq;
  }

  async function jsonRequest(path, options, seq) {
    const controller = new AbortController();
    state.controllers.add(controller);
    try {
      const response = await fetch(path, { credentials: 'same-origin', redirect: 'error', ...options, headers: { 'Content-Type': 'application/json' }, signal: controller.signal });
      if (seq !== state.seq) throw new DOMException('이전 요청', 'AbortError');
      const contentType = response.headers.get('Content-Type') || '';
      if (!contentType.includes('application/json')) throw new Error(response.status === 401 ? '로그인 세션이 만료되었습니다. 다시 로그인하십시오.' : '서버 응답이 JSON 형식이 아닙니다. 로그인 세션과 API 경로를 확인하십시오.');
      const data = await response.json();
      if (!response.ok) {
        const message = data.error || data.detail || data.message || '요청 처리 실패';
        const error = new Error((response.status === 409 ? '다른 변경이 먼저 저장되었습니다. 최신 결과를 다시 불러오십시오. ' : response.status === 403 ? '이 작업을 수정할 권한이 없습니다. ' : '') + (typeof message === 'string' ? message : JSON.stringify(message)));
        error.status = response.status;
        throw error;
      }
      return data;
    } finally { state.controllers.delete(controller); }
  }

  function acceptResult(data, allowJob) {
    if (!data || data.schema !== 'aicaf-finops/v2' || typeof data.analysis_id !== 'string' || !data.context || !data.datasets || !Array.isArray(data.calculation) || !Array.isArray(data.rows)) throw new Error('비용 분석 응답의 계약이 유효하지 않습니다.');
    state.result = data;
    state.base = data;
    state.draft = clone(data.options);
    state.nodes = new Map(data.calculation.map(node => [node.id, node]));
    state.pricesByRef = new Map(array(data.prices).map(price => [price.ref, price]));
    state.loading = false;
    state.saving = false;
    state.dirty = false;
    state.rowPage = 0;
    state.job = allowJob && data.job && data.job.analysis_id === data.analysis_id ? data.job : null;
    state.history = array(Array.isArray(data.history) ? data.history : data.history && data.history.runs);
  }

  const dimensionLabels = { month: '월', date: '일자', subscription: '구독', resource_group: '리소스 그룹', service: '서비스', resource: '자원', region: '현재 지역', sku: '현재 SKU', meter: '미터', tag_value: '청구 태그값', pricing_model: '과금 모델', charge_type: '청구 유형' };
  const canEdit = () => !!(state.base && state.base.can_edit === true && !state.loading && !state.saving);
  const disabled = extra => !canEdit() || extra ? ' disabled' : '';
  const mutation = extra => ' data-mutation' + disabled(extra);
  const selected = (value, current) => String(value) === String(current) ? ' selected' : '';
  const cfg = () => state.base && state.base.datasets[state.draft.dataset || state.base.context.dataset] || {};

  function selectField(label, path, choices, current, extra) {
    return '<label><span>' + escape(label) + '</span><select aria-label="' + escape(label) + '" data-path="' + escape(path) + '"' + mutation(extra) + '>' + choices.map(choice => {
      const pair = Array.isArray(choice) ? choice : [choice, choice];
      return '<option value="' + escape(pair[0]) + '"' + selected(pair[0], current) + '>' + escape(pair[1]) + '</option>';
    }).join('') + '</select></label>';
  }

  function headerView(data) {
    const context = data.context;
    const config = cfg();
    const options = state.draft;
    const source = data.inputs && data.inputs.source_period || {};
    const whole = config.date_mode === 'collection_period_only';
    const dims = array(config.dimensions).filter(dim => dim !== 'tag');
    const currencies = array(config.currencies).slice();
    if (!currencies.includes(options.currency || context.currency)) currencies.push(options.currency || context.currency);
    const period = context.period || {};
    const start = options.start || String(source.fromUtc || period.start || '').slice(0, 10);
    const end = options.end || String(source.toUtc || period.end || '').slice(0, 10);
    const filters = options.filters || {};
    let fields = selectField('원장', 'dataset', Object.entries(data.datasets).map(([key, item]) => [key, item.label + ' · ' + statusText(item.status)]), options.dataset || context.dataset);
    fields += selectField('청구 기준', 'basis', array(config.basis).map(value => [value, basisText(value)]), options.basis || context.basis);
    fields += selectField('원문 통화', 'currency', currencies, options.currency || context.currency);
    fields += selectField('집계 차원', 'group_by', dims.map(dim => [dim, dimensionLabels[dim] || dim]), options.group_by || context.group_by);
    fields += '<label><span>시작일</span><input aria-label="시작일" type="date" data-path="start" value="' + escape(start) + '"' + mutation(whole) + '></label><label><span>종료일</span><input aria-label="종료일" type="date" data-path="end" value="' + escape(end) + '"' + mutation(whole) + '></label>';
    if (dims.includes('month')) fields += selectField('조회 월', 'filters.month', [['', '전체 수집 월'], ...array(config.values && config.values.month).map(value => [value, value])], filters.month || '');
    if ((options.dataset || context.dataset) === 'tag') fields += selectField('청구 태그키', 'tag_key', array(config.tag_keys), options.tag_key || array(config.tag_keys)[0]);
    const filterFields = dims.filter(dim => dim !== 'month').map(dim => selectField(dimensionLabels[dim] || dim, 'filters.' + dim, [['', '전체'], ...array(config.values && config.values[dim]).map(value => [value, value])], filters[dim] || '')).join('');
    return '<section class="card cf-header"><div class="cf-heading"><h2>비용 분석</h2><span class="pill">' + (data.track === 'new' ? '신규 도입 시나리오' : '기구축 비용 최적화') + '</span>' + (!data.can_edit ? '<span class="pill">조회 전용 · 조건·가정 수정 불가</span>' : '') + '</div><p class="muted">' + escape(data.customer || '') + (data.data_classification ? ' · ' + escape(data.data_classification) : '') + ' · 수집 ' + escape(data.inputs && data.inputs.collected_at || '시각 미확인') + '</p><div class="fg cf-controls">' + fields + '</div><p class="cf-grain muted">' + escape(config.grain || context.grain) + '</p>' + (whole ? '<p class="banner">수집 전체기간 원장입니다. 부분기간 조회에는 일별 상세 비용 수집이 필요합니다. 날짜 입력은 비활성화합니다.</p>' : config.date_mode === 'monthly' ? '<p class="muted">월 집계는 월 중간으로 나누지 않습니다. 전체 수집 월 또는 완전한 월 범위를 선택하십시오. 일별 비용과의 교차 조회는 지원하지 않습니다.</p>' : '<p class="muted">수집된 일별 행의 기간만 조회합니다.</p>') + '<details class="cf-filters" data-section="filters"><summary>지원 차원 드릴다운</summary><div class="fg cf-controls">' + filterFields + '</div>' + (array(config.dimensions).includes('tag') ? '<p class="muted">현재 태그는 아래 자원 배분에서 키별로 조회합니다. 청구 태그 원장은 별도 수집 집계이며 합산하지 않습니다.</p>' : '') + '<button class="sec" type="button" data-click="filters-reset"' + mutation() + '>필터·기간 해제</button></details><p class="muted">Actual과 Amortized는 선택 원장에서 지원하는 기준만 제공합니다. 통화 간 합산은 하지 않습니다. 할인·크레딧·세금 포함 여부는 수집 원문에 따르며 미확인 항목은 추정하지 않습니다.</p></section>';
  }

  function summaryView(data) {
    return '<section class="card cf-calculated" data-calculated><div class="cf-summary"><div><span class="muted">선택 범위 비용</span><strong>' + metric(data.summary.total) + '</strong><small class="muted">' + escape(statusText(data.summary.total.status)) + '</small></div><div><span class="muted">금액 확인 부분합</span><strong>' + metric(data.summary.known_subtotal) + '</strong><small class="muted">미확인 금액 ' + escape(data.summary.unknown_amount_count) + '행 · 조회 ' + escape(data.summary.row_count) + '행</small></div></div><p class="muted">' + escape(periodText(data.context)) + ' · ' + escape(basisText(data.context.basis)) + ' · 원문 통화 ' + escape(data.context.currency) + '</p></section>';
  }

  function groupView(data) {
    const groups = array(data.groups).slice(0, 10);
    const maximum = Math.max(1, ...groups.filter(group => finite(group.value)).map(group => Math.abs(group.value)));
    const groupBy = data.context.group_by;
    const time = groupBy === 'month' || groupBy === 'date';
    const sorted = time ? array(data.groups).slice().sort((a, b) => String(a.label).localeCompare(String(b.label))) : groups;
    let chart = '';
    if (time && sorted.some(group => finite(group.value))) {
      const height = 190, width = 720;
      const known = sorted.filter(group => finite(group.value));
      const minimum = Math.min(0, ...known.map(group => group.value));
      const maxValue = Math.max(1, ...known.map(group => group.value));
      // 위치·길이만 변환한다. 축·표시 금액은 서버 원장 값을 그대로 사용한다.
      const x = index => 40 + index * 640 / Math.max(1, sorted.length - 1);
      const y = value => 20 + 125 * (maxValue - value) / (maxValue - minimum);
      let segment = [];
      let lines = '';
      sorted.forEach((group, index) => {
        if (!finite(group.value)) { if (segment.length) lines += '<polyline points="' + segment.join(' ') + '"/>'; segment = []; return; }
        segment.push(x(index) + ',' + y(group.value));
      });
      if (segment.length) lines += '<polyline points="' + segment.join(' ') + '"/>';
      chart = '<svg class="cf-trend" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="' + (groupBy === 'date' ? '일별 비용 추이' : '월별 비용 추이') + '"><g fill="none" stroke="var(--blue)" stroke-width="2">' + lines + '</g>' + sorted.map((group, index) => finite(group.value) ? '<circle cx="' + x(index) + '" cy="' + y(group.value) + '" r="4" fill="var(--blue)"><title>' + escape(group.label + '\n' + metricInfo(group)) + '</title></circle><text x="' + x(index) + '" y="173" text-anchor="middle">' + escape(group.label) + '</text>' : '').join('') + '</svg>';
    }
    return '<div class="cf-composition"><h3>' + (time ? '비용 추이' : '상위 비용 · ' + escape(dimensionLabels[groupBy] || groupBy)) + '</h3><p class="muted">선택한 원장의 같은 기간·통화·기준만 표시합니다.' + (!time && data.groups.length > 10 ? ' 상위 10개만 표시하며 나머지 금액을 임의 합산하지 않습니다.' : '') + '</p>' + chart + (groups.length ? '<ol class="cf-bars">' + (time ? sorted : groups).map(group => {
      const index = data.groups.indexOf(group);
      return '<li><div class="cf-bar-label"><button class="cf-link" data-click="drill" data-index="' + index + '"' + mutation(group.label === '미확인/미배분') + '>' + escape(group.label) + '</button>' + metric(group) + '</div><div class="cf-bar-track"><span class="cf-bar-fill' + (group.value < 0 ? ' cf-negative' : '') + '" style="width:' + (finite(group.value) ? Math.min(100, Math.abs(group.value) / maximum * 100) : 0) + '%" title="' + escape(metricInfo(group)) + '"></span></div></li>';
    }).join('') + '</ol>' : '<p class="cf-na">' + escape(statusText(data.summary.total.status)) + ' · ' + escape(cfg().reason || '선택한 범위에 비용 행이 없습니다. 미수집 금액은 0으로 표시하지 않습니다.') + '</p>') + '</div>';
  }

  function rowsView(data) {
    const search = state.rowSearch.toLocaleLowerCase();
    const rows = array(data.rows).map((row, index) => ({ row, index })).filter(item => !search || [item.row.resource_name, item.row.resource, item.row.service, item.row.meter, item.row.source, item.row.month, item.row.date].join(' ').toLocaleLowerCase().includes(search));
    const pages = Math.max(1, Math.ceil(rows.length / 25));
    state.rowPage = Math.min(state.rowPage, pages - 1);
    const shown = rows.slice(state.rowPage * 25, state.rowPage * 25 + 25);
    return '<div class="cf-ledger-tools"><label class="cf-search"><span class="muted">표 검색 · 현재 조회 행만</span><input type="search" aria-label="표 검색" value="' + escape(state.rowSearch) + '" data-local="row-search" placeholder="자원·서비스·미터·출처"></label><span class="muted">표 ' + rows.length + '행 · 서버 합계와 무관한 표시 검색</span></div><div class="cf-table-scroll" tabindex="0" aria-label="정규화 비용 원장"><table><thead><tr><th>월·일자</th><th>자원·서비스·미터</th><th>구독·리소스 그룹</th><th>현재 지역·SKU</th><th class="cf-right">비용 · 원문 통화</th><th>근거</th></tr></thead><tbody>' + shown.map(item => {
      const row = item.row;
      return '<tr><td>' + escape(row.date || row.month || '수집 전체기간') + '<small>' + escape(basisText(row.basis)) + '</small></td><td><b>' + escape(row.resource_name || row.tag_value || row.pricing_model || '자원 차원 미수집') + '</b><small>' + escape([row.service, row.meter, row.charge_type, row.tag_key].filter(Boolean).join(' · ')) + '</small></td><td>' + escape(row.subscription || '미수집') + '<small>' + escape(row.resource_group || '미수집') + '</small></td><td>' + escape(row.region || '미수집') + '<small>' + escape(row.sku || '미수집') + '</small></td><td class="cf-right">' + metric({ value: row.amount, node: row.id, unit: row.currency, source: row.source, status: finite(row.amount) ? 'known' : 'missing' }) + '</td><td><button class="sec cf-small" data-click="row-open" data-index="' + item.index + '" aria-label="원장 행 ' + (item.index + 1) + ' 근거">상세</button></td></tr>';
    }).join('') + (shown.length ? '' : '<tr><td colspan="6" class="cf-na">표시할 행이 없습니다. 비용이 0이라는 의미가 아닙니다.</td></tr>') + '</tbody></table></div><div class="cf-pagination"><button class="sec cf-small" data-click="row-prev"' + (state.rowPage <= 0 ? ' disabled' : '') + '>이전</button><span class="muted">' + (state.rowPage + 1) + ' / ' + pages + '</span><button class="sec cf-small" data-click="row-next"' + (state.rowPage >= pages - 1 ? ' disabled' : '') + '>다음</button></div>';
  }

  function ledgerView(data) {
    return '<section class="card cf-calculated" id="cf-ledger" data-calculated>' + groupView(data) + '<h3 class="cf-subheading">정규화 비용 원장</h3><p class="muted">다른 원장의 한계 집계와 합산하지 않습니다. 지역·SKU는 수집 시점 인벤토리의 정확한 ARM ID 조인이며 청구 시점 속성을 의미하지 않습니다.</p><div id="cf-rows">' + rowsView(data) + '</div></section>';
  }

  const phases = [['candidate', '후보'], ['evaluated', '검토 완료'], ['planned', '계획'], ['applied', '적용 기록'], ['verified', '검증 기록'], ['blocked', '보류'], ['dismissed', '제외']];

  function editField(label, path, value, type, hint, limits) {
    const attributes = ' aria-label="' + escape(label) + '" data-path="' + escape(path) + '" data-edit' + mutation();
    const content = type === 'textarea' ? '<textarea rows="2" maxlength="2048"' + attributes + '>' + escape(value) + '</textarea>' : '<input type="' + (type || 'text') + '" value="' + escape(value == null ? '' : value) + '"' + (type === 'number' ? ' min="0" step="any"' : ' maxlength="2048"') + (limits || '') + attributes + '>';
    return '<label><span>' + escape(label) + '</span>' + content + (hint ? '<small class="muted">' + escape(hint) + '</small>' : '') + '</label>';
  }

  function editSelect(label, path, choices, value) {
    return selectField(label, path, choices, value).replace(' data-path=', ' data-edit data-path=');
  }

  function checkField(label, path, value, hint) {
    return '<label class="cf-check"><input type="checkbox" data-path="' + escape(path) + '" data-edit' + (value === true ? ' checked' : '') + mutation() + '><span>' + escape(label) + (hint ? '<small class="muted">' + escape(hint) + '</small>' : '') + '</span></label>';
  }

  function nodeMetric(id) {
    const node = state.nodes.get(id);
    return node ? { ...node, node: node.id } : { value: null, status: 'missing', node: id };
  }

  function waterfallView(data) {
    const comparison = data.comparison || {};
    const start = comparison.as_is || {};
    if (!finite(start.value)) return '<p class="cf-na">기준 비용 미확인으로 비용 워터폴을 표시할 수 없습니다.</p>';
    const bars = [{ label: 'AS-IS', low: 0, high: start.value, metric: start, kind: 'base' }];
    let previous = start.value;
    array(data.actions).forEach((action, index) => {
      const after = nodeMetric('action_' + index + '_overall');
      if (action.status !== 'estimated' || !finite(action.applied && action.applied.value) || !finite(after.value)) return;
      bars.push({ label: action.name || '확인 조치', low: after.value, high: previous, metric: action.applied, kind: 'effect' });
      previous = after.value;
    });
    if (comparison.recurring_cost && finite(comparison.recurring_cost.value) && finite(comparison.known_to_be && comparison.known_to_be.value) && finite(comparison.to_be && comparison.to_be.value)) bars.push({ label: '추가 운영비', low: comparison.known_to_be.value, high: comparison.to_be.value, metric: comparison.recurring_cost, kind: 'opex' });
    if (finite(comparison.to_be && comparison.to_be.value)) bars.push({ label: 'TO-BE', low: 0, high: comparison.to_be.value, metric: comparison.to_be, kind: 'base' });
    const minimum = Math.min(0, ...bars.map(bar => Math.min(bar.low, bar.high)));
    const maximum = Math.max(1, ...bars.map(bar => Math.max(bar.low, bar.high)));
    const width = Math.max(720, bars.length * 90), height = 230;
    const y = value => 28 + 140 * (maximum - value) / (maximum - minimum);
    const step = (width - 60) / bars.length;
    return '<div class="cf-chart-scroll"><svg class="cf-waterfall" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="확인 조치 비용 워터폴"><line x1="20" x2="' + (width - 20) + '" y1="' + y(0) + '" y2="' + y(0) + '" stroke="var(--line)"/>' + bars.map((bar, index) => {
      const top = y(Math.max(bar.high, bar.low));
      const bottom = y(Math.min(bar.high, bar.low));
      const x = 30 + step * index + step * 0.18;
      const color = bar.kind === 'effect' ? 'var(--green)' : bar.kind === 'opex' ? 'var(--muted)' : 'var(--blue)';
      return '<rect x="' + x + '" y="' + top + '" width="' + step * 0.64 + '" height="' + Math.max(1, bottom - top) + '" rx="3" fill="' + color + '"><title>' + escape(bar.label + '\n' + metricInfo(bar.metric)) + '</title></rect><text x="' + (x + step * 0.32) + '" y="' + Math.max(14, top - 7) + '" text-anchor="middle">' + escape(format(bar.metric.value, bar.metric.unit)) + '</text><text x="' + (x + step * 0.32) + '" y="198" text-anchor="middle"><title>' + escape(bar.label) + '</title>' + escape(bar.label.length > 9 ? bar.label.slice(0, 8) + '…' : bar.label) + '</text>';
    }).join('') + '</svg></div><p class="muted">조치 전·후 위치는 서버 원장의 버킷 잔액 참조를 사용합니다. 운영비 미확인 시 전체 TO-BE와 순절감은 미산정입니다. 이 값은 실현 절감 증거가 아닙니다.</p>';
  }

  function comparisonView(data) {
    const comparison = data.comparison || {};
    const items = [['AS-IS · 선택 기준', comparison.as_is], ['조치 총효과 · 중복 제거', comparison.gross_savings], ['추가 운영비 · 동일 기간', comparison.recurring_cost], ['TO-BE · 운영비 포함', comparison.to_be], ['순절감', comparison.savings], ['순절감률', comparison.savings_rate]];
    return '<div data-calculated><div class="cf-comparison">' + items.map(([label, item]) => '<div><span class="muted">' + escape(label) + '</span><strong>' + metric(item) + '</strong></div>').join('') + '</div>' + (comparison.to_be && comparison.to_be.status === 'no_confirmed_actions' ? '<p class="cf-na">확인된 조치가 없어 TO-BE는 현재 비용과 동일한 비교 기준만 표시합니다. 절감 가능액이 0으로 검증된 것은 아닙니다.</p>' : '') + waterfallView(data) + (array(data.actions).length ? '<h4>조치별 기여·겹침 제외</h4><div class="cf-table-scroll"><table><thead><tr><th>조치</th><th>계산 상태</th><th class="cf-right">요청 금액</th><th class="cf-right">적용 효과</th><th class="cf-right">겹침·잔액상한 제외</th><th>범위·근거</th></tr></thead><tbody>' + data.actions.map(action => '<tr><td>' + escape(action.name || '이름 미입력') + '<small>' + escape(statusText(action.phase)) + '</small></td><td>' + escape(statusText(action.status)) + '</td><td class="cf-right">' + metric(action.requested || { value: action.amount, unit: action.currency, source: action.evidence || '금액 근거 미입력', status: 'user_input' }) + '</td><td class="cf-right">' + metric(action.applied) + '</td><td class="cf-right">' + metric(action.overlap_removed) + '</td><td>' + escape(action.source || '') + '<small>' + escape(action.constraint || action.confidence || '확인되지 않은 조치는 합계에서 제외') + '</small></td></tr>').join('') + '</tbody></table></div>' : '<p class="muted">확인된 금액과 대상 원장 행이 있는 조치만 입력하십시오. 임의 절감률 기본값은 사용하지 않습니다.</p>') + '</div>';
  }

  function actionsView(data) {
    const rows = array(data.rows).filter(row => finite(row.amount) && row.amount > 0);
    const actions = array(state.draft.actions);
    return '<div id="cf-actions">' + actions.map((action, index) => '<fieldset class="cf-action"><legend>확인 조치 ' + (index + 1) + '</legend><div class="fg cf-controls">' + editField('조치명', 'actions.' + index + '.name', action.name) + editField('확인 절감액 · ' + (action.currency || data.context.currency), 'actions.' + index + '.amount', action.amount, 'number', '선택기간·같은 기준의 확인 금액. 임의 절감률 사용 안 함') + editSelect('진행 상태', 'actions.' + index + '.phase', phases, action.phase || 'candidate') + editField('담당자', 'actions.' + index + '.owner', action.owner) + editField('금액·범위 근거', 'actions.' + index + '.evidence', action.evidence, 'textarea') + editField('적용 조건·제약', 'actions.' + index + '.constraint', action.constraint, 'textarea') + '<label class="cf-scope"><span>대상 원장 행 · 다중 선택</span><select multiple size="5" aria-label="대상 원장 행" data-path="actions.' + index + '.row_ids" data-edit' + mutation() + '>' + rows.map(row => '<option value="' + escape(row.id) + '"' + (array(action.row_ids).includes(row.id) ? ' selected' : '') + '>' + escape(row.id + ' · ' + (row.resource_name || row.month || row.resource || '') + ' · ' + (row.meter || row.service || '')) + '</option>').join('') + '</select><small class="muted">현재 저장된 조회의 양수·확인 금액 행만 제공합니다. 여러 조치는 입력 순서대로 같은 버킷 잔액을 차감합니다.</small></label></div><p class="muted">조치 기준 ' + escape(action.currency || '통화 미입력') + ' · ' + escape(basisText(action.basis)) + ' · 범위 스냅샷 ' + escape(action.evidence_snapshot ? action.evidence_snapshot.slice(0, 12) : '확인 전') + '</p><div class="cf-checks">' + checkField('현재 원장·기간·금액 근거의 적용 범위를 확인함', 'actions.' + index + '.scope_confirmed', action.scope_confirmed) + checkField('이 확인 조치를 시나리오에 포함', 'actions.' + index + '.enabled', action.enabled) + '</div><div class="cf-action-buttons"><button class="sec cf-small" data-click="action-up" data-index="' + index + '"' + mutation(index === 0) + '>위로</button><button class="sec cf-small" data-click="action-down" data-index="' + index + '"' + mutation(index === actions.length - 1) + '>아래로</button><button class="del" data-click="action-remove" data-index="' + index + '"' + mutation() + '>조치 제외</button></div></fieldset>').join('') + '</div><button class="sec" data-click="action-add"' + mutation(actions.length >= 50) + '>확인 조치 추가</button>';
  }

  function scenarioView(data) {
    const projection = state.draft.projection || {};
    const fx = state.draft.fx || {};
    return '<section class="card" id="cf-scenario"><h3>현재 비용에서 개선 후 비용까지</h3><p class="muted">같은 원장·통화·기간의 확인 조치만 비교합니다. 진행 상태는 사용자 기록이며 실제 절감 실현을 뜻하지 않습니다.</p>' + (state.result ? comparisonView(state.result) : '<p class="cf-na">저장·재계산 전 조치 효과는 표시하지 않습니다.</p>') + '<h4>금액·범위가 확인된 조치</h4>' + actionsView(data) + '<h4 class="cf-subheading">추가 운영비·전환비</h4><div class="fg cf-controls">' + editField('추가 운영비 · 동일 조회기간', 'recurring_cost', state.draft.recurring_cost, 'number', '없음이 확인된 경우에만 0을 명시적으로 입력') + editField('추가 운영비 근거', 'recurring_cost_evidence', state.draft.recurring_cost_evidence, 'textarea') + editField('전환 일회성 비용', 'transition_cost', state.draft.transition_cost, 'number') + editField('전환 비용 근거', 'transition_cost_evidence', state.draft.transition_cost_evidence, 'textarea') + '</div><details data-section="projection" class="cf-filters"><summary>월·연간 환산과 회수기간 · 선택 가정</summary><div class="cf-checks">' + checkField('동일 일평균 사용량·단가·조치 효과의 반복 가정 확인', 'projection.enabled', projection.enabled, '공식 예측이나 연간 실적이 아님') + '</div><div class="fg cf-controls">' + editField('기간 환산 근거', 'projection.evidence', projection.evidence, 'textarea') + editField('환산 연도', 'projection.year', projection.year, 'number', '', ' max="2200" step="1"') + editField('누적 비교 개월', 'projection.horizon_months', projection.horizon_months, 'number', '1~120개월. 비워 두면 서버 계약의 비교기간을 사용', ' max="120" step="1"') + '</div>' + (state.result ? '<div class="cf-comparison" data-calculated>' + [['월 환산 순절감', state.result.projection.monthly_savings], ['연간 환산 순절감', state.result.projection.annual_savings], ['전환 비용', state.result.projection.transition_cost], ['회수기간', state.result.projection.payback_months]].map(([label, item]) => '<div><span class="muted">' + label + '</span><strong>' + metric(item) + '</strong></div>').join('') + '</div><p class="muted">' + array(state.result.projection.assumptions).map(escape).join(' · ') + '</p><details data-section="cumulative"><summary>서버 원장의 월별 누적 순효과</summary><div class="cf-table-scroll"><table><thead><tr><th>비교 월</th><th>전환비 차감 누적 효과</th></tr></thead><tbody>' + array(state.result.projection.cumulative).map(point => '<tr><td>' + escape(point.month) + '</td><td>' + metric(point) + '</td></tr>').join('') + '</tbody></table></div></details>' : '') + '</details><details data-section="fx" class="cf-filters"><summary>표시 환율 · 청구 원문과 분리</summary><p class="muted">환율·기준일·출처가 모두 확인된 경우에만 별도 표시합니다. 원문 통화 비용과 합산하지 않습니다.</p><div class="fg cf-controls">' + editField('표시 환산 통화', 'fx.target_currency', fx.target_currency) + editField('표시 환율', 'fx.rate', fx.rate, 'number') + editField('환율 기준일', 'fx.date', fx.date, 'date') + editField('환율 출처', 'fx.source', fx.source, 'textarea') + '</div>' + (state.result ? '<p data-calculated>별도 표시 환산 ' + metric(state.result.display_fx) + '</p>' : '') + '</details></section>';
  }

  function priceLabel(price) {
    return price ? [price.product, price.meter, price.sku, price.unit, price.region, format(price.price == null ? price.amount : price.price, price.currency)].filter(Boolean).join(' · ') : '실제 수집 단가 선택';
  }

  function priceButton(label, kind, index, key, ref) {
    const price = state.pricesByRef && state.pricesByRef.get(ref);
    return '<label class="cf-price-field"><span>' + escape(label) + '</span><button class="sec cf-price-button" data-click="price-open" data-price-kind="' + kind + '" data-price-index="' + index + '" data-price-key="' + key + '"' + mutation() + '>' + escape(priceLabel(price)) + '</button></label>';
  }

  function mappingFor(index, create) {
    const token = array(state.base.ai && state.base.ai.tokens)[index];
    if (!token) return null;
    const mappings = array(state.draft.ai_mappings);
    let mapping = mappings.find(item => String(item.account || '').toLowerCase() === String(token.account || '').toLowerCase() && item.deployment === token.deployment);
    if (!mapping && create) {
      mapping = { account: token.account, deployment: token.deployment || '', evidence: '', target_confirmed: false };
      if (!state.draft.ai_mappings) state.draft.ai_mappings = [];
      state.draft.ai_mappings.push(mapping);
    }
    return mapping;
  }

  function aiView(data) {
    const ai = data.ai || {};
    const tokens = array(ai.tokens);
    const accounts = new Set(tokens.map(token => String(token.account || '').toLowerCase()));
    const billed = array(ai.billed_rows).length ? ai.billed_rows : data.context.dataset === 'ai_daily' ? data.rows : array(data.rows).filter(row => row.resource && accounts.has(String(row.resource).toLowerCase()));
    const retailContext = { ...data.context, basis: '관측 사용량 × 사용자 선택 소매단가(청구비용 아님)' };
    const measuredContext = { ...data.context, basis: 'Azure Monitor 관측 사용량(청구 기준과 독립)' };
    return '<section class="card" id="cf-ai"><h3>AI 비용 · 청구·관측 사용량·소매 추정 분리</h3><p class="muted">' + escape(ai.cost_semantics || '청구행과 관측 토큰, 사용자 선택 소매가격 추정을 독립 기준으로 표시합니다.') + ' 계정·배포·모델·배포유형을 보존하고 캐시·요청의 미수집 값을 0으로 바꾸지 않습니다.</p><div data-calculated><h4>현재 조회 원장의 AI 계정 청구행</h4>' + (billed.length ? '<p class="muted">관측 계정 ARM ID와 일치하는 선택 원장 행입니다. 계정·미터 단위 청구이며 배포별 청구비용이나 모든 AI 비용의 총계로 간주하지 않습니다.</p><div class="cf-table-scroll cf-bill-ledger"><table><thead><tr><th>계정·미터</th><th>원문 기준·기간</th><th class="cf-right">청구행 비용</th></tr></thead><tbody>' + billed.map(row => '<tr><td>' + escape(row.resource_name || row.resource || row.account || '계정 미확인') + '<small>' + escape(row.meter || row.service || '') + '</small></td><td>' + escape(basisText(row.basis || data.context.basis)) + '<small>' + escape(row.date || row.month || periodText(data.context)) + '</small></td><td class="cf-right">' + metric({ value: row.amount, node: row.node || row.id, unit: row.currency, source: row.source, status: finite(row.amount) ? 'known' : 'missing' }) + '</td></tr>').join('') + '</tbody></table></div>' : '<p class="cf-na">같은 조회에서 AI 계정 청구행이 연결되지 않았습니다. AI 일별·자원 원장을 선택해 원문 청구를 확인하십시오. 관측 토큰으로 청구비용을 대신 산정하지 않습니다.</p>') + '<h4 class="cf-subheading">배포별 관측 사용량 · 선택기간</h4>' + (tokens.length ? '<div class="cf-table-scroll"><table><thead><tr><th>계정 · 배포·모델·유형</th><th>관측 일수</th><th class="cf-right">입력 토큰</th><th class="cf-right">출력 토큰</th><th class="cf-right">캐시 토큰</th><th class="cf-right">요청</th><th>배포별 청구비용</th></tr></thead><tbody>' + tokens.map(token => '<tr><td>' + escape(token.account_name || token.account || '계정 미확인') + '<small>' + escape([token.deployment || '배포 미확인', token.model || '모델 미수집', token.deployment_type || token.sku || '배포유형 미수집'].join(' · ')) + '</small></td><td title="' + escape('출처: 관측 메타데이터 · 기간 ' + periodText(data.context)) + '">' + escape(token.observed_days == null ? '미수집' : token.observed_days) + '</td><td class="cf-right">' + metric(token.input_tokens, measuredContext) + '</td><td class="cf-right">' + metric(token.output_tokens, measuredContext) + '</td><td class="cf-right">' + metric(token.cached_tokens, measuredContext) + '</td><td class="cf-right">' + metric(token.requests, measuredContext) + '</td><td>' + metric(token.billing_cost || { value: null, status: 'not_collected' }, { ...data.context, basis: '배포별 청구 차원' }) + '<small>' + escape(token.billing_cost && token.billing_cost.reason || '배포별 청구 차원 미수집') + '</small></td></tr>').join('') + '</tbody></table></div>' : '<p class="cf-na">' + escape(statusText(ai.status)) + ' · ' + escape(ai.reason || 'Azure Monitor의 계정·배포·일자별 입력/출력/캐시 토큰·요청 지표 수집이 필요합니다.') + '</p>') + '<p class="muted">처리량 과금·PTU 가동률: ' + escape(statusText(ai.ptu && ai.ptu.status)) + ' · ' + escape(ai.ptu && ai.ptu.reason || 'PTU 배포·사용량 지표 수집 여부를 확인하십시오.') + '</p></div><h4 class="cf-subheading">선택 소매가격 추정 · 청구비용에 합산 안 함</h4><p class="muted">' + escape(ai.pricing_basis || '같은 관측 사용량에 실제 수집 단가 행을 명시적으로 연결한 추정입니다.') + ' 단가의 모델·지역·배포유형·유효일·컨텍스트 조건을 확인하십시오. 캐시 전용 단가를 전체 입력 토큰에 적용하지 않습니다.</p>' + tokens.map((token, index) => {
      const mapping = mappingFor(index, false) || {};
      const evidence = editField('단가 전환 조건·근거', 'ai_mapping', mapping.evidence, 'textarea').replace(' data-edit', ' data-ai-index="' + index + '" data-ai-key="evidence" data-edit');
      const confirmation = checkField('동일 관측 토큰에 적용할 전환 조건 확인', 'ai_mapping', mapping.target_confirmed).replace(' data-edit', ' data-ai-index="' + index + '" data-ai-key="target_confirmed" data-edit');
      return '<details class="cf-ai-prices cf-filters" data-section="ai-prices-' + index + '"><summary>' + escape((token.account_name || token.account) + ' · ' + (token.deployment || '배포 미확인')) + ' · 단가 연결</summary><div class="fg cf-controls">' + priceButton('현재 입력 단가', 'ai', index, 'price_in_ref', mapping.price_in_ref) + priceButton('현재 출력 단가', 'ai', index, 'price_out_ref', mapping.price_out_ref) + priceButton('전환 입력 단가', 'ai', index, 'target_in_ref', mapping.target_in_ref) + priceButton('전환 출력 단가', 'ai', index, 'target_out_ref', mapping.target_out_ref) + evidence + '</div>' + confirmation + (state.result ? '<div class="cf-comparison" data-calculated>' + [['관측량의 현재 소매 추정', token.retail_current || token.retail_estimate], ['동일 토큰 전환 소매 추정', token.retail_target || token.retail_to_be], ['소매가격 차이 · 실제 절감 아님', token.retail_savings]].map(([label, item]) => '<div><span class="muted">' + label + '</span><strong>' + metric(item, retailContext) + '</strong></div>').join('') + '</div>' : '') + '</details>';
    }).join('') + '</section>';
  }

  function newView(data) {
    if (data.track !== 'new') return '';
    const result = data.new || {};
    const options = state.draft.new || {};
    const seed = result.seed || {};
    const context = { basis: '신규 도입 시나리오·소매가격 추정(현재 청구비용과 분리)', period: { start: options.baseline_month || result.baseline_month, end: options.baseline_month || result.baseline_month } };
    const months = array(data.datasets.monthly && data.datasets.monthly.values && data.datasets.monthly.values.month).filter(month => /^\d{4}-\d{2}$/.test(month));
    const numericFields = [['확인 월 요청량', 'monthly_requests', '건/월 · 비워 두면 인터뷰 일 처리량 × 확인 근무일'], ['월 근무일', 'work_days', '일/월 · 기본 근무일 가정 없음'], ['기존 월 투입 시간', 'monthly_work_hours', '시간/월 · 비워 두면 인터뷰 처리시간을 사용'], ['시간당 비용', 'hourly_cost', '원문 통화/시간 · 미입력은 미산정'], ['자동화 적용 비율 · 0~1', 'automation_share', '사용자 확인 가정 · 기본 절감률 없음'], ['요청당 입력 토큰', 'input_tokens', '토큰/요청'], ['요청당 출력 토큰', 'output_tokens', '토큰/요청'], ['초기 구축비', 'build_cost', '일회성 · 미입력 시 회수기간 미산정'], ['별도 부속 서비스·운영비', 'support_cost', '원문 통화/월 · 근거가 없으면 미산정']];
    const metrics = [['기존 유지 월 비용', result.maintain_cost], ['신규 도입 월 비용', result.adoption_cost], ['선택 소매 AI 운영비', result.ai_cost], ['월 순효과', result.net_benefit], ['동일 가정 연간 효과', result.annual_net_benefit], ['초기비 회수기간', result.roi_months]];
    return '<section class="card" id="cf-new"><h3>신규 도입 · 업무비용과 AI 소매 추정</h3><p class="muted">' + escape(result.classification || '신규 도입 가정이며 기존 고객 청구비용과 구분합니다.') + ' 인건비·자동화율·구축비·지원비는 자동 예시값을 넣지 않습니다. 입력 원문과 확인 가정·정확한 단가가 있어야 계산됩니다.</p>' + (seed.quote ? '<blockquote class="cf-quote"><p>인터뷰 업무량 원문 · ' + escape(seed.qid) + '</p><p>“' + escape(seed.quote) + '”</p><p class="muted">월 근무일 확인 전 월 비용으로 승격하지 않습니다.</p></blockquote>' : '<p class="cf-na">인터뷰 업무량 원문이 연결되지 않았습니다. 확인된 월 업무량·시간과 근거를 직접 입력하십시오.</p>') + '<div class="fg cf-controls">' + editSelect('신규 기준 완료월', 'new.baseline_month', [['', 'Actual 완료월 선택'], ...months], options.baseline_month || '') + numericFields.map(([label, name, hint]) => editField(label, 'new.' + name, options[name], 'number', hint, name === 'automation_share' ? ' max="1"' : name === 'work_days' ? ' max="31"' : '')).join('') + editField('부속 서비스·운영비 근거', 'new.support_cost_evidence', options.support_cost_evidence, 'textarea') + editField('신규 도입 가정·단가 적용 근거', 'new.evidence', options.evidence, 'textarea') + priceButton('신규 입력 단가', 'new', 0, 'price_in_ref', options.price_in_ref) + priceButton('신규 출력 단가', 'new', 0, 'price_out_ref', options.price_out_ref) + '</div>' + (state.result ? '<div class="cf-comparison" data-calculated>' + metrics.map(([label, item]) => '<div><span class="muted">' + label + '</span><strong>' + metric(item, context) + '</strong></div>').join('') + '</div><p class="muted">서버 업무량 계산값: 월 처리량 ' + metric(result.monthly_requests, context) + ' · 월 작업시간 ' + metric(result.monthly_work_hours, context) + '</p>' : '<p class="cf-na">확인 가정 저장·재계산 전 신규 비용과 ROI는 표시하지 않습니다.</p>') + '<p class="muted">' + array(result.limitations || result.assumptions).map(escape).join(' · ') + '</p></section>';
  }

  function priceRows() {
    const picker = state.pricePicker;
    if (!picker) return [];
    const direction = picker.key.includes('out') ? 'output' : 'input';
    const currency = state.base.context.currency;
    const query = picker.search.toLocaleLowerCase();
    return array(state.base.prices).map((price, index) => ({ price, index })).filter(({ price }) => {
      const denominator = price.token_denominator || price.denominator;
      const value = price.price == null ? price.amount : price.price;
      const eligible = price.currency === currency && price.direction === direction && price.type === 'Consumption' && price.tier_minimum === 0 && finite(value) && denominator && !/\bCached\b/i.test(price.meter || '');
      return eligible && (!picker.region || price.region === picker.region) && (!query || [price.product, price.meter, price.sku, price.unit, price.region].join(' ').toLocaleLowerCase().includes(query));
    });
  }

  function priceListView() {
    const picker = state.pricePicker;
    const rows = priceRows();
    const pages = Math.max(1, Math.ceil(rows.length / 40));
    picker.page = Math.max(0, Math.min(picker.page, pages - 1));
    const current = rows.slice(picker.page * 40, picker.page * 40 + 40);
    return '<p class="muted">조건에 맞는 원문 단가 ' + rows.length + '행 · ' + (picker.page + 1) + ' / ' + pages + '</p><div class="cf-table-scroll"><table><thead><tr><th>원문 제품·미터·SKU</th><th>지역·유효일</th><th>원문 단위·소매가격</th><th>선택</th></tr></thead><tbody>' + current.map(({ price, index }) => '<tr><td>' + escape(price.product) + '<small>' + escape(price.meter) + '</small><small>' + escape(price.sku) + '</small></td><td>' + escape(price.region || '미수집') + '<small>' + escape(price.effective_date || '유효일 미수집') + '</small></td><td>' + escape(price.unit) + '<small>' + metric({ value: price.price == null ? price.amount : price.price, unit: price.currency, source: price.source, status: 'retail_catalogue' }, { basis: 'Azure 공식 소매 단가(청구금액 아님)', period: { start: price.effective_date, end: '종료 기준 미수집' } }) + '</small></td><td><button class="sec cf-small" data-click="price-choose" data-price-item="' + index + '"' + mutation() + '>이 단가 선택</button></td></tr>').join('') + (current.length ? '' : '<tr><td colspan="4" class="cf-na">조건에 맞는 수집 단가가 없습니다. 유사 모델·임의 단가로 대체하지 않습니다.</td></tr>') + '</tbody></table></div><div class="cf-pagination"><button class="sec cf-small" data-click="price-prev"' + (picker.page <= 0 ? ' disabled' : '') + '>이전 단가</button><button class="sec cf-small" data-click="price-next"' + (picker.page >= pages - 1 ? ' disabled' : '') + '>다음 단가</button></div>';
  }

  function openPrice(button) {
    state.pricePicker = { kind: button.dataset.priceKind, index: Number(button.dataset.priceIndex), key: button.dataset.priceKey, search: '', region: '', page: 0, analysisId: state.base.analysis_id };
    const regions = Array.from(new Set(priceRows().map(({ price }) => price.region).filter(Boolean))).sort();
    showDialog('<p class="muted">실제 수집된 원문 제품·미터·SKU·단위·지역만 선택합니다. 단가 행번호를 모델 이름으로 표시하지 않습니다. 지역·배포유형·모델·유효일·가격 적용 조건을 확인하십시오.</p><div class="cf-price-filters"><label><span class="muted">단가 검색</span><input type="search" aria-label="단가 검색" data-local="price-search" placeholder="원문 제품·미터·SKU·단위"></label><label><span class="muted">단가 지역</span><select aria-label="단가 지역" data-local="price-region"><option value="">전체 수집 지역</option>' + regions.map(region => '<option value="' + escape(region) + '">' + escape(region) + '</option>').join('') + '</select></label></div><div id="cf-price-list">' + priceListView() + '</div><button class="sec" data-click="price-clear"' + mutation() + '>단가 연결 해제</button>', '실제 소매 단가 선택');
  }

  function choosePrice(price) {
    const picker = state.pricePicker;
    if (!picker || !canEdit() || picker.analysisId !== state.base.analysis_id) return;
    if (price && !priceRows().some(item => item.price === price)) return;
    markDirty();
    if (picker.kind === 'ai') {
      const mapping = mappingFor(picker.index, true);
      if (!mapping) return;
      mapping[picker.key] = price ? price.ref : '';
      if (picker.key.startsWith('target')) mapping.target_confirmed = false;
    } else if (picker.kind === 'new') setPath('new.' + picker.key, price ? price.ref : '');
    state.pricePicker = null;
    render();
  }

  function allocationView(data) {
    const allocation = data.allocation || {};
    const keys = array(cfg().tag_keys).slice();
    if (state.draft.allocation_key && !keys.includes(state.draft.allocation_key)) keys.push(state.draft.allocation_key);
    return '<section class="card" id="cf-allocation"><h3>현재 태그 기준 배분</h3><p class="muted">수집시점의 현재 태그이며 역사적 청구 태그·공유비용 배분정책과 구분합니다. 청구 태그 원장에서는 하나의 키만 선택하므로 다른 키의 비용을 합산하지 않습니다.</p>' + selectField('현재 태그 배분키', 'allocation_key', [['', '배분키 선택'], ...keys], state.draft.allocation_key || '', data.context.dataset !== 'resource') + '<div data-calculated>' + (allocation.total ? '<div class="cf-comparison">' + [['태그값 확인 비용', allocation.allocated], ['미배분·확인 불가', allocation.unallocated], ['태그 확인 금액 비율', allocation.coverage]].map(([label, item]) => '<div><span class="muted">' + label + '</span><strong>' + metric(item) + '</strong></div>').join('') + '</div>' : '') + (array(allocation.groups).length ? '<div class="cf-table-scroll"><table><thead><tr><th>현재 태그값</th><th class="cf-right">비용</th><th>행 수</th></tr></thead><tbody>' + allocation.groups.map(group => '<tr><td>' + escape(group.label) + '</td><td class="cf-right">' + metric(group) + '</td><td>' + escape(group.count) + '</td></tr>').join('') + '</tbody></table></div>' : '') + '<p class="muted">' + escape(allocation.reason || statusText(allocation.status)) + '</p></div></section>';
  }

  function observationsView(data) {
    const budgets = data.budgets || {};
    const records = array(budgets.rows || budgets.records);
    const observation = (label, payload, condition) => '<div class="cf-observation"><h4>' + label + ' · ' + escape(statusText(payload && payload.status)) + '</h4><p class="muted">' + escape(payload && payload.reason || condition) + '</p>' + (array(payload && (payload.rows || payload.records || payload.points)).length ? '<pre title="' + escape('출처: ' + (payload.source || '서버 관측 원문')) + '">' + escape(JSON.stringify(payload.rows || payload.records || payload.points, null, 2)) + '</pre>' : '') + '</div>';
    return '<section class="card cf-calculated" data-calculated id="cf-observations"><h3>원문 예산·예측·이상 지출</h3><p class="muted">예산 범위·기간·통화가 현재 조회와 동일하다는 증거 없이 총비용과 비교하지 않습니다. 예산 알림은 자동 사용 한도나 비용 차단이 아닙니다.</p>' + (records.length ? '<div class="cf-table-scroll"><table><thead><tr><th>예산 · 원문 범위</th><th class="cf-right">설정 금액</th><th class="cf-right">항목 현재 사용</th><th class="cf-right">자체 범위 사용비율</th><th>기간·통화</th></tr></thead><tbody>' + records.map(budget => {
      const context = { basis: '예산 원문 자체 범위', period: { start: budget.period && (budget.period.startDate || budget.period.start), end: budget.period && (budget.period.endDate || budget.period.end) } };
      return '<tr><td>' + escape(budget.name) + '<small>' + escape(budget.source || '') + '</small><small>' + escape(budget.scope ? JSON.stringify(budget.scope) : '범위 세부 미수집') + '</small></td><td class="cf-right">' + metric(budget.amount, context) + '</td><td class="cf-right">' + metric(budget.current_spend, context) + '</td><td class="cf-right">' + metric(budget.ratio, context) + '</td><td>' + escape(budget.period ? JSON.stringify(budget.period) : '기간 미수집') + '<small>' + escape(budget.currency || '통화 미수집') + ' · ' + escape(budget.raw && budget.raw.timeGrain || '주기 미수집') + '</small></td></tr>';
    }).join('') + '</tbody></table></div>' : '<p class="cf-na">예산 ' + escape(statusText(budgets.status)) + ' · ' + escape(budgets.reason || '동일 범위의 예산 결과와 조회 권한 확인이 필요합니다.') + '</p>') + observation('공식 비용 예측 · Forecast', data.forecast, '같은 범위·통화·기간의 Cost Management Forecast 수집이 필요합니다.') + observation('공식 이상 지출', data.anomalies, '공식 이상 결과 또는 전체 비용 일별 이력이 필요합니다. 단순 증감을 이상 판정으로 표시하지 않습니다.') + '</section>';
  }

  function candidatesView(data) {
    const candidates = array(data.candidates);
    if (!candidates.length) return '<section class="card"><h3>개선 후보 · Advisor</h3><p class="cf-na">비용 권고가 없거나 수집되지 않았습니다. Advisor 원문 비용 권고를 수집해야 후보를 검토할 수 있습니다.</p></section>';
    return '<section class="card" id="cf-candidates"><h3>개선 후보 · 우선 검토·실행 기록</h3><p class="muted">공급자의 원문 예상 금액입니다. 범위·약정·기간·통화·중복 여부 확인 전 조치 총효과와 TO-BE에 자동 합산하지 않습니다.</p><div class="cf-table-scroll"><table><thead><tr><th>권고 · 대상 범위</th><th>영향·확인 상태</th><th class="cf-right">공급자 연간 예상</th><th>검토 진행 기록</th><th>제약·근거</th></tr></thead><tbody>' + candidates.map((candidate, index) => '<tr><td>' + escape(candidate.label) + '<small>' + escape(candidate.resource || '범위 미확인') + '</small></td><td>' + escape(({ High: '높음', Medium: '중간', Low: '낮음' })[candidate.impact] || candidate.impact || '미확인') + '<small>' + escape(statusText(candidate.status)) + '</small></td><td class="cf-right">' + metric({ value: candidate.annual_estimate, unit: (candidate.currency || '통화 미수집') + '/년', source: candidate.source, status: 'provider_estimate' }, { basis: 'Advisor 공급자 예상(청구 실적·조치 합계 아님)', period: {} }) + '</td><td><select aria-label="개선 후보 ' + (index + 1) + ' 진행 상태" data-edit data-candidate="' + index + '" data-path="candidate_states"' + mutation() + '>' + phases.map(([value, label]) => '<option value="' + value + '"' + selected(value, state.draft.candidate_states && state.draft.candidate_states[candidate.id] || candidate.phase || 'candidate') + '>' + label + '</option>').join('') + '</select></td><td>' + escape(candidate.constraint || '') + '<small>' + escape(candidate.source || '') + '</small><small>' + escape(candidate.confidence || '') + '</small></td></tr>').join('') + '</tbody></table></div></section>';
  }

  function provenanceView(data) {
    const inputs = data.inputs || {};
    return '<details class="card" data-section="provenance"><summary>수집 상태·입력 해시·계산 근거</summary><div class="cf-table-scroll"><table><thead><tr><th>원장</th><th>상태·수집 건수</th><th>수집 API·한계·필요 조건</th></tr></thead><tbody>' + Object.values(data.datasets).map(item => '<tr><td>' + escape(item.label) + '<small>' + escape(item.grain) + '</small></td><td>' + escape(statusText(item.status)) + ' · ' + escape(item.record_count) + '건</td><td>' + escape(item.api || '수집 경로 미확인') + '<small>' + escape(item.reason || '') + '</small></td></tr>').join('') + '</tbody></table></div><p class="muted">수집 실패·권한 부족·부분 수집을 빈 정상 조회나 0원으로 대체하지 않습니다. 비용 조회 권한과 해당 원장의 수집 결과를 확인하십시오.</p><dl class="cf-evidence">' + [['분석 ID', data.analysis_id], ['계산 버전', data.core_version], ['계산 소스 SHA256', data.core_source_sha256], ['비용 근거 SHA256', inputs.evidence_sha256], ['인벤토리 SHA256', inputs.topology_sha256], ['인터뷰 입력 SHA256', inputs.answers_sha256]].map(([label, value]) => '<dt>' + label + '</dt><dd>' + escape(value || '미확인') + '</dd>').join('') + '</dl>' + (data.input_files ? '<pre>' + escape(JSON.stringify(data.input_files, null, 2)) + '</pre>' : '') + '<p class="muted">원장 검사: ' + array(data.checks).map(check => escape(check.code) + ' · ' + (check.passed === true ? '통과' : '확인 필요')).join(' / ') + '</p><details data-section="calculation"><summary>동일 계산 참조와 수식 원장</summary><div class="cf-table-scroll"><table><thead><tr><th>계산 참조</th><th>원장 값</th><th>식·출처</th></tr></thead><tbody>' + array(data.calculation).map(node => '<tr><td>' + escape(node.id) + '<small>' + escape(node.label) + '</small></td><td>' + metric({ ...node, node: node.id }) + '</td><td>' + escape(node.expression ? node.expression.op + '(' + array(node.expression.args).join(', ') + ')' : '입력 원문') + '<small>' + escape(node.source) + '</small></td></tr>').join('') + '</tbody></table></div></details></details>';
  }

  function setPath(path, value) {
    const keys = path.split('.');
    let target = state.draft;
    keys.slice(0, -1).forEach((key, index) => {
      if (target[key] == null) target[key] = /^\d+$/.test(keys[index + 1]) ? [] : {};
      target = target[key];
    });
    const key = keys[keys.length - 1];
    if (path.startsWith('filters.') && (value === '' || value == null)) delete target[key];
    else target[key] = value;
  }

  function markDirty() {
    abortWork();
    state.result = null;
    state.job = null;
    state.selectedHistory = null;
    state.building = false;
    state.downloadBusy = false;
    state.historyLoading = false;
    state.requiredJob = null;
    state.dirty = true;
    state.error = '';
    state.notice = '조건·가정이 변경되어 이전 수치와 다운로드가 무효화되었습니다. 저장·재계산 후 새 버전을 생성하십시오.';
    if (root) {
      root.querySelectorAll('[data-calculated]').forEach(element => { element.hidden = true; });
      const dialog = root.querySelector('#cf-dialog');
      if (dialog && dialog.open) dialog.close();
      const unsaved = root.querySelector('#cf-unsaved');
      if (unsaved) unsaved.hidden = false;
      const save = root.querySelector('[data-click="save"]');
      if (save) save.disabled = !canEdit();
      updateFeedback();
      refreshOutput();
    }
  }

  function updateFeedback() {
    const feedback = root && root.querySelector('#cf-feedback');
    if (feedback) feedback.innerHTML = (state.error ? '<div class="cf-error banner" role="alert">' + escape(state.error) + ' <button class="sec cf-small" data-click="reload">최신 결과 다시 불러오기</button></div>' : '') + (state.notice ? '<p class="muted" role="status">' + escape(state.notice) + '</p>' : '');
  }

  function readEdit(control) {
    return control.multiple ? Array.from(control.selectedOptions).map(option => option.value) : control.type === 'checkbox' ? control.checked : control.type === 'number' ? control.value === '' ? null : Number(control.value) : control.value;
  }

  function editControl(control) {
    if (control.disabled || !canEdit()) return;
    const path = control.dataset.path;
    const value = readEdit(control);
    markDirty();
    if (control.dataset.aiIndex != null) {
      const mapping = mappingFor(Number(control.dataset.aiIndex), true);
      if (mapping) mapping[control.dataset.aiKey] = value;
    } else if (control.dataset.candidate != null) {
      const candidate = array(state.base.candidates)[Number(control.dataset.candidate)];
      if (candidate) { if (!state.draft.candidate_states) state.draft.candidate_states = {}; state.draft.candidate_states[candidate.id] = value; }
    } else {
      setPath(path, value);
      if (['projection.year', 'projection.horizon_months'].includes(path) && value == null) delete state.draft.projection[path.split('.')[1]];
      if (path.startsWith('fx.')) state.draft.fx.source_currency = state.base.context.currency;
      const match = /^actions\.(\d+)\.(.+)$/.exec(path);
      if (match) {
        const action = state.draft.actions[Number(match[1])];
        if (match[2] === 'row_ids') {
          action.scope_confirmed = false;
          action.evidence_snapshot = '';
          const confirm = root.querySelector('[data-path="actions.' + match[1] + '.scope_confirmed"]');
          if (confirm) confirm.checked = false;
        }
        if (match[2] === 'scope_confirmed') action.evidence_snapshot = value ? state.base.inputs && state.base.inputs.evidence_sha256 || '' : '';
      }
    }
    updateFeedback();
  }

  async function saveOptions() {
    if (!canEdit()) return;
    const base = state.base;
    const invalid = root && root.querySelector('[data-mutation]:invalid:not(:disabled)');
    if (invalid) { state.error = '입력 범위·형식을 확인하십시오: ' + (invalid.getAttribute('aria-label') || invalid.dataset.path || '가정 입력'); updateFeedback(); invalid.focus(); return; }
    const newOptions = state.draft.new || {};
    if (base.track === 'new' && ['hourly_cost', 'automation_share', 'build_cost', 'support_cost', 'price_in_ref', 'price_out_ref'].some(key => newOptions[key] != null && newOptions[key] !== '') && !String(newOptions.evidence || '').trim()) {
      state.error = '신규 도입 비용 가정에는 업무량·인건비·자동화·단가 적용 조건의 근거를 입력해야 합니다. 근거 확인 전 계산·저장하지 않습니다.';
      updateFeedback();
      return;
    }
    if (!Number.isInteger(base.revision)) { state.error = '저장 revision이 응답에 없습니다. 최신 결과를 다시 불러오십시오.'; render(); return; }
    const seq = abortWork();
    state.result = null;
    state.job = null;
    state.selectedHistory = null;
    state.saving = true;
    state.dirty = true;
    state.error = '';
    state.notice = '조건 저장·서버 원장 재계산 중…';
    render();
    try {
      const data = await jsonRequest(basePath() + '/options', { method: 'PUT', body: JSON.stringify({ analysis_id: base.analysis_id, revision: base.revision, options: clone(state.draft) }) }, seq);
      if (seq !== state.seq) return;
      acceptResult(data, false);
      state.notice = '변경 사항을 저장하고 동일 계산 원장으로 재계산했습니다. 보고서는 새로 생성해야 합니다.';
      render();
    } catch (error) {
      if (seq === state.seq && error.name !== 'AbortError') {
        state.saving = false;
        state.error = error.message || '저장·재계산에 실패했습니다.';
        if (error.status === 403) state.base.can_edit = false;
        render();
      }
    }
  }

  function onChange(event) {
    if (event.target.dataset.local === 'price-region' && state.pricePicker) { state.pricePicker.region = event.target.value; state.pricePicker.page = 0; root.querySelector('#cf-price-list').innerHTML = priceListView(); return; }
    if (event.target.dataset.local === 'history') {
      if (state.historyLoading || event.target.disabled) return;
      state.selectedHistory = state.history.find(job => jobId(job) === event.target.value) || null;
      state.error = '';
      state.notice = state.selectedHistory ? '이전 결과를 명시적으로 선택했습니다. 현재 계산과 별도의 분석 버전입니다.' : '현재 분석 결과로 돌아왔습니다.';
      updateFeedback();
      refreshOutput();
      return;
    }
    const control = event.target.closest('[data-path]');
    if (!control || control.disabled || !canEdit()) return;
    if (control.hasAttribute('data-edit')) { editControl(control); return; }
    const path = control.dataset.path;
    const value = control.type === 'checkbox' ? control.checked : control.type === 'number' ? control.value === '' ? null : Number(control.value) : control.value;
    markDirty();
    setPath(path, value);
    if (path === 'dataset') {
      const config = cfg();
      state.draft.filters = {};
      state.draft.basis = array(config.basis).includes(state.draft.basis) ? state.draft.basis : array(config.basis)[0];
      if (!array(config.dimensions).includes(state.draft.group_by) || state.draft.group_by === 'tag') delete state.draft.group_by;
      delete state.draft.start;
      delete state.draft.end;
      delete state.draft.tag_key;
    }
    if (path === 'filters.month') { delete state.draft.start; delete state.draft.end; }
    saveOptions();
  }

  function showDialog(html, title) {
    const dialog = root.querySelector('#cf-dialog');
    if (!dialog) return;
    const heading = title || '원장 근거';
    dialog.setAttribute('aria-label', heading);
    dialog.innerHTML = '<div class="cf-dialog-head"><h3>' + escape(heading) + '</h3><button class="sec cf-small" data-click="dialog-close" aria-label="닫기">닫기</button></div>' + html;
    dialog.showModal();
  }

  function showRow(index) {
    const data = state.result;
    const row = data && data.rows[index];
    if (!row) return;
    const fields = [['원장 행 ID', row.id], ['원본 레코드', row.source], ['자원 ARM ID', row.resource], ['통화·기준', row.currency + ' · ' + basisText(row.basis)], ['원문 기간', row.period], ['조인 속성 출처', row.dimension_sources], ['현재 태그', row.tags], ['입력 파일 SHA256', data.inputs && data.inputs.evidence_sha256]];
    showDialog('<dl class="cf-evidence">' + fields.map(([label, value]) => '<dt>' + escape(label) + '</dt><dd>' + escape(typeof value === 'object' ? JSON.stringify(value, null, 2) : value || '미수집') + '</dd>').join('') + '</dl><h4>원문 · 변경 없이 보존</h4><pre>' + escape(JSON.stringify(row.raw, null, 2)) + '</pre>');
  }

  function onClick(event) {
    const button = event.target.closest('[data-click]');
    if (!button || button.disabled) return;
    const action = button.dataset.click;
    if (action === 'reload') { mount(state.aid); return; }
    if (action === 'dialog-close') { root.querySelector('#cf-dialog').close(); return; }
    if (action === 'row-open') { showRow(Number(button.dataset.index)); return; }
    if (action === 'download') { downloadFile(button.dataset.kind); return; }
    if (action === 'history-load') { loadHistory(); return; }
    if (action === 'job-resume' && state.result && state.job && state.job.analysis_id === state.result.analysis_id) { state.building = true; state.error = ''; pollJob(state.seq, jobId(state.job), state.result.analysis_id); refreshOutput(); return; }
    if (action === 'row-prev' || action === 'row-next') { state.rowPage += action === 'row-next' ? 1 : -1; if (state.result) root.querySelector('#cf-rows').innerHTML = rowsView(state.result); return; }
    if (!canEdit()) return;
    if (action === 'save') { saveOptions(); return; }
    if (action === 'build') { buildReports(); return; }
    if (action === 'price-open') { openPrice(button); return; }
    if (action === 'price-choose') { choosePrice(array(state.base.prices)[Number(button.dataset.priceItem)]); return; }
    if (action === 'price-clear') { choosePrice(null); return; }
    if (action === 'price-next' || action === 'price-prev') { if (state.pricePicker) { state.pricePicker.page += action === 'price-next' ? 1 : -1; root.querySelector('#cf-price-list').innerHTML = priceListView(); } return; }
    if (action === 'action-add') {
      markDirty();
      if (!state.draft.actions) state.draft.actions = [];
      if (state.draft.actions.length >= 50) return;
      state.draft.actions.push({ id: 'action-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + '-' + state.draft.actions.length), name: '', kind: 'confirmed_amount', amount: null, currency: state.base.context.currency, basis: state.base.context.basis, row_ids: [], enabled: false, scope_confirmed: false, evidence: '', evidence_snapshot: '', phase: 'candidate', owner: '', constraint: '' });
      render();
      const last = root.querySelectorAll('[data-path$=".name"]');
      if (last.length) last[last.length - 1].focus();
      return;
    }
    if (['action-remove', 'action-up', 'action-down'].includes(action)) {
      const index = Number(button.dataset.index);
      const actions = array(state.draft.actions);
      markDirty();
      if (action === 'action-remove') actions.splice(index, 1);
      else { const destination = index + (action === 'action-up' ? -1 : 1); if (destination >= 0 && destination < actions.length) [actions[index], actions[destination]] = [actions[destination], actions[index]]; }
      render();
      return;
    }
    if (action === 'filters-reset') { markDirty(); state.draft.filters = {}; delete state.draft.start; delete state.draft.end; saveOptions(); return; }
    if (action === 'drill' && state.result) {
      const data = state.result;
      const group = data.groups[Number(button.dataset.index)];
      if (!group) return;
      const dimension = data.context.group_by;
      markDirty();
      setPath('filters.' + dimension, group.key == null ? group.label : group.key);
      const next = array(cfg().dimensions).filter(dim => !['tag', dimension].includes(dim) && !(state.draft.filters || {})[dim]);
      const preferred = ['resource_group', 'service', 'resource', 'meter', 'region', 'sku'];
      state.draft.group_by = preferred.find(dim => next.includes(dim)) || dimension;
      saveOptions();
    }
  }

  const fileTypes = { xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' };
  const jobId = job => job && String(job.job_id || job.id || '');
  const jobStatuses = { none: '미생성', queued: '생성 대기', pending: '생성 대기', running: '생성 중', done: '완료', failed: '생성 실패', interrupted: '중단됨', cancelled: '취소됨' };

  function timeText(value) {
    if (value == null || value === '') return '완료 시각 미확인';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '시각 형식 미확인' : date.toLocaleString('ko-KR');
  }

  function bytesText(value) {
    if (!finite(value)) return '크기 미확인';
    return new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 0 }).format(value) + ' bytes';
  }

  function downloadJob() {
    const job = state.selectedHistory || state.job;
    if (!job || job.status !== 'done' || !job.finished || !jobId(job) || !array(job.files).length) return null;
    if (state.selectedHistory) return state.history.some(item => jobId(item) === jobId(job) && item.analysis_id === job.analysis_id) ? job : null;
    if (!state.result || state.dirty || state.saving || job.analysis_id !== state.result.analysis_id) return null;
    const required = state.requiredJob;
    if (required && (jobId(job) !== required.id || !required.getConfirmed || required.previousId === required.id && job.finished === required.previousFinished)) return null;
    return job;
  }

  function outputView() {
    const job = state.selectedHistory || state.job;
    const ready = downloadJob();
    const waiting = job && job.status === 'done' && !ready;
    const buttons = Object.keys(fileTypes).map(kind => {
      const file = ready && array(ready.files).find(item => item.kind === kind);
      return '<button class="sec" data-click="download" data-kind="' + kind + '"' + (!file || state.downloadBusy ? ' disabled' : '') + '>' + (kind === 'xlsx' ? 'Excel 다운로드' : 'PPT 다운로드') + (file ? ' · ' + escape(bytesText(file.bytes)) : '') + '</button>';
    }).join('');
    const runs = state.history;
    return '<h3>분석 버전 생성·검증 다운로드</h3><p class="muted">현재 서버 계산을 고정하여 두 문서를 생성합니다. 조건·가정 변경 시 이전 응답과 다운로드를 즉시 무효화하며, 이전 결과는 이력에서 명시적으로 선택한 경우에만 받습니다.</p><div class="row"><button data-click="build"' + mutation(state.dirty || !state.result || state.building) + '>Excel·PPT 새 버전 생성</button><span class="muted" role="status">' + escape(waiting ? '새 작업 완료 시각·파일 확인 대기' : jobStatuses[job && job.status || 'none'] || job && job.status || '미생성') + '</span>' + (job && ['running', 'queued', 'pending'].includes(job.status) && !state.building ? '<button class="sec cf-small" data-click="job-resume">상태 다시 확인</button>' : '') + '</div>' + (job ? '<p class="muted cf-job-meta">' + (state.selectedHistory ? '명시적으로 선택한 이전 결과' : '현재 분석 결과') + ' · 분석 ID ' + escape(job.analysis_id) + ' · 작업 ' + escape(jobId(job)) + ' · ' + escape(timeText(job.finished)) + '</p>' + (job.context ? '<p class="muted">' + escape(periodText(job.context)) + ' · ' + escape(basisText(job.context.basis)) + ' · ' + escape(job.context.currency || '통화 미확인') + '</p>' : '') : '<p class="cf-na">현재 조건으로 생성한 완료 문서가 없습니다.</p>') + (job && array(job.steps).length ? '<ol class="cf-job-steps">' + job.steps.map(step => '<li><span>' + escape(step.label || step.name || '') + '</span><span class="pill">' + escape(({ ok: '완료', done: '완료', running: '진행 중', failed: '실패', pending: '대기', skipped: '건너뜀' })[step.status] || step.status) + '</span></li>').join('') + '</ol>' : '') + (job && job.error ? '<p class="cf-error">' + escape(typeof job.error === 'string' ? job.error : JSON.stringify(job.error)) + '</p>' : '') + '<div class="row cf-downloads">' + buttons + '</div>' + (state.error ? '<p class="cf-error" role="status">' + escape(state.error) + '</p>' : '') + '<p class="muted">파일 크기·Office MIME·ZIP 서명·SHA256을 검증한 뒤 Blob으로 저장합니다. 네트워크 실패·빈 파일·손상·해시 불일치는 오류로 표시하며 내려받지 않습니다.</p><details class="cf-filters" data-section="history"><summary>이전 분석 버전 · 불변 스냅샷</summary><div class="cf-history"><button class="sec" data-click="history-load"' + (state.historyLoading ? ' disabled' : '') + '>이전 버전 조회</button><label><span class="muted">이전 버전</span><select aria-label="이전 버전" data-local="history"' + (state.historyLoading ? ' disabled' : '') + '><option value="">현재 분석 결과</option>' + runs.map(run => '<option value="' + escape(jobId(run)) + '"' + selected(jobId(run), jobId(state.selectedHistory)) + '>' + escape(timeText(run.finished) + ' · ' + (jobStatuses[run.status] || run.status) + ' · ' + String(run.analysis_id || '').slice(0, 12) + ' · ' + jobId(run)) + '</option>').join('') + '</select></label></div><p class="muted">이전 버전의 수치·가정·기간은 현재 화면의 계산과 동일하다고 간주하지 않습니다. 선택한 분석 ID를 문서 안에서 확인하십시오.</p></details>';
  }

  function refreshOutput() {
    const output = root && root.querySelector('#cf-output');
    if (!output) return;
    const history = output.querySelector('[data-section="history"]');
    const wasOpen = history && history.open;
    output.innerHTML = outputView();
    if (wasOpen) output.querySelector('[data-section="history"]').open = true;
  }

  async function pollJob(seq, id, analysisId) {
    if (seq !== state.seq || !root || !root.isConnected || !state.result || state.result.analysis_id !== analysisId) return;
    try {
      const job = await jsonRequest(basePath() + '/jobs/' + encodeURIComponent(id), { method: 'GET' }, seq);
      if (seq !== state.seq) return;
      if (!job || job.analysis_id !== analysisId || jobId(job) !== id) throw new Error('생성 작업의 분석 ID 또는 작업 ID가 현재 요청과 일치하지 않습니다. 이전 파일은 다운로드하지 않습니다.');
      if (state.requiredJob) state.requiredJob.getConfirmed = true;
      state.job = job;
      if (job.status === 'done' && downloadJob()) {
        state.building = false;
        state.notice = '현재 분석의 새 버전 생성이 완료되었습니다. 검증 다운로드를 사용할 수 있습니다.';
        state.lastDoneJob = clone(job);
      } else if (['failed', 'interrupted', 'cancelled'].includes(job.status)) {
        state.building = false;
        state.error = '문서 생성이 완료되지 않았습니다. ' + (typeof job.error === 'string' ? job.error : jobStatuses[job.status]);
      } else if (job.status === 'done' && job.finished && !array(job.files).length) {
        state.building = false;
        state.error = '생성 작업에 완료 파일이 없습니다. 새 버전을 다시 생성하십시오.';
      } else {
        state.pollTimer = setTimeout(() => pollJob(seq, id, analysisId), 1200);
      }
      updateFeedback();
      refreshOutput();
    } catch (error) {
      if (seq === state.seq && error.name !== 'AbortError') {
        state.building = false;
        state.error = '작업 상태 확인 실패: ' + (error.message || '네트워크 오류');
        if (state.job && state.job.analysis_id !== analysisId || error.message && error.message.includes('분석 ID')) state.job = null;
        updateFeedback();
        refreshOutput();
      }
    }
  }

  async function buildReports() {
    if (!canEdit() || !state.result || state.dirty || state.building) return;
    const analysisId = state.result.analysis_id;
    const previous = state.job || state.lastDoneJob;
    const seq = abortWork();
    state.job = null;
    state.selectedHistory = null;
    state.requiredJob = null;
    state.downloadBusy = false;
    state.building = true;
    state.error = '';
    state.notice = '현재 분석을 고정하여 새 보고서 버전을 요청하는 중…';
    updateFeedback();
    refreshOutput();
    try {
      const job = await jsonRequest(basePath() + '/build', { method: 'POST', body: JSON.stringify({ analysis_id: analysisId }) }, seq);
      if (seq !== state.seq) return;
      if (!job || job.analysis_id !== analysisId || !jobId(job)) throw new Error('보고서 생성 응답의 분석 ID 또는 작업 ID가 유효하지 않습니다.');
      state.requiredJob = { id: jobId(job), analysisId, previousId: jobId(previous), previousFinished: previous && previous.finished, getConfirmed: false };
      state.job = job;
      refreshOutput();
      // POST의 완료 표시는 다운로드 권한으로 쓰지 않고 새 작업의 GET·finished를 기다린다.
      pollJob(seq, jobId(job), analysisId);
    } catch (error) {
      if (seq === state.seq && error.name !== 'AbortError') {
        state.building = false;
        state.error = '보고서 생성 실패: ' + (error.message || '네트워크 오류');
        updateFeedback();
        refreshOutput();
      }
    }
  }

  async function loadHistory() {
    if (state.historyLoading) return;
    const seq = state.seq;
    state.historyLoading = true;
    state.error = '';
    refreshOutput();
    try {
      const data = await jsonRequest(basePath() + '/history', { method: 'GET' }, seq);
      if (seq !== state.seq) return;
      state.history = array(data.runs).filter(run => jobId(run) && typeof run.analysis_id === 'string');
      state.selectedHistory = null;
      state.notice = '이전 분석 목록을 조회했습니다. 다운로드할 버전을 명시적으로 선택하십시오.';
    } catch (error) {
      if (seq === state.seq && error.name !== 'AbortError') state.error = '이전 버전 조회 실패: ' + (error.message || '네트워크 오류');
    } finally {
      if (seq === state.seq) { state.historyLoading = false; updateFeedback(); refreshOutput(); }
    }
  }

  function safeFilename(disposition, kind, analysisId) {
    let name = '';
    const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition || '');
    const plain = /filename="([^"]+)"|filename=([^;]+)/i.exec(disposition || '');
    try { name = encoded ? decodeURIComponent(encoded[1]) : plain ? (plain[1] || plain[2]).trim() : ''; } catch (_) { name = ''; }
    name = name.replace(/[\\/<>:"|?*\u0000-\u001f\u007f]/g, '_').slice(0, 180);
    return name && name.toLowerCase().endsWith('.' + kind) ? name : '비용분석_' + String(analysisId).replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 16) + '.' + kind;
  }

  async function downloadFile(kind) {
    const job = downloadJob();
    if (!job || state.downloadBusy || !fileTypes[kind]) return;
    const file = array(job.files).find(item => item.kind === kind);
    const seq = state.seq;
    const id = jobId(job);
    const controller = new AbortController();
    state.controllers.add(controller);
    state.downloadBusy = true;
    state.error = '';
    state.notice = '파일을 내려받아 크기·형식·ZIP·SHA256을 확인하는 중…';
    updateFeedback();
    refreshOutput();
    try {
      if (!file || !Number.isSafeInteger(file.bytes) || file.bytes <= 0 || !/^[a-f0-9]{64}$/i.test(file.sha256 || '')) throw new Error('완료 파일의 크기 또는 SHA256 메타데이터가 유효하지 않습니다.');
      const url = new URL(file.url, location.origin);
      const expected = basePath() + '/jobs/' + encodeURIComponent(id) + '/files/' + kind;
      if (url.origin !== location.origin || url.pathname !== expected || url.search || url.hash) throw new Error('파일 URL이 같은 출처의 해당 작업 다운로드 경로가 아닙니다.');
      let response;
      try { response = await fetch(url.href, { credentials: 'same-origin', redirect: 'error', signal: controller.signal }); } catch (error) { if (error.name === 'AbortError') throw error; throw new Error('다운로드 네트워크 요청에 실패했습니다. 연결·로그인 세션을 확인하십시오.'); }
      if (!response.ok) throw new Error('다운로드 네트워크 오류 (HTTP ' + response.status + '). 연결·접근 권한을 확인하십시오.');
      const mime = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
      if (mime !== fileTypes[kind]) throw new Error('다운로드 파일의 MIME 형식이 ' + kind.toUpperCase() + ' 문서 형식과 일치하지 않습니다.');
      const buffer = await response.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      if (!bytes.length) throw new Error('다운로드 파일이 비어 있습니다. 새 버전을 다시 생성하십시오.');
      if (bytes.length !== file.bytes) throw new Error('다운로드 파일 크기가 완료 메타데이터와 다릅니다. 손상·불완전 전송을 확인하십시오.');
      if (bytes.length < 22 || bytes[0] !== 0x50 || bytes[1] !== 0x4b || bytes[2] !== 0x03 || bytes[3] !== 0x04) throw new Error('다운로드 파일의 ZIP 서명이 올바르지 않습니다. 손상된 문서는 저장하지 않습니다.');
      const length = response.headers.get('Content-Length');
      if (length && Number(length) !== file.bytes) throw new Error('전송 크기 헤더가 파일 크기와 일치하지 않습니다.');
      if (!crypto.subtle) throw new Error('이 브라우저에서는 SHA256 검증을 사용할 수 없습니다. HTTPS 환경에서 다시 시도하십시오.');
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', buffer))).map(byte => byte.toString(16).padStart(2, '0')).join('');
      const serverHash = response.headers.get('X-Content-SHA256');
      if (digest !== file.sha256.toLowerCase() || serverHash && serverHash.toLowerCase() !== digest) throw new Error('파일 SHA256이 완료 결과 또는 전송 검증 헤더와 일치하지 않습니다. 손상된 문서는 저장하지 않습니다.');
      if (seq !== state.seq || jobId(downloadJob()) !== id || controller.signal.aborted) return;
      const blob = new Blob([buffer], { type: fileTypes[kind] });
      const blobUrl = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = blobUrl;
      anchor.download = safeFilename(response.headers.get('Content-Disposition'), kind, job.analysis_id);
      anchor.hidden = true;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
      state.notice = kind.toUpperCase() + ' 파일 검증 완료 · ' + bytesText(bytes.length) + ' · SHA256 ' + digest;
    } catch (error) {
      if (seq === state.seq && error.name !== 'AbortError') state.error = error.message || '다운로드 검증에 실패했습니다.';
    } finally {
      state.controllers.delete(controller);
      if (seq === state.seq) { state.downloadBusy = false; updateFeedback(); refreshOutput(); }
    }
  }

  function panelsView(data) {
    const current = state.result;
    return '<p id="cf-unsaved" class="banner"' + (state.dirty ? '' : ' hidden') + '>변경된 조건의 서버 계산이 필요합니다. 이전 수치·다운로드는 표시하지 않습니다.</p>' + (current ? summaryView(current) + ledgerView(current) + allocationView(current) + observationsView(current) : '<section class="card cf-pending" role="status">변경한 조건의 결과가 아직 확정되지 않았습니다. 이전 수치·다운로드는 표시하지 않습니다.</section>') + aiView(data) + newView(data) + scenarioView(data) + candidatesView(data) + '<section class="card" id="cf-output">' + outputView() + '</section>' + (current ? provenanceView(current) : '') + '<div class="cf-save-bar"><span class="muted">' + (state.saving ? '저장·재계산 중…' : state.dirty ? '아직 저장하지 않은 변경 사항' : '현재 서버 계산 결과') + '</span><button data-click="save"' + mutation(!state.dirty) + '>저장·재계산</button><button class="sec" data-click="reload">저장한 조건 다시 불러오기</button></div>';
  }

  function render() {
    if (!root || !root.isConnected) return;
    const opened = new Set(Array.from(root.querySelectorAll('details[open][data-section]')).map(element => element.dataset.section));
    const data = state.base;
    root.innerHTML = '<div class="cf-dashboard"><div id="cf-feedback" aria-live="polite">' + (state.error ? '<div class="cf-error banner" role="alert">' + escape(state.error) + ' <button class="sec cf-small" data-click="reload">최신 결과 다시 불러오기</button></div>' : '') + (state.notice ? '<p class="muted" role="status">' + escape(state.notice) + '</p>' : '') + '</div>' + (data ? headerView(data) + panelsView(data) : '<section class="card" role="status">' + (state.error ? '비용 분석을 불러오지 못했습니다.' : '비용 분석을 불러오는 중…') + '</section>') + '<dialog id="cf-dialog" class="cf-dialog" aria-label="원장 근거"></dialog></div>';
    root.querySelectorAll('details[data-section]').forEach(element => { element.open = opened.has(element.dataset.section); });
  }

  async function mount(aid) {
    const seq = abortWork();
    state.aid = aid;
    state.result = null;
    state.base = null;
    state.draft = {};
    state.job = null;
    state.selectedHistory = null;
    state.requiredJob = null;
    state.lastDoneJob = null;
    state.building = false;
    state.downloadBusy = false;
    state.historyLoading = false;
    state.error = '';
    state.notice = '';
    state.loading = true;
    state.dirty = false;
    state.saving = false;
    root = document.getElementById('cost-root');
    if (!root) throw new Error('비용 화면 대상 #cost-root가 없습니다.');
    root.onclick = onClick;
    root.onchange = onChange;
    root.oninput = event => {
      if (event.target.dataset.local === 'price-search' && state.pricePicker) { state.pricePicker.search = event.target.value; state.pricePicker.page = 0; root.querySelector('#cf-price-list').innerHTML = priceListView(); return; }
      if (event.target.matches('[data-edit][data-path]') && event.target.tagName !== 'SELECT') { editControl(event.target); return; }
      if (event.target.dataset.local === 'row-search' && state.result) {
        state.rowSearch = event.target.value;
        state.rowPage = 0;
        const cursor = event.target.selectionStart;
        root.querySelector('#cf-rows').innerHTML = rowsView(state.result);
        const search = root.querySelector('[data-local="row-search"]');
        search.focus();
        if (cursor != null) search.setSelectionRange(cursor, cursor);
      }
    };
    if (!document.getElementById('cost-finops-styles')) {
      const link = document.createElement('link');
      link.id = 'cost-finops-styles';
      link.rel = 'stylesheet';
      link.href = '/static/cost_finops.css';
      document.head.appendChild(link);
    }
    render();
    try {
      const data = await jsonRequest(basePath(), { method: 'GET' }, seq);
      if (seq !== state.seq) return null;
      acceptResult(data, true);
      render();
      if (state.job && ['running', 'queued', 'pending'].includes(state.job.status) && jobId(state.job)) {
        state.requiredJob = { id: jobId(state.job), analysisId: data.analysis_id, getConfirmed: false };
        state.building = true;
        pollJob(seq, jobId(state.job), data.analysis_id);
      }
      return data;
    } catch (error) {
      if (error.name !== 'AbortError' && seq === state.seq) {
        state.error = error.message || '비용 분석을 불러오지 못했습니다.';
        state.loading = false;
        render();
      }
      return null;
    }
  }

  window.CostFinops = { mount, state, unmount: () => { abortWork(); root = null; state.result = null; } };
}());
