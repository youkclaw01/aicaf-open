"""실제 포털 함수·초기 상태를 Node에서 실행해 순서와 질문 접근을 검증한다."""
from pathlib import Path
import json
import re
import subprocess

APP = Path(__file__).resolve().parents[1]
HTML = APP/'static/index.html'


def node(code):
    result = subprocess.run(['node','-e',code], capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def function(name, next_name):
    text = HTML.read_text(encoding='utf-8')
    start = text.index('async function '+name+'(') if 'async function '+name+'(' in text else text.index('function '+name+'(')
    end = text.index('function '+next_name+'(', start+1)
    if text[end-6:end] == 'async ':end -= 6
    return text[start:end]


def test_real_initial_portal_tab_is_collection_and_current_state():
    text = HTML.read_text(encoding='utf-8')
    line = next(line for line in text.splitlines() if line.startswith('let Q=[]'))
    actual = node(line+';console.log(JSON.stringify(TAB))')
    assert actual == 'infra'


def test_actual_load_does_not_request_interview_questions_before_as_is():
    code = function('load', 'interview')
    prefix = """let Q=[],STATE=null,INFRA=null,curArea='A',curTopic=null;let calls=[];
async function api(url){calls.push(url);if(url==='/api/aicaf/assessments/97')return {assessment:{id:97},my_role:'viewer',workflow:{questionnaire_ready:false}};return []}
"""
    result = node(prefix+code+";load(97).then(()=>console.log(JSON.stringify(calls)))")
    assert not any('/questions?' in call for call in result)


def test_official_guideline_and_derivation_are_visible_and_escaped():
    q = {'guidelines':[{'id':'WAF-AI-SE3','quote':'<script>bad</script>',
         'source_url':'https://learn.microsoft.com/en-us/azure/well-architected/ai/design-principles',
         'guidance_ko':'실제 신원과 접근권한 확인'}], 'derivation_reason':'실제 구성과 사용 신원의 연결',
         'fact_refs':['F_T_unit'], 'related_analysis':[{'area_id':'secgov','headline':'사전 분석 원문'}]}
    code = function('qguide','qcard')
    prefix = "const esc=s=>(s??'').toString().replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));"
    result = node(prefix+code+';console.log(JSON.stringify(qguide('+json.dumps(q,ensure_ascii=False)+')))')
    assert '공식 지침' in result
    assert 'WAF-AI-SE3' in result
    assert '&lt;script&gt;bad&lt;/script&gt;' in result
    assert '<script>bad' not in result
    assert q['derivation_reason'] in result and '사전 분석 원문' in result


def test_workflow_client_uses_server_prerequisites_not_tab_order_only():
    module = APP/'static/assessment-workflow.js'
    assert module.is_file()
    values = node('const f=require('+json.dumps(str(module))+');'+"""
const a={questionnaire_ready:false,reports:[{kind:'interview',ready:false}]};
const b={questionnaire_ready:true,reports:[{kind:'interview',ready:false}]};
const c={questionnaire_ready:true,reports:[{kind:'interview',ready:true}]};
console.log(JSON.stringify([f.canOpen(a,'infra'),f.canOpen(a,'interview'),f.canOpen(b,'interview'),f.canOpen(b,'integrated'),f.canOpen(c,'integrated')]));
""")
    assert values == [True,False,True,False,True]


def test_portal_tabs_expose_five_step_order_with_summary_report():
    code = function('tabs', 'workflowBanner')
    prefix = """const TAB='infra';const STATE={workflow:{questionnaire_ready:true,reports:[{kind:'interview',ready:true}]}};
const window={AssessmentFlow:{canOpen:()=>true}};"""
    html = node(prefix+code+';console.log(JSON.stringify(tabs(26)))')
    labels = re.findall(r'>([^<]+)</button>', html)
    assert labels == ['1. 현황 분석', '2. 인터뷰', '3. 아키텍처', '4. 비용 분석', '5. Summary Report', '프로젝트·멤버']
    assert '참고 도구' not in html

