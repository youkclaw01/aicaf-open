/* 세 보고서의 실제 서버 계약과 네이티브 산출물 상태만 표시한다. */
(function(root){
'use strict';
const escapeHtml=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function canOpen(state,tab){
  if(tab==='infra'||tab==='team'||tab==='arch'||tab==='cost')return true;
  if(tab==='interview')return state?.questionnaire_ready===true||(state?.reports||[]).some(r=>r.kind==='interview'&&r.record?.status==='ready');
  if(tab==='integrated')return (state?.reports||[]).some(r=>r.kind==='interview'&&r.ready===true)||(state?.reports||[]).some(r=>r.kind==='integrated'&&r.record?.status==='ready');
  return false;
}
function phaseText(job){
  const units=job?.progress?.units||[];
  const active=units.filter(u=>u.status==='running');
  const unit=active.length?active[active.length-1]:units[units.length-1];
  if(!unit)return job?.status==='queued'?'실행 대기':'현재 처리 단계를 확인 중입니다.';
  const p=unit.phase||'',n=unit.unit||'';
  const label=p==='visual_review'?'실제 장표 시각 검토':p==='content_review'?'내용 독립 검토':
    n.startsWith('issue-plan')?'현황 분석 범위 계획':n.startsWith('area-')?'영역별 현황 작성·검토':
    p==='native'?'Excel 동결·PPT 생성':p==='render'?'실제 문서 렌더':p==='consistency'?'문서·근거 일관성 검증':
    p==='interpret'?'입력과 요구 범위 확인':'분석·검토 처리';
  const done=units.filter(u=>u.status==='completed'||u.status==='reused').length;
  return label+(units.length?` · 확인된 세부 처리 ${done}/${units.length}`:'');
}
let current=null,epoch=0,timer=null;
function unmount(){epoch++;if(timer)clearTimeout(timer);timer=null;current=null;}
async function api(url,options){
  const response=await fetch(url,options);
  if(!response.ok)throw new Error(await response.text());
  return response.json();
}
function live(ctx){return current===ctx&&ctx.epoch===epoch&&ctx.element.isConnected;}
function renderStages(job){
  const stage=job?.progress?.stage;
  if(!stage||!Array.isArray(stage.stages)||!stage.stages.length)return '';
  const done=job?.status==='generated_candidate';
  const mark={done:'완료',running:'진행 중',pending:'대기',waiting:'대기',failed:'중단'};
  const pct=done?100:Math.max(0,Math.min(100,Number(stage.percent)||0));
  const label=done?`18단계 - 100% 완료 / 총 ${stage.total||18}단계`:escapeHtml(stage.label||'');
  const rows=stage.stages.map(r=>{
    const st=done?'done':(r.state||'waiting');
    const unitInfo=r.total_units?` (${r.done_units||0}/${r.total_units})`:'';
    const badgeCls=st==='done'?'background:var(--green-t);color:var(--green)':st==='running'?'background:var(--blue-t);color:var(--blue)':st==='failed'?'background:var(--red-t);color:var(--red)':'background:var(--fill2);color:var(--muted)';
    return `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 12px;border-radius:var(--r-sm);background:${st==='running'?'var(--blue-t)':'var(--fill2)'}">
      <span style="font-size:13px;font-weight:${st==='running'?'600':'500'};color:var(--ink)"><b style="color:var(--muted);margin-right:6px;font-variant-numeric:tabular-nums">${escapeHtml(r.n)}.</b>${escapeHtml(r.name)}${escapeHtml(unitInfo)}</span>
      <span class="pill" style="${badgeCls};font-size:11px;padding:2px 8px">${escapeHtml(mark[st]||st)}</span>
    </div>`;
  }).join('');
  return `<div class="card" style="margin-top:var(--s4)">
    <div class="row" style="justify-content:space-between">
      <h3 style="margin:0">18단계 진행 현황</h3>
      <span class="pill" style="background:var(--blue-t);color:var(--blue);font-weight:600">${label}</span>
    </div>
    <div style="height:8px;background:var(--fill);border-radius:4px;overflow:hidden;margin:12px 0">
      <div style="height:100%;width:${pct}%;background:var(--blue);transition:width .3s"></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:12px">${rows}</div>
  </div>`;
}
function renderCompletedContent(ctx,rec){
  const ov=rec?.overview;
  if(!ov)return '';
  const prioKo={first:'1순위 (최우선)',next:'2순위 (전제 확인 후)',later:'3순위 (단계적 추진)',pending:'우선순위 확인 필요',second:'2순위',third:'3순위'};
  const changeKo={modify:'변경',add:'추가',retain:'유지',verify:'확인·검증'};
  const areasHtml=(ov.areas||[]).length?`<div class="card" style="margin-top:var(--s4)">
    <h3 style="margin-top:0">영역별 진단 요약 (${ov.areas.length}개 영역)</h3>
    <table>
      <tr><th style="width:130px">영역</th><th>핵심 진단 결과 및 시사점</th><th style="width:90px;text-align:right">관측 건수</th></tr>
      ${ov.areas.map(a=>`<tr><td><b>${escapeHtml(a.name)}</b></td><td style="font-size:14px;line-height:1.5">${escapeHtml(a.headline)}</td><td style="text-align:right" class="muted">${escapeHtml(a.observations)}건</td></tr>`).join('')}
    </table>
  </div>`:'';
  if(ctx.kind!=='integrated'){
    return `<div class="grid" style="margin-top:var(--s4);grid-template-columns:repeat(3,1fr)">
      <div class="kpi"><div class="muted">보고서 분량</div><div class="v">${escapeHtml(ov.pages||rec.pages||'-')}장</div></div>
      <div class="kpi"><div class="muted">영역별 분석</div><div class="v">${escapeHtml((ov.areas||[]).length)}개 영역</div></div>
      <div class="kpi"><div class="muted">실렌더 시각 검토</div><div class="v">${escapeHtml(ov.reviewed_pages||ov.pages||'-')}장 완료</div></div>
    </div>`+areasHtml;
  }
  const kpis=`<div class="grid" style="margin-top:var(--s4)">
    <div class="kpi"><div class="muted">최종 보고서 분량</div><div class="v">${escapeHtml(ov.pages||rec.pages||'-')}장</div></div>
    <div class="kpi"><div class="muted">시사점 종합 방향</div><div class="v">${escapeHtml(ov.direction_count||0)}건</div></div>
    <div class="kpi"><div class="muted">핵심 진단 항목</div><div class="v">${escapeHtml(ov.diagnosis_count||0)}건</div></div>
    <div class="kpi"><div class="muted">개선과제 (Action)</div><div class="v">${escapeHtml(ov.action_count||0)}건</div></div>
  </div>`;
  const actionsHtml=(ov.actions||[]).length?`<div class="card" style="margin-top:var(--s4)">
    <h3 style="margin-top:0">도출 개선과제 목록 (${ov.actions.length}건)</h3>
    <table>
      <tr><th style="width:110px">과제 ID</th><th>개선과제명 및 산출물</th><th style="width:200px">적용 대상</th><th style="width:140px">구분·우선순위</th></tr>
      ${ov.actions.map(a=>`<tr>
        <td><code>${escapeHtml(a.id)}</code></td>
        <td style="font-size:14px"><b>${escapeHtml(a.title)}</b>${a.deliverable?`<div class="muted" style="font-size:13px;margin-top:2px">산출물: ${escapeHtml(a.deliverable)}</div>`:''}</td>
        <td class="muted" style="font-size:13px">${escapeHtml(a.target||'-')}</td>
        <td><span class="pill">${escapeHtml(changeKo[a.change_type]||a.change_type||'')} · ${escapeHtml(prioKo[a.priority_level]||a.priority_level||'-')}</span></td>
      </tr>`).join('')}
    </table>
  </div>`:'';
  return kpis+areasHtml+actionsHtml;
}
function draw(ctx){
  if(!live(ctx))return;
  const state=ctx.state,report=(state.reports||[]).find(r=>r.kind===ctx.kind);
  if(!report){ctx.element.textContent='보고서 처리 계약을 확인하지 못했습니다.';return;}
  const rec=report.record,job=rec?.job;
  const running=job&&['queued','running'].includes(job.status);
  const status=running?'진행 중':report.ready?'생성·자동 검토 완료':
    ['failed','interrupted','invalid'].includes(report.status)?'확인 필요':'준비 중';
  const urls=rec?.status==='ready'?['pptx','xlsx','pdf'].map(kind=>
    `<a href="/api/aicaf/assessments/${ctx.aid}/workflow/reports/${ctx.kind}/revisions/${encodeURIComponent(rec.revision_id)}/files/${kind}"><button class="sec">${kind.toUpperCase()} 받기</button></a>`).join(''):'';
  const can=ctx.canEdit&&report.can_generate&&!ctx.pending;
  const title=ctx.kind==='integrated'?'Summary Report (현황분석 종합 및 시사점 및 개선과제 보고서)':report.name;
  const note=ctx.kind==='current_state'?'관측 현황·구성·운영·비용과 확인 범위만 분석합니다. 최종 시사점과 개선과제는 5. Summary Report에서 종합합니다.':
    ctx.kind==='interview'?'공식 지침과 현황에 연결된 실제 질문·고객 답변·평가 근거를 독립 보고서로 동결합니다.':
    '완료된 현황분석 보고서와 인터뷰 보고서의 같은 버전을 근거로 종합 판단·시사점·개선과제를 작성합니다.';
  ctx.element.innerHTML=`<div class="card"><div class="row"><h3 style="margin:0">${escapeHtml(title)}</h3><span class="pill">${escapeHtml(status)}</span></div>
    <p class="muted">${note}</p>
    ${running?`<p>${escapeHtml(phaseText(job))}</p>`:''}
    ${job?.error?`<p class="err">${escapeHtml(job.error)}</p>`:''}
    ${rec?.error?`<p class="err">${escapeHtml(rec.error)}</p>`:''}
    ${state.source_error&&ctx.kind==='current_state'?`<p class="muted">${escapeHtml(state.source_error)}</p>`:''}
    ${!report.can_generate&&!running?`<p class="muted">${escapeHtml(report.blocked_reason||'현재 원천과 선행 보고서를 확인해야 합니다.')}</p>`:''}
    ${urls&&!report.ready?'<p class="muted">아래 파일은 이전 개정입니다. 현재 입력의 완료 근거로 사용하지 않습니다.</p>':''}
    <div class="row" style="gap:8px">${ctx.canEdit?`<button data-action="generate" ${can?'':'disabled'}>${ctx.pending?'요청 확인 중…':report.ready?'다시 생성':ctx.kind==='interview'?'인터뷰 보고서 생성':'분석·보고서 생성'}</button>`:''}${urls}</div>
    ${ctx.kind==='current_state'&&report.ready&&!state.questionnaire_ready&&ctx.canEdit?'<p class="muted">현황분석 보고서는 보존됩니다. 공식 지침 기반 질문 구성만 다시 처리할 수 있습니다.</p><button class="sec" data-action="questions">질문 구성 다시 처리</button>':''}
    ${report.ready?'<p class="muted" style="margin-bottom:0">동일 입력·내용 검토·실제 장표 검토 완료. 고객 제출 승인은 별도입니다.</p>':''}
    ${ctx.error?`<p class="err">${escapeHtml(ctx.error)}</p>`:''}</div>
    ${renderStages(job)}${renderCompletedContent(ctx,rec)}`;
  const generate=ctx.element.querySelector('[data-action="generate"]');
  if(generate)generate.addEventListener('click',()=>start(ctx));
  const questions=ctx.element.querySelector('[data-action="questions"]');
  if(questions)questions.addEventListener('click',()=>regenerateQuestions(ctx));
}
async function refresh(ctx){
  if(!live(ctx)||ctx.refreshing)return;
  if(timer)clearTimeout(timer);timer=null;ctx.refreshing=true;
  try{
    const next=await api(`/api/aicaf/assessments/${ctx.aid}/workflow`);
    if(!live(ctx))return;
    const prior=ctx.state;ctx.state=next;ctx.error=null;draw(ctx);
    if(prior&&(prior.current_step!==next.current_step||prior.questionnaire_sha256!==next.questionnaire_sha256||prior.source_sha256!==next.source_sha256)){
      if(ctx.onTransition)await ctx.onTransition(next);
    }
  }catch(error){if(live(ctx)){ctx.error=error.message;draw(ctx);}}
  ctx.refreshing=false;
  if(live(ctx))timer=setTimeout(()=>refresh(ctx),3500);
}
async function start(ctx){
  if(!live(ctx)||ctx.pending)return;
  const report=ctx.state.reports.find(r=>r.kind===ctx.kind);
  if(!report?.can_generate||!ctx.canEdit)return;
  if(report.ready&&!root.confirm('보고서를 다시 생성합니다. 이전 파일은 보존되며 변경된 근거에 연결된 후속 보고서는 다시 확인해야 합니다. 계속할까요?'))return;
  ctx.pending=true;ctx.error=null;draw(ctx);
  try{
    const input=await api(`/api/aicaf/assessments/${ctx.aid}/workflow/reports/${ctx.kind}`);
    if(!live(ctx))return;
    if(!ctx.nonce)ctx.nonce=root.crypto.randomUUID();
    const result=await api(`/api/aicaf/assessments/${ctx.aid}/workflow/reports/${ctx.kind}/start`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nonce:ctx.nonce,
        input_sha256:input.input_sha256,parent_revision_id:input.candidate?.revision_id||null,requirements:''})});
    if(!live(ctx))return;
    ctx.nonce=null;ctx.pending=false;await refresh(ctx);
  }catch(error){if(live(ctx)){ctx.pending=false;ctx.error=error.message;draw(ctx);}}
}
async function regenerateQuestions(ctx){
  if(!live(ctx)||ctx.pending||!ctx.canEdit)return;
  ctx.pending=true;draw(ctx);
  try{
    await api(`/api/aicaf/assessments/${ctx.aid}/workflow/questions`,{method:'POST'});
    if(live(ctx)){ctx.pending=false;await refresh(ctx);}
  }catch(error){if(live(ctx)){ctx.pending=false;ctx.error=error.message;draw(ctx);}}
}
function mount(aid,kind,element,options={}){
  unmount();if(!element)return;
  const ctx={aid:Number(aid),kind,element,epoch,canEdit:options.canEdit===true,state:options.state,
    onTransition:options.onTransition,pending:false,error:null,nonce:null};
  current=ctx;if(ctx.state)draw(ctx);refresh(ctx);
}
const exported={canOpen,phaseText,mount,unmount};
if(typeof module!=='undefined'&&module.exports)module.exports=exported;
root.AssessmentFlow=exported;
})(typeof window!=='undefined'?window:globalThis);
