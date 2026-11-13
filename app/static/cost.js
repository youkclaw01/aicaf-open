// Cost Optimization 탭 화면. index.html 의 디자인 토큰(CSS 변수)만 쓴다. 그래프는 인라인 SVG(외부 라이브러리·CDN 없음).
// 계산은 서버 cost_calc.compute() 결과만 표시한다(화면에서 다시 계산하지 않음 → 엑셀·PPT 와 같은 숫자).
let COST = null, COST_EDIT = null, COST_PT = null;
const CC = ['var(--blue)', 'var(--purple)', 'var(--green)', 'var(--orange)', 'var(--muted)', 'var(--faint)', 'var(--link)'];
const ce = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const tk = (v, mx) => v.toLocaleString(undefined, mx < 10 ? { minimumFractionDigits: 2, maximumFractionDigits: 2 } : { maximumFractionDigits: 0 });  // 축 눈금: 작은 값은 소수 둘째 자리
function cm(v, cur) { if (v === null || v === undefined) return '—'; const a = Math.abs(v); return (cur || COST.currency) + ' ' + (a >= 100 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })) }
// 근거 툴팁: 값 옆 ⓘ 에 식·출처
function tip(o) { return o ? `<span class="cx-tip" tabindex="0" title="${ce('식: ' + (o.f || '') + '\n출처: ' + (o.src || ''))}">ⓘ</span>` : '' }
function vbox(label, o, fmt) { const v = o && o.v; const t = v === null || v === undefined ? `<span class="cx-na">수집 안 됨</span>` : (fmt ? fmt(v) : cm(v)); return `<div class="kpi"><div class="muted">${ce(label)} ${tip(o)}</div><div class="cx-v">${t}</div></div>` }
function secBadge(s) { if (!s) return ''; const ok = s.status === 'collected' && s.count; return `<span class="pill ${ok ? '' : 'must'}" title="${ce((s.api || '') + (s.reason ? ' · ' + s.reason : ''))}">${ce(s.status_ko)}${ok ? ' · ' + s.count + '건' : (s.reason ? ' · ' + ce(s.reason) : '')}</span>` }
function naCard(title, s) { return `<div class="card"><h3>${ce(title)}</h3><p class="cx-na">${ce(s ? s.status_ko : '수집 안 됨')}${s && s.reason ? ' — ' + ce(s.reason) : ''}</p><p class="muted">수집되지 않은 값은 0으로 채우지 않습니다. 인프라 분석 탭에서 수집 스크립트(aicaf-evidence.json)를 올리면 표시됩니다.</p></div>` }

