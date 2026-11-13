"""진행 표시 연결 계약: 서버가 개정 진행 기록에 18단계 값을 싣고, 화면이 그 값을 그대로 보여 준다.

- record_progress 가 작업 원장 진행 이벤트를 받을 때마다 progress.stage 를 asis_stages.compute 로 갱신한다.
- 단일 전체 %(overall_percent)는 단계 안에서 내려갈 수 있어 더는 주 표시가 아니다. 화면은 stage.label 을 쓴다.
- 이전 개정(progress.stage 없음)은 화면이 옛 표시로 안전하게 되돌아간다.
"""
from pathlib import Path
import json
import re
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import asis_view


def make(tmp_path):
    controller = asis_view.Controller(ROOT=tmp_path, assessment=None, infra_case_dir=None, db=None)
    aid, revision = 7, 'r-20261017T003438-6633c2dd81122918'
    folder = controller.job_dir(aid, revision)
    folder.mkdir(parents=True, exist_ok=True)
    (folder.parent.parent / 'state.json').write_text('{}', encoding='utf-8')
    (folder / 'job.json').write_text(json.dumps({'status': 'running', 'revision_id': revision, 'steps': [], 'run': {'assessment_id': aid}}), encoding='utf-8')
    return controller, aid, revision, folder


def tasks_event(rows):
    total = sum(r['weight'] for r in rows.values())
    done = sum(r['weight'] for r in rows.values() if r['status'] == 'completed')
    return {'unit': 'tasks', 'phase': 'tasks', 'status': 'updated',
            'task_progress': {'schema': 'asis-task-progress/1', 'overall_percent': round(100 * done / total, 2), 'completed_tasks': 0, 'total_tasks': len(rows),
                              'running_tasks': [], 'waiting_tasks': [], 'blocked_tasks': [], 'plan_version': 1, 'superseded_tasks': 0,
                              'stage_ledger': {k: {'status': v['status'], 'weight': v['weight']} for k, v in rows.items()}}}


def test_record_progress_writes_the_stage_label_from_the_ledger(tmp_path):
    controller, aid, revision, folder = make(tmp_path)
    rows = {'interpret': {'status': 'completed', 'weight': 1.0}, 'issue-plan': {'status': 'completed', 'weight': 1.0},
            'issue-plan-area-a': {'status': 'completed', 'weight': 1.0}, 'area-a-O_P0001': {'status': 'completed', 'weight': 1.0},
            'area-a-O_P0002': {'status': 'running', 'weight': 1.0}}
    controller.record_progress(aid, revision, tasks_event(rows))
    progress = json.loads((folder / 'job.json').read_text(encoding='utf-8'))['progress']
    stage = progress['stage']
    assert stage['label'] == '4단계 - 50% 진행 중 / 총 18단계' and stage['current'] == 4 and stage['total'] == 18
    assert len(stage['stages']) == 18 and stage['stages'][3]['state'] == 'running'


def test_stage_label_does_not_drop_when_a_later_stage_registers_more_units(tmp_path):
    """단일 전체 %는 단위가 늘면 내려갔다(운영 실측 94.97%에서 하락 예정). 단계 번호는 앞 단계가 끝나기 전에는 뒤로 가지 않는다."""
    controller, aid, revision, folder = make(tmp_path)
    rows = {'interpret': {'status': 'completed', 'weight': 1.0}, 'area-a-O_P0001': {'status': 'completed', 'weight': 1.0}, 'area-a-O_P0002': {'status': 'running', 'weight': 1.0}}
    controller.record_progress(aid, revision, tasks_event(rows))
    first = json.loads((folder / 'job.json').read_text(encoding='utf-8'))['progress']['stage']['current']
    rows['direction-D_P0001'] = {'status': 'pending', 'weight': 1.0}
    rows['direction-D_P0002'] = {'status': 'pending', 'weight': 1.0}
    controller.record_progress(aid, revision, tasks_event(rows))
    second = json.loads((folder / 'job.json').read_text(encoding='utf-8'))['progress']['stage']['current']
    assert second == first == 4, '앞 단계(영역 작성·검토)가 끝나기 전에 뒤 단계 작업이 등록돼도 현재 단계는 뒤로 가지 않는다'


def test_task_progress_event_carries_the_ledger_for_the_stage_calculation():
    import asis_tasks
    source = Path(asis_tasks.__file__).read_text(encoding='utf-8')
    assert "'stage_ledger'" in source, '진행 이벤트가 원장의 작업 상태·가중치를 실어야 서버가 단계를 계산한다'


def _node(script):
    import shutil
    import subprocess
    node = shutil.which('node')
    if node is None:
        import pytest
        pytest.skip('node 가 없어 화면 렌더 시험을 건너뛴다')
    out = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-600:]
    return out.stdout


