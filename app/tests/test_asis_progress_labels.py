"""계획 단계와 자원 대기·후속 의존은 모바일 UI에서 실행과 구분해 표시한다."""
from pathlib import Path
import json
import subprocess


def test_plan_phase_labels_and_dependency_waiting_are_reader_facing(tmp_path):
    source=Path(__file__).resolve().parents[1]/'static/asis.js'
    payload={'can_edit':True,'can_generate':True,'area_names':{},'latest':{'status':'running','progress':{
        'overall_percent':41.67,'completed_tasks':7,'total_tasks':30,'units':[
            {'unit':'synthesis-plan','phase':'interpret','status':'running'}],
        'running_tasks':[{'id':'synthesis-plan','title':'종합 방향별 범위 계획·검토'}],
        'waiting_tasks':[{'id':'action-1','title':'진단 승인 대기','reason':'dependency_wait'}],
        'blocked_tasks':[]}}}
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={};const node=id=>nodes[id]||(nodes[id]={id,value:'',innerHTML:'',disabled:false,textContent:''});
const root={isConnected:true,innerHTML:'',querySelector:selector=>node(selector),contains:()=>true};
const document={getElementById:id=>id==='asis-root'?root:id==='asis-component-style'?{}:null};
const payload=JSON.parse(process.argv[3]);
const context={document,window:{},fetch:async()=>({ok:true,text:async()=>JSON.stringify(payload)}),
    setTimeout:()=>1,clearTimeout:()=>{},console};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
context.window.AsisUI.mount(123).then(()=>{
 const html=node('#as-progress').innerHTML;
 assert(html.includes('7 / 30'),'completion counts missing');
 assert(html.includes('41.67%'),'small verified progress rounded away');
 assert(html.includes('선행 작업 대기 1'),'dependency waiting not distinguished');
 assert(!html.includes('synthesis-plan'),'internal planning identifier shown');
 assert(!html.includes('목표·요구 해석 진행 중'),'wrong generic phase label');
 console.log('progress labels passed');
}).catch(error=>{console.error(error.message);process.exitCode=1;});
'''
    path=tmp_path/'check.js';path.write_text(script,encoding='utf-8')
    result=subprocess.run(['node',str(path),str(source),json.dumps(payload,ensure_ascii=False)],capture_output=True,encoding='utf-8',timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