// ---------- SVG 그래프 ----------
function svgBars(cats, series, opt = {}) {
  const { h = 240, fmt = v => cm(v), stacked = false } = opt;
  const W = 440, H = h, L = 52, B = 30, T = 14, R = 10, n = cats.length, k = series.length;
  const tot = i => series.reduce((s, x) => s + Math.max(0, x.vals[i] || 0), 0);
  const max = Math.max(1, ...cats.map((_, i) => stacked ? tot(i) : Math.max(...series.map(x => x.vals[i] || 0))));
  const y = v => T + (H - T - B) * (1 - v / max), gw = (W - L - R) / n, bw = stacked ? gw * 0.55 : gw * 0.7 / k;
  let g = '';
  for (let t = 0; t <= 4; t++) { const v = max * t / 4; g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--hair)"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" font-size="10" fill="var(--muted)">${tk(v, max)}</text>` }
  cats.forEach((c, i) => {
    let acc = 0;
    series.forEach((s, j) => {
      const v = s.vals[i]; if (v === null || v === undefined) return;
      const x = L + i * gw + (stacked ? (gw - bw) / 2 : gw * 0.15 + j * bw), top = stacked ? y(acc + v) : y(v), bot = stacked ? y(acc) : y(0);
      if (!s.hide) g += `<rect x="${x}" y="${top}" width="${bw - 2}" height="${Math.max(0, bot - top)}" fill="${(s.pt && s.pt(i)) || s.color || CC[j]}" rx="2"><title>${ce(c + ' · ' + s.name + ': ' + fmt(v))}</title></rect>`;
      if (!stacked || j === k - 1) g += `<text x="${x + bw / 2}" y="${(stacked ? y(acc + v) : top) - 3}" text-anchor="middle" font-size="10" fill="var(--ink2)">${ce(fmt(stacked && !opt.own ? acc + v : v).replace(/^\S+ /, ''))}</text>`;
      acc += v;
    });
    const mx = Math.max(3, Math.floor(gw / 11));  // 막대 폭에 맞춰 축 이름을 줄인다(겹침 방지)
    g += `<text x="${L + i * gw + gw / 2}" y="${H - 10}" text-anchor="middle" font-size="11" fill="var(--muted)"><title>${ce(c)}</title>${ce(c.length > mx ? c.slice(0, mx - 1) + '…' : c)}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" class="cx-svg" role="img">${g}</svg>` + legend(series);
}
function legend(series) { return `<div class="cx-leg">${series.filter(s => !s.hide).map((s, j) => `<span><i style="background:${s.color || CC[j]}"></i>${ce(s.name)}</span>`).join('')}</div>` }
function svgDonut(items) {
  const tot = items.reduce((s, x) => s + x.v, 0) || 1; let a = -Math.PI / 2, g = '';
  items.forEach((it, i) => {
    const f = it.v / tot, b = a + f * 2 * Math.PI, R = 80, r = 50, cx = 100, cy = 100, lg = f > 0.5 ? 1 : 0;
    const p = (ang, rr) => [cx + rr * Math.cos(ang), cy + rr * Math.sin(ang)];
    if (f >= 0.9999) { g += `<circle cx="${cx}" cy="${cy}" r="${(R + r) / 2}" fill="none" stroke="${CC[i % CC.length]}" stroke-width="${R - r}"/>` }
    else { const [x1, y1] = p(a, R), [x2, y2] = p(b, R), [x3, y3] = p(b, r), [x4, y4] = p(a, r); g += `<path d="M${x1} ${y1}A${R} ${R} 0 ${lg} 1 ${x2} ${y2}L${x3} ${y3}A${r} ${r} 0 ${lg} 0 ${x4} ${y4}Z" fill="${CC[i % CC.length]}"><title>${ce(it.name + ': ' + cm(it.v) + ' (' + (f * 100).toFixed(1) + '%)')}</title></path>` }
    a = b;
  });
  return `<div class="cx-row"><svg viewBox="0 0 200 200" class="cx-donut">${g}</svg><table class="cx-tbl">${items.map((it, i) => `<tr><td><i class="cx-dot" style="background:${CC[i % CC.length]}"></i>${ce(it.name)}</td><td class="r">${cm(it.v)}</td><td class="r muted">${(it.v / tot * 100).toFixed(1)}%</td></tr>`).join('')}</table></div>`;
}
function svgLine(points, { h = 240, W = 480 } = {}) {
  const H = h, L = 70, B = 26, T = 12, R = 10, xs = points.map(p => p.m), vs = points.map(p => p.v);
  const mn = Math.min(0, ...vs), mx = Math.max(1, ...vs), x = m => L + (W - L - R) * (m - xs[0]) / Math.max(1, xs[xs.length - 1] - xs[0]), y = v => T + (H - T - B) * (1 - (v - mn) / (mx - mn || 1));
  let g = `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="var(--muted)"/>`;
  for (let t = 0; t <= 4; t++) { const v = mn + (mx - mn) * t / 4; g += `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" font-size="10" fill="var(--muted)">${tk(v, Math.max(Math.abs(mn), Math.abs(mx)))}</text><line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--hair)"/>` }
  g += `<polyline fill="none" stroke="var(--blue)" stroke-width="2.5" points="${points.map(p => x(p.m) + ',' + y(p.v)).join(' ')}"/>`;
  points.forEach(p => { g += `<circle cx="${x(p.m)}" cy="${y(p.v)}" r="3" fill="${p.v >= 0 ? 'var(--green)' : 'var(--red)'}"><title>${p.m}개월: ${cm(p.v)}</title></circle>`; if (p.m % 3 === 0) g += `<text x="${x(p.m)}" y="${H - 8}" text-anchor="middle" font-size="10" fill="var(--muted)">${p.m}</text>` });
  const be = points.find(p => p.v >= 0); if (be) g += `<line x1="${x(be.m)}" x2="${x(be.m)}" y1="${T}" y2="${H - B}" stroke="var(--green)" stroke-dasharray="4 3"/><text x="${x(be.m) + 4}" y="${T + 10}" font-size="11" fill="var(--green)">손익분기 ${be.m}개월</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" class="cx-svg">${g}</svg>`;
}

