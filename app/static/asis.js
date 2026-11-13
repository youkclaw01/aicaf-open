// 현황분석 전용 UI. 완료된 검토 단계와 제출 승인을 구분하고 입력·포커스를 보존한다.
(function () {
  'use strict';
  const S={aid:null,current:null,draft:'',error:'',busy:false,seq:0,root:null,shell:null,timer:null};
  const E=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const base=aid=>'/api/aicaf/assessments/'+encodeURIComponent(aid)+'/asis';
  const statusLabels={queued:'실행 대기',running:'작성·검토 진행 중',generated_candidate:'생성 후보',failed:'생성 실패',interrupted:'작업 중단'};
  const phaseLabels={interpret:'목표·요구 해석',author:'작성',content_review:'내용 검토',repair:'보완',native:'PPT·현황 데이터 생성',render:'실제 렌더',visual_review:'시각 검토',consistency:'원장 대조'};
  const unitLabels={interpret:'목표·요구 해석','issue-plan':'분석 이슈 구성','synthesis-plan':'종합 방향 구성','action-item-plan':'진단·개선과제 구성','area-shared-unresolved':'공통 미확인 항목',synthesis:'종합 분석',actions:'개선·확인 과제',summary:'요약',document:'전체 문서',native_pair:'PPT·현황 데이터',actual_render:'실제 PPT 렌더',visual_pages:'장표별 시각 검토',pair_consistency:'최종 원장 대조'};
  function stageBlock(latest){
    const stage=latest.progress?.stage;if(!stage||!Array.isArray(stage.stages))return null;
    const done=latest.status==='generated_candidate';
    const mark={done:'완료',running:'진행 중',pending:'대기',waiting:'대기',failed:'중단'};
    const rows=stage.stages.map(r=>'<li class="as-st as-st-'+E(r.state)+(r.n===stage.current&&!done?' as-st-now':'')+'"><span class="as-st-n">'+E(r.n)+'</span><span class="as-st-name">'+E(r.name)+'</span><span class="as-st-state">'+E(mark[r.state]||r.state)+(r.total_units&&r.state!=='done'?' · '+E(r.done_units)+'/'+E(r.total_units):'')+'</span></li>').join('');
    const pct=done?100:Math.max(0,Math.min(100,Number(stage.percent)||0));
    return '<p class="as-live as-stage-label">'+E(stage.label)+'</p><div class="as-track" role="progressbar" aria-label="현재 단계 진행률" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+pct+'"><div class="as-fill" style="width:'+pct+'%"></div></div>'+(stage.reopened?'<p class="as-muted">앞 단계의 보완 때문에 이 단계를 다시 진행하고 있습니다.</p>':'')+'<ol class="as-stages" aria-label="현황분석 18단계">'+rows+'</ol>';
  }
  async function request(url,options) {
    const r=await fetch(url,{credentials:'same-origin',redirect:'error',...options,headers:{'Content-Type':'application/json'}});
    const text=await r.text();if(!r.ok)throw new Error(text);
    try{return JSON.parse(text);}catch(e){throw new Error('로그인 상태와 현황분석 API 응답을 확인해야 합니다.');}
  }
  const file=(aid,revision,kind,download)=>base(aid)+'/revisions/'+encodeURIComponent(revision)+'/files/'+kind+(download?'?download=1':'');
  function quality(row) {return row&&row.quality_status==='automated_review_passed'?'자동 검토 완료 · 제출 승인 전':'생성 후보 · 자동 검토 미완료';}

  const senderLabels={analysis_owner:'현황분석 담당',area_owner:'영역 담당',global_manager:'전체 관리자',human:'결정'};
  async function chatPoll(aid,revision) {
    if(!revision||!S.root||!S.root.isConnected)return;
    const key=aid+':'+revision;
    if(S.chatKey!==key){S.chatKey=key;S.chat={after:0,messages:[],open:[],closed:false,text:'',busy:false,error:''};}
    try{
      const data=await request(base(aid)+'/revisions/'+encodeURIComponent(revision)+'/chat?after='+S.chat.after);
      if(S.chatKey!==key)return;
      if(data.messages.length){S.chat.messages=S.chat.messages.concat(data.messages);S.chat.after=data.messages[data.messages.length-1].seq;}
      S.chat.open=data.open_questions;S.chat.closed=data.closed;S.chat.error='';
    }catch(e){if(S.chatKey===key)S.chat.error=e.message;}
    drawChat();
  }
  function drawChat() {
    const box=S.root&&S.root.querySelector('#as-chat');if(!box)return;
    const c=S.chat;
    const visible=!!c&&(c.messages.length>0||c.open.length>0)&&!c.closed;
    if(!visible){box.hidden=true;if(box.innerHTML)box.innerHTML='';return;}
    box.hidden=false;
    const focus=box.contains(document.activeElement)&&document.activeElement.id==='as-chat-text';
    const typed=focus?document.activeElement.value:c.text;
    const log=c.messages.map(m=>'<li class="as-msg'+(m.sender==='human'?' human':'')+'"><small>'+E(senderLabels[m.sender]||m.sender)+'</small>'+E(m.text)+'</li>').join('');
    const canDecide=!!(S.current&&S.current.can_edit);
    const questions=c.open.map(q=>'<div class="as-q" data-q="'+E(q.id)+'"><p>'+E(q.summary)+'</p>'+
      (canDecide?'<div class="as-actions">'+q.options.map(o=>'<button class="sec" data-chat-option="'+E(o.id)+'" data-chat-q="'+E(q.id)+'"'+(c.busy?' disabled':'')+'>'+E(o.label)+'</button>').join('')+'</div>'+
      '<label class="as-chat-note" for="as-chat-text">선택지 대신 직접 지시를 입력할 수도 있습니다.</label><textarea id="as-chat-text" rows="3" maxlength="4000">'+E(typed)+'</textarea>'+
      '<div class="as-actions"><button data-chat-send="'+E(q.id)+'"'+(c.busy?' disabled':'')+'>지시 보내기</button></div>':
      '<p class="as-chat-note">결정 권한이 있는 담당자가 답변해야 합니다.</p>')+'</div>').join('');
    box.innerHTML='<h4>현황분석 담당</h4><p class="as-chat-note">'+(c.open.length?'확인이 필요한 항목이 있습니다. 결정하면 같은 작업이 이어서 진행됩니다.':'진행 중 발생한 문제와 처리 결과를 알려 드립니다.')+'</p><ul class="as-chat-log" aria-live="polite">'+log+'</ul>'+questions+(c.error?'<p class="as-error">'+E(c.error)+'</p>':'');
    const logBox=box.querySelector('.as-chat-log');if(logBox)logBox.scrollTop=logBox.scrollHeight;
    if(focus){const t=box.querySelector('#as-chat-text');if(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);}}
  }
  async function chatDecide(q,body) {
    const c=S.chat,latest=(S.current||{}).latest||{};if(!c||c.busy||!latest.revision_id)return;
    c.busy=true;drawChat();
    try{
      await request(base(S.aid)+'/revisions/'+encodeURIComponent(latest.revision_id)+'/chat/questions/'+encodeURIComponent(q)+'/decision',{method:'POST',body:JSON.stringify(body)});
      c.text='';c.error='';
    }catch(e){c.error=e.message;}
    c.busy=false;await chatPoll(S.aid,latest.revision_id);
  }
  function shell() {
    if(S.shell===S.root)return;
    if(!document.getElementById('asis-component-style')) {
      const style=document.createElement('style');style.id='asis-component-style';
      style.textContent='#asis-root{overflow-wrap:anywhere}#asis-root h3{color:#1d1d1f}#asis-root .as-muted{color:#6e6e73;font-size:14px;line-height:1.65}#asis-root .as-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:16px}#asis-root button{display:inline-flex;align-items:center;justify-content:center;min-height:40px;padding:0 16px;line-height:1.2}#asis-root button:disabled{background:#e8e8ed;color:#86868b;border-color:#e8e8ed}#asis-root textarea{box-sizing:border-box;width:100%;min-height:110px;margin-top:8px;font:inherit}#asis-root .as-track{height:8px;background:#e8e8ed;border-radius:4px;overflow:hidden;margin:12px 0}#asis-root .as-fill{height:100%;background:#0071e3;transition:width .3s}#asis-root .as-order{display:flex;gap:8px;flex-wrap:wrap;padding:0;list-style:none;font-size:13px;color:#6e6e73}#asis-root .as-order li{padding:4px 0}#asis-root .as-order li+li:before{content:"→";margin-right:8px}#asis-root .as-units{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 24px;margin:16px 0}#asis-root .as-unit{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #e8e8ed;padding:8px 0;font-size:14px}#asis-root .as-unit span:last-child{color:#6e6e73;text-align:right}#asis-root .as-live{font-weight:600;color:#1d1d1f}#asis-root .as-result-links{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}#asis-root code{font-size:12px;overflow-wrap:anywhere}#asis-root .as-error{color:#e30000;white-space:pre-wrap}#asis-root .as-chat{margin-top:20px;border-top:1px solid #e8e8ed;padding-top:16px}#asis-root .as-chat h4{margin:0 0 4px;color:#1d1d1f;font-size:15px;font-weight:600}#asis-root .as-chat-log{max-height:300px;overflow:auto;margin:12px 0;padding:8px 0;list-style:none;display:flex;flex-direction:column;gap:8px;border-top:1px solid #f0f0f2;border-bottom:1px solid #f0f0f2}#asis-root .as-msg{max-width:86%;padding:8px 12px;border-radius:14px;font-size:14px;line-height:1.55;background:#f5f5f7;color:#1d1d1f;align-self:flex-start}#asis-root .as-msg.human{align-self:flex-end;background:#0071e3;color:#fff}#asis-root .as-msg small{display:block;color:#6e6e73;font-size:12px;margin-bottom:2px}#asis-root .as-msg.human small{color:#d2e6ff}#asis-root .as-q{border:1px solid #d2d2d7;border-radius:12px;padding:12px 14px;margin:8px 0;background:#fff}#asis-root .as-q p{margin:0 0 8px;font-size:14px;line-height:1.55;color:#1d1d1f}#asis-root .as-chat-note{color:#6e6e73;font-size:13px;margin:6px 0 0}@media(max-width:620px){#asis-root .as-units{grid-template-columns:1fr}#asis-root .as-actions button{flex:1;min-width:140px}}#asis-root .as-stages{list-style:none;margin:12px 0 0;padding:0}#asis-root .as-st{display:grid;grid-template-columns:28px 1fr auto;gap:12px;align-items:center;padding:7px 0;border-bottom:1px solid #e8e8ed;font-size:14px;color:#6e6e73}#asis-root .as-st-n{font-variant-numeric:tabular-nums;color:#86868b}#asis-root .as-st-done .as-st-name{color:#1d1d1f}#asis-root .as-st-now{color:#1d1d1f;font-weight:600}#asis-root .as-st-now .as-st-state{color:#0071e3}#asis-root .as-st-failed .as-st-state{color:#b3261e}#asis-root .as-st-state{text-align:right;font-variant-numeric:tabular-nums}#asis-root .as-stage-label{font-size:17px}';
      document.head.append(style);
    }
    S.root.innerHTML='<section class="card"><h3>3. 현황분석 작성·검토</h3><p class="as-muted">고객 목표와 수집 근거를 분석하고, 작성·내용 검토·장표 시각 검토를 분리해 진행합니다.</p><div id="as-message" role="alert"></div><div id="as-progress"></div><div id="as-chat" class="as-chat" hidden></div><label for="asis-requirements">추가 요구사항 — 다음 생성 요청에 적용</label><textarea id="asis-requirements" rows="4" maxlength="6000"></textarea><p class="as-muted">추가 요구와 직전 분석은 개정에 보존됩니다. 추가 요구를 확인된 운영 사실이나 충족 결과로 간주하지 않습니다.</p><div class="as-actions"><button data-as-action="start">새 현황분석 생성</button><button class="sec" data-as-action="refresh">현재 상태 새로 조회</button></div><div id="as-source"></div></section><section class="card"><h3>4. 현황분석 결과</h3><div id="as-result"></div></section>';
    S.root.querySelector('#asis-requirements').value=S.draft;
    S.root.onclick=e=>{const o=e.target.closest('[data-chat-option]');if(o&&S.root.contains(o)){chatDecide(o.dataset.chatQ,{option_id:o.dataset.chatOption});return;}const g=e.target.closest('[data-chat-send]');if(g&&S.root.contains(g)){const t=S.root.querySelector('#as-chat-text');const v=t?t.value.trim():'';if(v)chatDecide(g.dataset.chatSend,{free_text:v});return;}const b=e.target.closest('[data-as-action]');if(!b||!S.root.contains(b))return;const a=b.dataset.asAction;if(a==='start')start(S.aid);else if(a==='preview')preview(S.aid,S.current?.candidate?.revision_id);else mount(S.aid);};
    S.root.oninput=e=>{if(e.target.id==='asis-requirements')S.draft=e.target.value;if(e.target.id==='as-chat-text'&&S.chat)S.chat.text=e.target.value;};
    S.shell=S.root;
  }
  function draw() {
    if(!S.root||!S.root.isConnected)return;shell();
    const c=S.current||{},latest=c.latest||{},candidate=c.candidate||{},running=['queued','running'].includes(latest.status);
    const can=c.can_edit&&c.can_generate&&!S.busy&&!running;
    S.root.querySelector('#asis-requirements').disabled=!c.can_edit;
    const button=S.root.querySelector('[data-as-action="start"]');button.disabled=!can;button.textContent=S.busy?'요청 처리 중':'새 현황분석 생성';
    S.root.querySelector('#as-message').innerHTML=S.error?'<p class="as-error">'+E(S.error)+'</p>':'';
    const events=latest.progress?.units||[];
    const pct=latest.status==='generated_candidate'?100:Math.min(100,Math.max(0,Number(latest.progress?.overall_percent)||0));
    const label=latest.status==='generated_candidate'?quality(latest):(statusLabels[latest.status]||'현재 입력 확인');
    const order=['목표·요구 해석','영역별 작성·검토','종합·개선·요약','PPT·현황 데이터','실제 렌더','장표별 시각 검토','최종 원장 대조'];
    let units='';
    const areaUnits=Object.entries(c.area_names||{});
    if(events.some(v=>v.unit==='area-shared-unresolved'))areaUnits.push(['shared-unresolved','공통 미확인 항목']);
    areaUnits.forEach(([id,name])=>{
      const entries=events.filter(v=>v.unit==='area-'+id).sort((a,b)=>String(a.updated_at||'').localeCompare(String(b.updated_at||'')));
      const last=entries[entries.length-1];let text='대기';
      if(last){const phase=phaseLabels[last.phase]||last.phase;const end={completed:'완료',reused:'기존 검토 재사용',running:'진행 중',required:'필요',invalidated:'재검토 대기',blocked:'차단',failed:'실패'}[last.status]||last.status;text=phase+' '+end;}
      units+='<div class="as-unit"><span>'+E(name)+'</span><span>'+E(text)+'</span></div>';
    });
    const last=events.filter(v=>v.phase!=='tasks')[events.filter(v=>v.phase!=='tasks').length-1];
    const runningTasks=latest.progress?.running_tasks||[];
    const active=runningTasks.length?runningTasks.map(v=>v.title).join(' · '):last?(unitLabels[last.unit]||c.area_names?.[last.unit.replace(/^area-/,'')]||last.unit)+' · '+(last.phase==='interpret'&&last.unit!=='interpret'?'범위 구성':phaseLabels[last.phase]||last.phase):'';
    const taskCounts=Number.isInteger(latest.progress?.total_tasks)?'<p class="as-muted">검증 완료 '+E(latest.progress.completed_tasks||0)+' / '+E(latest.progress.total_tasks)+'개 작업</p>':'';
    const waiting=latest.progress?.waiting_tasks||[];
    const holds=latest.progress?.blocked_tasks||[];
    const waitingText=waiting.length?'<p class="as-muted">선행 작업 대기 '+E(waiting.length)+(holds.length?' · 확인 필요 '+E(holds.length):'')+'</p>':holds.length?'<p class="as-muted">확인 필요 '+E(holds.length)+'</p>':'';
    const pages=latest.progress?.total_pages?'<p class="as-muted">장표 시각 검토 '+E(latest.progress.reviewed_pages||0)+' / '+E(latest.progress.total_pages)+'장</p>':'';
    const stageHtml=stageBlock(latest);
    S.root.querySelector('#as-progress').innerHTML=(stageHtml?'<p class="as-muted">'+E(label)+'</p>'+stageHtml:'<ol class="as-order" aria-label="현황분석 전체 진행 순서">'+order.map(v=>'<li>'+E(v)+'</li>').join('')+'</ol><p class="as-live">'+E(label)+(latest.status?' · '+pct+'%':'')+'</p><div class="as-track" role="progressbar" aria-label="완료 단계 기준 진행률" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+pct+'"><div class="as-fill" style="width:'+pct+'%"></div></div>')+(active&&running?'<p class="as-muted">'+E(active)+'</p>':'')+taskCounts+waitingText+pages+'<div class="as-units">'+units+'</div>'+(latest.error?'<p class="as-error">'+E(latest.error)+'<br>직전 후보·입력·검토 기록은 보존됩니다.</p>':'')+'<p class="as-muted">'+(stageHtml?'단계별 진행률은 그 단계의 검증된 세부 작업 완료 기준이며, 단계 번호는 소요 시간의 비율이 아닙니다.':'진행률은 검증된 세부 작업의 완료 기준이며 예상 처리 시간의 비율은 아닙니다.')+'</p>';
    S.root.querySelector('#as-source').innerHTML=(c.runtime_ready===false?'<p class="as-muted">운영 실행 환경 검증 중입니다. 검증 완료 전에는 새 생성을 시작하지 않습니다.</p>':'')+'<details><summary class="as-muted">입력·개정 확인</summary><p class="as-muted">입력 SHA-256 <code>'+E(c.input_sha256||'미확인')+'</code><br>직전 후보 '+E(candidate.revision_id||'없음')+'</p></details>';
    const rev=candidate.revision_id;
    S.root.querySelector('#as-result').innerHTML=rev?'<p class="as-live">'+E(quality(candidate))+'</p><p class="as-muted">'+(c.candidate_matches_current_input?'현재 수집 입력과 일치':'이전 입력 개정 — 현재 수집과 다름')+'</p><div class="as-result-links"><button class="sec" data-as-action="preview">실제 PPT 렌더 보기</button><a href="'+file(S.aid,rev,'pptx',true)+'">현황분석 PPT</a><a href="'+file(S.aid,rev,'xlsx',true)+'">현황 데이터 XLSX</a><a href="'+file(S.aid,rev,'pdf',true)+'">렌더 PDF</a></div><p class="as-muted">자동 내용·시각 검토와 고객 제출 승인은 별개입니다. 수집 시점 이후의 운영 현재성은 별도 확인이 필요합니다.</p>':'<p class="as-muted">등록된 분석 후보가 없습니다. 정적 샘플이나 이전 방식의 산출물로 대체하지 않습니다.</p>';
    clearTimeout(S.timer);S.timer=null;
    if(latest.revision_id&&(running||(S.chat&&(S.chat.open.length||S.chat.messages.length)&&!S.chat.closed)))chatPoll(S.aid,latest.revision_id);else drawChat();
    if(running)S.timer=setTimeout(()=>{if(S.root?.isConnected)mount(S.aid);},7000);
  }
  async function mount(aid) {
    clearTimeout(S.timer);S.timer=null;
    if(S.aid!==aid){S.aid=aid;S.current=null;S.draft='';S.error='';S.busy=false;S.shell=null;}
    S.root=document.getElementById('asis-root');if(!S.root)return;
    const seq=++S.seq;draw();
    try{const data=await request(base(aid));if(seq!==S.seq||S.aid!==aid)return;S.current=data;S.error='';}
    catch(e){if(seq!==S.seq||S.aid!==aid)return;S.error=e.message;}
    draw();
  }
  async function start(aid) {
    const c=S.current;if(S.busy||S.aid!==aid||!c?.can_edit||!c.can_generate||['queued','running'].includes(c.latest?.status))return;
    S.busy=true;const body={nonce:crypto.randomUUID(),input_sha256:c.input_sha256,requirements:S.draft,parent_revision_id:c.candidate?.revision_id||null};draw();
    try{await request(base(aid)+'/start',{method:'POST',body:JSON.stringify(body)});if(S.aid===aid)S.error='';}
    catch(e){if(S.aid===aid)S.error=e.message;}
    finally{if(S.aid===aid){S.busy=false;await mount(aid);}}
  }
  function preview(aid,rev) {
    if(!rev)return;document.getElementById('asis-preview-dialog')?.remove();
    const dialog=document.createElement('dialog');dialog.id='asis-preview-dialog';dialog.style.cssText='width:95vw;height:94vh;max-width:1600px;padding:12px;border:1px solid #d4d7dc';
    const heading=document.createElement('div');heading.className='row';
    const label=document.createElement('b');label.textContent='같은 개정의 실제 PPT 렌더';
    const close=document.createElement('button');close.className='sec';close.textContent='닫기';close.style.marginLeft='auto';heading.append(label,close);dialog.append(heading);
    const iframe=document.createElement('iframe');iframe.title='현황분석 PPT 실제 PDF 렌더';iframe.src=file(aid,rev,'pdf',false);iframe.style.cssText='width:100%;height:calc(100% - 52px);border:0;margin-top:8px';dialog.append(iframe);
    const y=window.scrollY;close.onclick=()=>dialog.close();dialog.onclose=()=>{dialog.remove();window.scrollTo(0,y);};document.body.append(dialog);dialog.showModal();
  }
  window.AsisUI={mount,start,preview,state:S,unmount:()=>{S.seq++;clearTimeout(S.timer);S.timer=null;S.root=null;S.shell=null;}};
}());
