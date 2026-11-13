// 아키텍처 탭 화면(AS-IS·TO-BE 하위 화면, D1·D2 카드). index.html 의 디자인 토큰(CSS 변수)·기존 클래스만 쓴다.
let ARCH = null, ARCH_SUB = 'AS-IS', ARCH_PT = null;
const AX = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ARCH_D = { D1: '전체 인프라 상세도', D2: '네트워크 흐름도' };
const ARCH_ST = { ok: '완료', running: '진행 중', failed: '실패', '대기': '대기' };
const ARCH_CK = { A1: '리전이 가장 바깥', A2: '행 순서', A3: '관리 블록 분리', A4: '판독 노드 일치', A5: '근거 없는 서비스 0', A6: '선 색=방향', A7: '겹침·넘침 0', A8: 'D2 범례·띠·패널', A9: '샘플 명칭 0', A10: '외부 진입 행·inbound 선', A11: '수집 관계 선 80% 이상', A12: '개수·표시명', A13: '빈 상자 0·채움률', A14: '주황/초록=근거', A15: '이름 잘림 0' };
const ARCH_FILE = { 'diagram.pptx': 'PPT', 'image.png': '이미지(모델 초안)', 'spec.md': '명세(md)', 'spec.json': '명세(json)', 'layout.json': '판독(layout.json)', 'preview.png': '미리보기' };
async function archLoad(aid) { ARCH = await api(`/api/aicaf/assessments/${aid}/arch`); ARCH._aid = aid }
function archView(aid) { if (!ARCH || ARCH._aid !== aid) { archLoad(aid).then(() => { paintArch(aid); pollArch(aid) }); return '<div class="card" id="arch">불러오는 중…</div>' } setTimeout(() => pollArch(aid), 0); return `<div id="arch">${archBody(aid)}</div>` }
function paintArch(aid) { const el = $('#arch'); if (el) el.innerHTML = archBody(aid) }
function archBody(aid) {
  const A = ARCH; const can = canDo('interviewer');
  const sub = `<div class="topics">${['AS-IS', 'TO-BE'].map(s => `<button class="${ARCH_SUB === s ? 'on' : ''}" onclick="ARCH_SUB='${s}';paintArch(${aid})">${s} 아키텍처</button>`).join('')}</div>`;
  const head = `<div class="card"><h3>${ARCH_SUB} 아키텍처</h3><p class="muted">${ARCH_SUB === 'AS-IS' ? '인프라 분석 수집 결과(topology·evidence)와 현황 판정으로 그립니다. 미흡 항목은 주황 테두리와 발견 ID로 표시합니다.' : `AS-IS 위에 과제의 추가·변경만 얹습니다(초록 = 신규·변경, 과제 ID 표시). 과제 출처: <b>${AX(A.tobe_source)}</b>.`} 아이콘은 Microsoft 공식 Azure 아이콘(V24)만 씁니다.</p>${A.ready ? '' : '<p class="err">인프라 분석 탭에서 수집 결과(topology.json)를 먼저 올려 주세요.</p>'}</div>`;
  return sub + head + `<div class="cx-grid">${['D1', 'D2'].map(d => archCard(aid, A.jobs[ARCH_SUB + '_' + d], d, can && A.ready)).join('')}</div>`
}
function archCard(aid, j, d, can) {
  const st = j.status; const run = st === 'running';
  const steps = (j.steps || []).length ? `<div class="steps">${j.steps.map(s => `<span class="step ${STEP_CLS[s.status] || ''}">${AX(s.label)}<small>${AX(ARCH_ST[s.status] || s.status)}${s.detail ? ' · ' + AX(s.detail) : ''}</small></span>`).join('')}</div>` : '';
  const ck = j.checks && Object.keys(j.checks).length ? `<table class="cx-tbl" style="margin-top:var(--s3)"><tr><th>검사</th><th>내용</th><th>결과</th></tr>${Object.entries(j.checks).map(([k, v]) => `<tr><td>${k} ${AX(ARCH_CK[k] || '')}</td><td class="muted">${AX(v.detail)}</td><td><span class="pill ${v.pass ? '' : 'must'}">${v.pass ? '통과' : '미통과'}</span></td></tr>`).join('')}</table>` : '';
  const ic = j.icons ? `<p class="muted">PPT 아이콘 ${j.icons.pictures}개 · ${j.icons.distinct.length}종 · icon-map 밖 ${j.icons.not_in_icon_map.length}개${j.icons.warnings.length ? ' · 경고 ' + AX(j.icons.warnings.join(', ')) : ''}</p>` : '';
  const files = (j.files || []).length ? `<div class="row" style="margin-top:var(--s3)">${j.files.map(f => `<a class="open" href="/api/aicaf/assessments/${aid}/arch/${j.state}/${d}/file/${f.name}?dl=1">${AX(ARCH_FILE[f.name] || f.name)}</a>`).join('')}</div>` : '';
  const img = (j.files || []).some(f => f.name === 'image.png') ? `<div class="row" style="margin-top:var(--s3);align-items:flex-start"><a href="/api/aicaf/assessments/${aid}/arch/${j.state}/${d}/file/image.png" target="_blank"><img src="/api/aicaf/assessments/${aid}/arch/${j.state}/${d}/file/image.png?t=${j.finished || ''}" alt="모델 초안" style="width:100%;border-radius:var(--r);box-shadow:var(--shadow)"></a></div>` : '';
  const label = { none: '아직 없음', running: `생성 중 · ${j.round || 1}회차`, done: '완료', failed: '실패' }[st] || st;
  return `<div class="card"><div class="row"><h3 style="margin:0">${d} ${ARCH_D[d]}</h3><span class="pill ${st === 'failed' ? 'must' : ''}">${AX(label)}</span><button style="margin-left:auto" ${can && !run ? '' : 'disabled'} onclick="archStart(${aid},'${j.state}','${d}')">${st === 'none' ? '생성' : '다시 생성'}</button></div>
${steps}${j.error ? `<p class="err">${AX(j.error)}</p>` : ''}${ck}${ic}${files}${img}${run ? `<p class="live">10초마다 자동 갱신 · ${new Date().toLocaleTimeString()}</p>` : ''}</div>`
}
async function archStart(aid, s, d) { try { ARCH.jobs[s + '_' + d] = await api(`/api/aicaf/assessments/${aid}/arch/${s}/${d}/start`, { method: 'POST', body: '{}' }) } catch (e) { alert(e.message) } paintArch(aid); pollArch(aid) }
function pollArch(aid) { clearTimeout(ARCH_PT); if (!ARCH || !Object.values(ARCH.jobs).some(j => j.status === 'running')) return; ARCH_PT = setTimeout(async () => { if (TAB !== 'arch') return; try { await archLoad(aid) } catch (e) { } paintArch(aid); pollArch(aid) }, 10000) }