// ---------- 화면 ----------
async function costLoad(aid) { COST_MSG = ''; COST = await api(`/api/aicaf/assessments/${aid}/cost`); COST._aid = aid; COST_EDIT = JSON.parse(JSON.stringify(COST.assumptions)) }
function costView(aid) {
  if (!COST) { costLoad(aid).then(() => paintCost(aid)).catch(e => { const el = $('#cost-root'); if (el) el.innerHTML = `<div class="card err">${ce(e.message)}</div>` }); return `<div id="cost-root"><div class="card">불러오는 중…</div></div>` }
  return `<div id="cost-root">${costBody(aid)}</div>`;
}
function paintCost(aid) { const el = $('#cost-root'); if (el) el.innerHTML = costBody(aid); if (COST && COST.job && COST.job.status === 'running') pollCost(aid) }
function costBody(aid) {
  const R = COST, act = R.actual, S = act.sections, cur = R.currency;
  const head = `<div class="card"><h3>Cost Optimization <span class="pill trk-${R.track}">${R.track === 'existing' ? '기구축 — 토큰 최적화' : '신규구축 — 도입 ROI'}</span></h3>
<p class="muted">수치 출처: 인프라 분석 탭에 올라온 <b>aicaf-evidence.json</b>(Cost Management·Azure Monitor·Azure Retail Prices)${R.collected_at ? ' · 수집 ' + ce(R.collected_at.slice(0, 16).replace('T', ' ')) : ''} · 인터뷰 비용 영역 답변 · 아래 가정값. 각 숫자의 ⓘ 에 식과 출처가 있습니다. 통화 ${ce(cur)}.</p>
${R.has_evidence ? '' : '<div class="banner">aicaf-evidence.json 이 아직 없습니다. 인프라 분석 탭에서 수집 스크립트 결과를 올리면 실제 비용이 채워집니다. 그 전까지는 모든 수집 항목이 "수집 안 됨"으로 표시됩니다.</div>'}
<div class="row" style="margin-top:8px">${['costMonthlyTotals', 'costByService', 'costByResource', 'costByTag', 'costByPricingModel', 'budgets', 'aiCostDaily', 'aiMetricsTokensDaily', 'pricesModels', 'pricesSupporting'].map(k => `<span class="muted" style="font-size:12px">${k}</span>${secBadge(S[k])}`).join(' ')}</div></div>`;
  const monthly = act.monthly.length ? `<div class="card"><h3>월별 비용 추이 ${tip(act.avg_monthly)}</h3>${svgBars(act.monthly.map(m => m.month), [{ name: '실비용', vals: act.monthly.map(m => m.actual) }, { name: 'AI 서비스', vals: (() => { const a = Object.fromEntries(act.ai_monthly.map(x => [x.month, x.cost])); return act.monthly.map(m => a[m.month] ?? null) })() }])}<p class="muted">완료월 평균 ${cm(act.avg_monthly.v)} · MTD 월은 평균에서 제외</p></div>` : naCard('월별 비용 추이', S.costMonthlyTotals);
  const svc = act.by_service.length ? (() => { const top = act.by_service.slice(0, 6), rest = act.by_service.slice(6).reduce((s, x) => s + x.cost, 0); return `<div class="card"><h3>서비스별 비중 <span class="cx-tip" title="출처: costByService (Cost Management Query, 기간 합)">ⓘ</span></h3>${svgDonut(top.map(x => ({ name: x.name, v: x.cost })).concat(rest ? [{ name: '기타', v: rest }] : []))}</div>` })() : naCard('서비스별 비중', S.costByService);
  const actualTables = `<details class="card"><summary><b>상세 표</b> — 리소스·태그·과금 유형·예산</summary>
${tbl('리소스별(상위 15)', ['리소스', '리소스 그룹', '서비스', '비용'], act.by_resource.slice(0, 15).map(x => [x.name, x.rg, x.service, cm(x.cost)]), S.costByResource)}
${tbl('태그별', ['키', '값', '비용'], act.by_tag.map(x => [x.key, x.value ?? '(태그 없음)', cm(x.cost)]), S.costByTag)}
${tbl('과금 유형별', ['과금 모델', '청구 유형', '비용'], act.by_pricing.map(x => [x.model, x.charge, cm(x.cost)]), S.costByPricingModel)}
${tbl('예산', ['예산', '금액', '주기', '현재 사용', '임계값'], act.budgets.map(b => [b.name, cm(b.amount), b.grain, cm(b.spend), (b.thresholds || []).join('% · ') + '%']), S.budgets)}</details>`;
  const body = R.track === 'existing' ? costExisting(aid) : costNew(aid);
  const iv = R.interview.length ? `<details class="card"><summary><b>인터뷰 비용 영역</b> — 응답 ${R.interview.filter(q => q.score).length}/${R.interview.length}</summary>${tbl('', ['문항', '주제', '질문', '점수', '요약'], R.interview.map(q => [q.id, q.topic, q.question, q.score || '-', q.summary || '']))}</details>` : '';
  return head + `<div class="cx-grid">${monthly}${svc}</div>` + body + actualTables + iv + costOut(aid);
}
function tbl(title, heads, rows, sec) {
  if (sec && (sec.status !== 'collected' || !rows.length)) return `${title ? `<h3 style="margin-top:16px">${ce(title)}</h3>` : ''}<p class="cx-na">${ce(sec.status_ko)}${sec.reason ? ' — ' + ce(sec.reason) : ''}</p>`;
  return `${title ? `<h3 style="margin-top:16px">${ce(title)}</h3>` : ''}<table><tr>${heads.map(h => `<th>${ce(h)}</th>`).join('')}</tr>${rows.map(r => `<tr>${r.map(c => `<td>${ce(c)}</td>`).join('')}</tr>`).join('')}</table>`;
}
function inp(path, v, { pct = false, step = 'any', w = 110 } = {}) { const dis = canDo('interviewer') ? '' : 'disabled'; return `<input type="number" step="${step}" style="width:${w}px" value="${pct ? +(v * 100).toFixed(2) : +(+v).toFixed(4)}" data-path="${path}" data-pct="${pct ? 1 : 0}" ${dis} onchange="costSet(this)">${pct ? ' %' : ''}` }
function costSet(el) { const p = el.dataset.path.split('.'); let o = COST_EDIT; for (let i = 0; i < p.length - 1; i++)o = o[p[i]]; const k = p[p.length - 1]; let v = el.type === 'number' ? +el.value : el.value; if (el.dataset.pct === '1') v = v / 100; o[k] = v; costSave() }
let COST_SAVE_T = null, COST_MSG = '';
function costSave() { clearTimeout(COST_SAVE_T); const aid = STATE.assessment.id; COST_SAVE_T = setTimeout(async () => { COST_MSG = '저장·재계산 중…'; const m = $('#cx-saved'); if (m) m.textContent = COST_MSG; try { COST = await api(`/api/aicaf/assessments/${aid}/cost/assumptions`, { method: 'PUT', body: JSON.stringify(COST_EDIT) }); COST._aid = aid; COST_EDIT = JSON.parse(JSON.stringify(COST.assumptions)); COST_MSG = '저장됨 · ' + new Date().toLocaleTimeString(); paintCost(aid) } catch (e) { COST_MSG = '저장 실패'; alert(e.message) } }, 400) }
async function costReset(aid) { if (!confirm('가정값을 기본값으로 되돌릴까요?')) return; COST = await api(`/api/aicaf/assessments/${aid}/cost/assumptions`, { method: 'DELETE' }); COST._aid = aid; COST_EDIT = JSON.parse(JSON.stringify(COST.assumptions)); paintCost(aid) }