def _render_script(latest_json):
    js_path = (APP / 'static' / 'asis.js').as_posix()
    return """
const fs=require('fs');const src=fs.readFileSync('%s','utf8');
const i=src.indexOf('function stageBlock');const k=src.indexOf('async function request');
const E=v=>String(v).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const stageBlock=new Function('E',src.slice(i,k)+'return stageBlock;')(E);
const latest=%s;
// 화면이 stageBlock 결과를 진행 영역에 쓰는 연결도 소스에서 그대로 확인한다: 연결 줄을 실제 값으로 평가한다.
const j=src.indexOf('const stageHtml=');const e=src.indexOf(';',j);
const wired=new Function('stageBlock','latest',src.slice(j,e+1)+'return stageHtml;')(stageBlock,latest);
console.log(JSON.stringify({block:stageBlock(latest),wired:wired}));
""" % (js_path, latest_json)


def _stage(current=4, percent=50, reopened=False):
    import asis_stages as S
    rows = {'interpret': {'status': 'completed', 'weight': 1.0}, 'issue-plan': {'status': 'completed', 'weight': 1.0},
            'issue-plan-area-a': {'status': 'completed', 'weight': 1.0}, 'area-a-O_P0001': {'status': 'completed', 'weight': 1.0},
            'area-a-O_P0002': {'status': 'running', 'weight': 1.0}}
    return S.compute(rows, job_status='running')


def test_progress_view_renders_the_label_and_all_eighteen_stages_from_the_server_value():
    stage = _stage()
    out = json.loads(_node(_render_script(json.dumps({'status': 'running', 'progress': {'stage': stage}}, ensure_ascii=False))))
    html = out['block']
    assert '4단계 - 50% 진행 중 / 총 18단계' in html
    assert html.count('class="as-st ') == 18, '18단계 모두 그려야 한다'
    assert 'as-st-now' in html and html.count('as-st-now') == 1, '현재 단계 하나만 강조'
    assert 'aria-valuenow="50"' in html


def test_progress_view_is_wired_into_the_progress_area():
    stage = _stage()
    out = json.loads(_node(_render_script(json.dumps({'status': 'running', 'progress': {'stage': stage}}, ensure_ascii=False))))
    assert out['wired'] is not None and '4단계 - 50% 진행 중 / 총 18단계' in out['wired'], '진행 영역이 단계 표시를 써야 한다'


def test_progress_area_assignment_uses_the_stage_html_and_keeps_the_old_display_as_fallback():
    """const stageHtml 만 만들고 진행 영역에 쓰지 않으면 화면이 바뀌지 않는다. innerHTML 대입식을 그대로 평가해 실제 결과를 본다."""
    script = """
const fs=require('fs');const src=fs.readFileSync('%s','utf8');
const j=src.indexOf('const stageHtml=');
const start=src.indexOf("S.root.querySelector('#as-progress').innerHTML=",j);
const end=src.indexOf(";\\n",start);
const stmt=src.slice(start,end+1).replace("S.root.querySelector('#as-progress').innerHTML=","return ");
const E=v=>String(v);
const mk=(latest,stageHtml)=>new Function('E','stageHtml','latest','label','order','pct','active','running','taskCounts','waitingText','pages','units',stmt)(E,stageHtml,latest,'작성·검토 진행 중',['a','b'],40,'',false,'','','','');
const withStage=mk({status:'running'},'<STAGE-HTML>');
const without=mk({status:'running'},null);
console.log(JSON.stringify({withStage,without}));
""" % (APP / 'static' / 'asis.js').as_posix()
    out = json.loads(_node(script))
    assert '<STAGE-HTML>' in out['withStage'] and 'class="as-order"' not in out['withStage'], '단계 값이 있으면 18단계 표시가 진행 영역에 들어가야 하고 옛 순서 표시는 빠져야 한다'
    assert '<ol class="as-order" aria-label="현황분석 전체 진행 순서">' in out['without'] and '40%' in out['without'], '단계 값이 없으면 옛 표시(순서 목록·전체 %)로 되돌아가야 한다'
    assert 'role="progressbar"' in out['without'] and 'aria-valuenow="40"' in out['without']


def test_old_revisions_without_a_stage_value_fall_back_to_the_previous_display():
    out = json.loads(_node(_render_script(json.dumps({'status': 'running', 'progress': {'overall_percent': 40}}))))
    assert out['block'] is None and out['wired'] is None, '단계 값이 없는 이전 개정은 옛 표시를 쓴다'


def test_completed_revision_shows_all_stages_done_and_full_bar():
    import asis_stages as S
    stage = S.compute({'interpret': {'status': 'completed', 'weight': 1.0}}, job_status='generated_candidate')
    out = json.loads(_node(_render_script(json.dumps({'status': 'generated_candidate', 'progress': {'stage': stage}}, ensure_ascii=False))))
    assert '완료 - 총 18단계' in out['block'] and 'aria-valuenow="100"' in out['block'] and 'as-st-now' not in out['block']


def test_stage_names_are_escaped_in_the_view():
    stage = _stage()
    stage['stages'][0]['name'] = '<img src=x onerror=alert(1)>'
    out = json.loads(_node(_render_script(json.dumps({'status': 'running', 'progress': {'stage': stage}}, ensure_ascii=False))))
    assert '<img' not in out['block'] and '&lt;img' in out['block']
