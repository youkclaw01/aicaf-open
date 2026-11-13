"""세부 작업 완료·현재 작업 이름을 실제 UI 코드가 표시하는 Node DOM 계약 시험."""
from pathlib import Path
import subprocess


def test_portal_displays_verified_task_counts_and_running_titles(tmp_path):
    source=Path(__file__).resolve().parents[1]/'static/asis.js'
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={};
const node=id=>nodes[id]||(nodes[id]={id,value:'',innerHTML:'',disabled:false,textContent:''});
const root={isConnected:true,innerHTML:'',querySelector:selector=>node(selector),contains:()=>true};
const document={getElementById:id=>id==='asis-root'?root:id==='asis-component-style'?{}:null};
const payload={can_edit:true,can_generate:true,area_names:{},latest:{status:'running',progress:{
 overall_percent:37.5,completed_tasks:3,total_tasks:8,progress_basis:'verified_central_tasks',
 running_tasks:[{id:'a',title:'네트워크 작성·검수'},{id:'b',title:'비용 근거 대조'}],units:[]}}};
const context={document,window:{},fetch:async()=>({ok:true,text:async()=>JSON.stringify(payload)}),
 setTimeout:()=>1,clearTimeout:()=>{},console};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
context.window.AsisUI.mount(123).then(()=>{
 const html=node('#as-progress').innerHTML;
 assert(html.includes('3 / 8'),'verified completion counts missing');
 assert(html.includes('네트워크 작성·검수')&&html.includes('비용 근거 대조'),'active task names missing');
 assert(html.includes('37.5%'),'authoritative progress missing');
 assert(!html.includes('tasks · tasks'),'internal task event leaked');
 console.log('task-progress UI contract passed');
}).catch(error=>{console.error(error.message);process.exitCode=1;});
'''
    path=tmp_path/'verify.js';path.write_text(script,encoding='utf-8')
    run=subprocess.run(['node',str(path),str(source)],capture_output=True,encoding='utf-8',timeout=15)
    assert run.returncode==0,run.stdout+run.stderr