function costExisting(aid) {
  const R = COST, E = R.existing, A = COST_EDIT, S = R.actual.sections;
  const kpi = `<div class="grid">${vbox('현재 월 AI 비용', E.base)}${vbox('월 절감 가능액', E.total_save)}${vbox('개선 후 월 AI 비용', E.after)}${vbox('절감률', E.save_pct, v => (v * 100).toFixed(1) + '%')}</div>`;
  const models = E.models.filter(m => m.monthly > 0).slice(0, 6);
  const mchart = models.length ? `<div class="card"><h3>AI 모델별 월 비용 <span class="cx-tip" title="${ce('출처: costByResource AI 미터 구성비 × AI 서비스 완료월 평균\n' + E.base.src)}">ⓘ</span></h3>${svgBars(models.map(m => m.family), [{ name: '입력', vals: models.map(m => m.in) }, { name: '출력', vals: models.map(m => m.out) }, { name: '캐시', vals: models.map(m => m.cached) }], { stacked: true })}</div>` : naCard('AI 모델별 월 비용', S.costByResource);
  const ok = E.levers.filter(l => l.save);
  let lvl = E.base.v || 0; const flo = ok.map(l => { lvl -= l.save; return lvl });
  const wf = E.base.v ? `<div class="card"><h3>절감 전/후 ${tip(E.total_save)}</h3>${svgBars(['개선 전'].concat(ok.map(l => l.name)).concat(['개선 후']), [{ name: '', vals: [0].concat(flo).concat([0]), color: 'transparent', hide: true }, { name: '월 AI 비용 · 절감', vals: [E.base.v].concat(ok.map(l => l.save)).concat([E.after.v]), color: 'var(--blue)', pt: i => i > 0 && i <= ok.length ? 'var(--green)' : null }], { stacked: true, own: true })}</div>` : naCard('절감 전/후', S.costByResource);
  const lev = `<div class="card" style="margin-top:var(--s5)"><div class="row"><h3 style="margin:0">토큰 최적화 시나리오</h3><span class="muted" id="cx-saved">${ce(COST_MSG)}</span>${canDo('interviewer') ? `<button class="sec" style="margin-left:auto" onclick="costReset(${aid})">가정값 기본값</button>` : ''}</div>
<p class="muted">적용 순서대로 앞 항목이 줄인 비용을 빼고 다음 항목을 계산합니다(중복 계산 방지). 비율은 가정값 — 바꾸면 바로 다시 계산·저장됩니다.</p>
<table><tr><th>개선 항목</th><th>가정</th><th>월 절감</th><th>식 · 근거</th></tr>
${E.levers.map(l => `<tr><td><b>${ce(l.name)}</b></td><td>${{ pp: inp('pp_shift', A.pp_shift, { pct: true }), batch: inp('batch_share', A.batch_share, { pct: true }), cache: inp('cache_share', A.cache_share, { pct: true }), route: inp('route_share', A.route_share, { pct: true }) + `<br><select data-path="light_model" onchange="costSet(this)" ${canDo('interviewer') ? '' : 'disabled'} style="margin-top:4px;max-width:200px"><option value="">자동(${ce(E.light_model)})</option>${R.model_options.map(m => `<option ${m === A.light_model ? 'selected' : ''}>${ce(m)}</option>`).join('')}</select>` }[l.id] || '<span class="muted">—</span>'}</td>
<td>${l.save != null ? '<b>' + cm(l.save) + '</b>' : `<span class="cx-na">${ce(l.status)}</span>`}</td><td class="muted" style="font-size:13px">${ce(l.f)}<br>${ce(l.src)}</td></tr>`).join('')}
<tr><td><b>최적화 적용 비용</b></td><td>${inp('impl_cost', A.impl_cost, { w: 120 })}</td><td>회수 ${E.payback.v == null ? '—' : E.payback_in_horizon ? E.payback.v.toFixed(1) + '개월' : A.horizon_months + '개월 내 회수 안 됨'} ${tip(E.payback)}</td><td class="muted">일회성 비용(가정)</td></tr></table></div>`;
  const tk = E.tokens || {};
  const tok = `<div class="card" style="margin-top:var(--s5)"><h3>배포별 토큰 사용량 <span class="cx-tip" title="${ce('출처: aiMetricsTokensDaily (Azure Monitor ProcessedPromptTokens·GeneratedTokens·ModelRequests, 일 단위 합)\n단가 기준 비용 = (입력 × 입력 단가 + 출력 × 출력 단가) ÷ 10⁶, pricesModels')}">ⓘ</span></h3>${(tk.rows || []).length ? `<table><tr><th>계정 · 배포</th><th>관측 일수</th><th>요청</th><th>입력 토큰</th><th>출력 토큰</th><th>요청당 입력/출력</th><th>단가 기준 비용</th></tr>${tk.rows.map(t => `<tr><td>${ce(t.account + ' · ' + t.deployment)}</td><td>${t.days}</td><td>${t.requests.toLocaleString()}</td><td>${t.in.toLocaleString()}</td><td>${t.out.toLocaleString()}</td><td>${t.in_per_req == null ? '—' : Math.round(t.in_per_req).toLocaleString() + ' / ' + Math.round(t.out_per_req).toLocaleString()}</td><td>${t.list_cost == null ? '<span class="cx-na">단가 없음</span>' : cm(t.list_cost)}</td></tr>`).join('')}</table>` : `<p class="cx-na">${ce(tk.status_ko || '수집 안 됨')}${tk.reason ? ' — ' + ce(tk.reason) : ''}</p>`}</div>`;
  const roi = E.cum.some(c => c.v != null) ? `<div class="card" style="margin-top:var(--s5)"><h3>누적 순효과 · 손익분기 ${tip(E.payback)}</h3>${svgLine(E.cum.filter(c => c.v != null), { h: 220, W: 900 })}</div>` : '';
  const note = E.base_kind && E.base_kind !== 'full' ? `<div class="banner" style="margin-top:var(--s5)">완료월 AI 비용이 0이라(사용 시작 직후) 최근 일별 AI 비용을 월로 환산해 기준액으로 썼습니다. ${ce(E.base.src)}</div>` : '';
  const pay = E.payback.v != null && !E.payback_in_horizon ? `<div class="banner" style="margin-top:var(--s5)">월 절감액 ${cm(E.total_save.v)} 로는 적용 비용 ${cm(COST_EDIT.impl_cost)} 을 ${COST_EDIT.horizon_months}개월 안에 회수하지 못합니다. 사용량이 늘면 절감액도 같은 비율로 커집니다.</div>` : '';
  return note + `<div style="margin-top:var(--s5)">${kpi}</div><div class="cx-grid">${mchart}${wf}</div>` + tok + lev + pay + roi;
}
function costNew(aid) {
  const R = COST, N = R.new, A = COST_EDIT, cur = R.currency;
  const kpi = `<div class="grid">${vbox('현재 유지 월 비용 B', N.B)}${vbox('AI 도입 월 비용 B′', N.Bp)}${vbox('월 순효과 Δ', N.D)}${vbox('회수기간', N.pay, v => v.toFixed(1) + '개월')}</div>
<div class="grid" style="margin-top:12px">${vbox('AI 운영비 A', N.A)}${vbox('토큰 비용', N.token)}${vbox('부속 서비스 X', N.X)}${vbox('환산 인원', N.FTE, v => v.toFixed(1) + '명')}</div>`;
  const I0 = N.I0.v || 0;
  const cmp = `<div class="card"><h3>현재 유지 vs AI 도입 ${tip(N.D)}</h3>${svgBars(['현재 유지', 'AI 도입'], [{ name: '인프라 I₀', vals: [I0, I0], color: 'var(--muted)' }, { name: '인건비', vals: [N.B.v - I0, N.W.v], color: 'var(--blue)' }, { name: 'AI 운영비', vals: [0, N.A.v], color: 'var(--purple)' }], { stacked: true })}
<p class="muted">I₀ ${N.I0.v == null ? '<span class="cx-na">수집 안 됨</span>' : cm(N.I0.v)} ${tip(N.I0)}</p></div>`;
  const roi = N.cum.some(c => c.v != null) ? `<div class="card"><h3>ROI 손익분기 ${tip(N.pay)}</h3>${svgLine(N.cum.filter(c => c.v != null))}
<table><tr><th>경우</th><th>월 순효과</th><th>회수기간</th></tr>${N.cases.map(c => `<tr><td>${ce(c.name)}</td><td>${cm(c.D)}</td><td>${c.pay ? c.pay.toFixed(1) + '개월' : '회수 불가'}</td></tr>`).join('')}</table></div>` : '';
  const dis = canDo('interviewer') ? '' : 'disabled';
  const tasks = `<table><tr><th>업무</th><th>월 건수 N</th><th>건당 시간 t</th><th>시간당 비용 w</th><th>기타 비용 E</th><th>자동화율 a</th><th></th></tr>${A.tasks.map((t, i) => `<tr><td><input value="${ce(t.name)}" data-path="tasks.${i}.name" onchange="costSet(this)" ${dis} style="width:140px"></td><td>${inp(`tasks.${i}.N`, t.N, { w: 110 })}</td><td>${inp(`tasks.${i}.t`, t.t, { w: 110 })}</td><td>${inp(`tasks.${i}.w`, t.w, { w: 90 })}</td><td>${inp(`tasks.${i}.E`, t.E, { w: 90 })}</td><td>${inp(`tasks.${i}.a`, t.a, { pct: true, w: 70 })}</td><td>${A.tasks.length > 1 && !dis ? `<button class="del" onclick="COST_EDIT.tasks.splice(${i},1);costSave()">빼기</button>` : ''}</td></tr>`).join('')}</table>${A.tasks.length < 5 && !dis ? `<button class="sec" style="margin-top:8px" onclick="COST_EDIT.tasks.push({name:'업무',N:100,t:0.5,w:30,E:0,a:0.5});costSave()">업무 추가</button>` : ''}`;
  const p = N.price || {};
  const ass = `<div class="card" style="margin-top:var(--s5)"><div class="row"><h3 style="margin:0">가정값</h3><span class="muted" id="cx-saved">${ce(COST_MSG)}</span>${dis ? '' : `<button class="sec" style="margin-left:auto" onclick="costReset(${aid})">가정값 기본값</button>`}</div>
<p class="muted">인터뷰 비용 영역(업무량·처리 시간·인건비·자동화율)을 받아 넣는 칸입니다. 바꾸면 즉시 저장·재계산됩니다.</p>${R.seed ? `<p class="muted">인터뷰 ${ce(R.seed.qid)} 답변에서 읽은 업무량: ${ce(R.seed.rule)} — “${ce(R.seed.quote)}”</p>` : ''}${tasks}
<div class="fg" style="margin-top:16px"><label><span>모델 (${ce(N.price_region)} 공식 단가)</span><select data-path="model" onchange="costSet(this)" ${dis}><option value="">자동(${ce(N.model)})</option>${R.model_options.map(m => `<option ${m === A.model ? 'selected' : ''}>${ce(m)}</option>`).join('')}</select><small>입력 ${p.in ?? '—'} · 출력 ${p.out ?? '—'} ${ce(cur)}/100만 토큰</small></label>
<label><span>요청당 입력 토큰 Tin</span>${inp('tin', A.tin, { step: 100 })}</label><label><span>요청당 출력 토큰 Tout</span>${inp('tout', A.tout, { step: 100 })}</label>
<label><span>AI Search SKU</span><select data-path="support.search" onchange="costSet(this)" ${dis}>${['', 'Basic', 'Standard S1', 'Standard S2'].map(x => `<option ${x === A.support.search ? 'selected' : ''} value="${x}">${x || '사용 안 함'}</option>`).join('')}</select></label>
<label><span>API Management SKU</span><select data-path="support.apim" onchange="costSet(this)" ${dis}>${['', 'Developer', 'Basic v2', 'Standard v2', 'Premium'].map(x => `<option ${x === A.support.apim ? 'selected' : ''} value="${x}">${x || '사용 안 함'}</option>`).join('')}</select></label>
<label><span>Log Analytics 수집량(GB/월)</span>${inp('support.log_gb', A.support.log_gb)}</label>
<label><span>초기 구축비 K (${ce(cur)})</span>${inp('K', A.K, { w: 140 })}</label><label><span>민감도 ±</span>${inp('sens', A.sens, { pct: true })}</label>
<label><span>부속 서비스 단가 지역</span><select data-path="region" onchange="costSet(this)" ${dis}>${['koreacentral', 'eastus2', 'swedencentral', 'westus2'].map(x => `<option ${x === A.region ? 'selected' : ''}>${x}</option>`).join('')}</select></label></div>
<table style="margin-top:12px"><tr><th>부속 서비스</th><th>SKU</th><th>단가</th><th>수량</th><th>월 비용</th></tr>${N.support.map(s => `<tr><td>${ce(s.service)}</td><td>${ce(s.sku)}</td><td>${s.price == null ? '<span class="cx-na">단가 없음</span>' : s.price + ' / ' + ce(s.unit)}</td><td>${s.qty}</td><td>${cm(s.monthly)}</td></tr>`).join('')}</table></div>`;
  return `<div style="margin-top:var(--s5)">${kpi}</div><div class="cx-grid">${cmp}${roi}</div>` + ass;
}
function costOut(aid) {
  const j = (COST && COST.job) || { status: 'none' }, run = j.status === 'running';
  const st = { none: '아직 만들지 않음', running: '생성 중', done: '완료', failed: '실패' }[j.status] || j.status;
  return `<div class="card"><h3>상세 결과 받기</h3><div class="row"><button ${run || !canDo('interviewer') ? 'disabled' : ''} onclick="costBuild(${aid})">${j.status === 'done' ? '다시 생성' : '엑셀·PPT 생성'}</button><span>상태 <b>${ce(st)}</b></span>${j.finished ? `<span class="muted">${new Date(j.finished * 1000).toLocaleString()}</span>` : ''}</div>
${j.steps ? `<div class="steps">${j.steps.map(s => `<span class="pill ${s.status === 'failed' ? 'must' : s.status === 'ok' ? 'trk-new' : ''}">${ce(s.label)} · ${ce(STEP_KO[s.status] || s.status)}</span>`).join('')}</div>` : ''}${j.error ? `<p class="err">${ce(j.error)}</p>` : ''}
${(j.files || []).length ? `<div class="row" style="margin-top:10px">${j.files.map(f => `<a href="/api/aicaf/assessments/${aid}/cost/file/${f.kind}"><button class="sec">${f.kind === 'xlsx' ? '엑셀 받기 (수식 포함)' : 'PPT 받기'} · ${fmtB(f.bytes)}</button></a>`).join('')}</div>` : ''}
<p class="muted">엑셀은 가정값 시트의 노란 칸을 바꾸면 시나리오·ROI 가 다시 계산됩니다. 화면·엑셀·PPT 는 같은 계산 결과를 씁니다(생성 시점 가정값 기준).</p></div>`;
}
async function costBuild(aid) { try { COST.job = await api(`/api/aicaf/assessments/${aid}/cost/build`, { method: 'POST', body: '{}' }) } catch (e) { alert(e.message) } paintCost(aid); pollCost(aid) }
function pollCost(aid) { clearTimeout(COST_PT); COST_PT = setTimeout(async () => { if (TAB !== 'cost') return; COST.job = await api(`/api/aicaf/assessments/${aid}/cost/job`); paintCost(aid) }, 2000) }
