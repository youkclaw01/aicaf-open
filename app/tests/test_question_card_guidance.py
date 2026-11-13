"""실제 문항 카드 실행 결과에 공식 지침과 현황 파생 맥락이 포함되는지 검증한다."""
import json
from pathlib import Path
import subprocess

APP=Path(__file__).resolve().parents[1]

def test_real_question_card_renders_derived_guidance_and_escapes_source_text():
    html=(APP/'static/index.html').read_text(encoding='utf-8')
    script=html.split('<script>')[-1].split('</script>')[0]
    startup="boot().catch(e=>$('#app').textContent='오류: '+e.message);"
    assert script.count(startup)==1
    script=script.replace(startup,'')
    question={'id':'WF_UNIT_QUESTION','origin_id':'UNIT-1','question':'명시적인 단위시험 질문','point':'시험 확인 기준',
        'top':'시험 상태','evidence':'시험 근거','area':'B','must':False,'terms':[],
        'interviewer_note':'현재 환경의 실제 호출 경로를 확인한다.','followups':['가상의 후속 확인 질문'],
        'guidelines':[{'id':'UNIT-GUIDE','guidance_ko':'실제 기준 설명을 카드에 전달한다.',
            'quote':'<img src=x onerror=unit_test_only>','source_url':'https://learn.microsoft.com/unit-test-only'}],
        'related_analysis':[{'headline':'단위시험 현황 판정'}],'derivation_reason':'현재 조회본 근거와 업무상 사용의 공백',
        'evaluation_use':'관측 사실과 시험 진술을 구분한다.'}
    checks='''
STATE={answers:{},consent:null,voice:{}};ME={roles:['aicaf.pm']};
canDo=()=>true;basisHtml=()=>'';suppHtml=()=>'';
const output=qcard(97,UNIT_QUESTION);
if(!output.includes('Microsoft 공식 원문'))throw new Error('derived_guidance_missing_from_actual_question_card');
if(!output.includes('단위시험 현황 판정'))throw new Error('current_state_context_missing_from_actual_question_card');
if(!output.includes('가상의 후속 확인 질문'))throw new Error('followup_missing_from_actual_question_card');
if(output.includes('<img src=x'))throw new Error('unescaped_source_markup');
console.log('actual_question_card_guidance_verified');
'''
    runner='''const fs=require('node:fs'),vm=require('node:vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));
vm.runInNewContext(x.script+'\\n'+x.checks,{UNIT_QUESTION:x.question,console,document:{querySelector:()=>({}),querySelectorAll:()=>[]},window:{},location:{search:'',pathname:'/interview/97'},URLSearchParams,URL,setTimeout,clearTimeout});'''
    result=subprocess.run(['node','-e',runner],input=json.dumps({'script':script,'checks':checks,'question':question}),capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'actual_question_card_guidance_verified' in result.stdout
