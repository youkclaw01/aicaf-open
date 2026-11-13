"""비용 v2 저장·실행 격리 단위시험. tmp_path의 명시적 가상 데이터만 사용한다."""
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_controller(tmp):
    spec = importlib.util.find_spec('cost_finops_view')
    assert spec is not None, 'v2 프로젝트별 저장·고정 실행 컨트롤러가 아직 없다'
    cls = getattr(importlib.import_module('cost_finops_view'), 'Controller', None)
    assert callable(cls), '프로젝트별 격리 컨트롤러가 필요하다'
    root = tmp/'data'
    root.mkdir()
    case = tmp/'cases'/'unit-fixture'
    (case/'Source').mkdir(parents=True)
    dbfile = root/'unit.sqlite3'
    c = sqlite3.connect(dbfile)
    c.execute('create table answers(assessment_id int,qid text,score int,summary text,transcript text)')
    c.commit();c.close()
    ev = {'schemaVersion':'unit-test-fixture', 'dataClassification':'test_fixture', 'costPeriod':{'fromUtc':'2026-09-01','toUtc':'2026-09-30'}, 'sections':{
        'costByResource':{'status':'collected','ok':True,'records':[{'Cost':100,'Currency':'USD','ResourceId':'/subscriptions/unit-fixture/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/unit-fixture'}]},
        'costMonthlyTotals':{'status':'collected','ok':True,'records':[{'Cost':100,'Currency':'USD','costType':'ActualCost','month':'2026-09'}]}}}
    (case/'Source'/'aicaf-evidence.json').write_text(json.dumps(ev),encoding='utf-8')
    (case/'Source'/'topology.json').write_text(json.dumps({'resources':[]}),encoding='utf-8')
    def db():
        c = sqlite3.connect(dbfile)
        c.row_factory = sqlite3.Row
        return c
    deps = {'ROOT':root, 'db':db, 'assessment':lambda aid:{'id':aid,'customer':'단위시험(예시·가상)','track':'existing','infra_case_id':'unit-fixture'},
            'infra_case_dir':lambda cid:case, 'questions':lambda track:[], 'audit':lambda *args:None}
    return cls(deps),case,ev


def test_save_preserves_source_and_rejects_stale_revision(tmp_path):
    ctrl,case,ev=make_controller(tmp_path)
    before=(case/'Source'/'aicaf-evidence.json').read_bytes()
    r=ctrl.result(501)
    assert r['summary']['total']['value']==100
    assert r['revision']==0
    new=ctrl.save(501,{'analysis_id':r['analysis_id'],'revision':0,'options':{'dataset':'monthly','currency':'USD'}})
    assert new['revision']==1
    assert new['analysis_id']!=r['analysis_id']
    assert (case/'Source'/'aicaf-evidence.json').read_bytes()==before
    with pytest.raises(web.HTTPConflict):
        ctrl.save(501,{'analysis_id':r['analysis_id'],'revision':0,'options':{}})


def test_build_freezes_analysis_and_old_result_is_not_new_version(tmp_path):
    ctrl,case,ev=make_controller(tmp_path)
    r=ctrl.result(501)
    j=ctrl.create_job(501,r['analysis_id'],'unit-fixture',start_worker=False)
    frozen=ctrl.snapshot(501,j['job_id'])
    ev['sections']['costByResource']['records'][0]['Cost']=999
    (case/'Source'/'aicaf-evidence.json').write_text(json.dumps(ev),encoding='utf-8')
    assert frozen['summary']['total']['value']==100
    assert ctrl.snapshot(501,j['job_id'])==frozen
    assert ctrl.result(501)['analysis_id']!=j['analysis_id']
    with pytest.raises(web.HTTPConflict):
        ctrl.create_job(501,r['analysis_id'],'unit-fixture',start_worker=False)


def test_interrupted_state_is_computed_without_get_writing(tmp_path):
    ctrl,case,ev=make_controller(tmp_path)
    j=ctrl.create_job(501,ctrl.result(501)['analysis_id'],'unit-fixture',start_worker=False)
    state_path=ctrl.job_dir(501,j['job_id'])/'job.json'
    state=json.loads(state_path.read_text(encoding='utf-8'))
    state['instance']='previous-instance'
    state['pid']=0
    state_path.write_text(json.dumps(state),encoding='utf-8')
    before=state_path.read_bytes()
    assert ctrl.job(501,j['job_id'])['status']=='interrupted'
    assert state_path.read_bytes()==before


def test_corrupt_source_reports_parse_failed_without_zero(tmp_path):
    ctrl,case,ev=make_controller(tmp_path)
    (case/'Source'/'aicaf-evidence.json').write_text('{invalid',encoding='utf-8')
    r=ctrl.result(501)
    assert r['source_status']['evidence']['status']=='parse_failed'
    assert r['summary']['total']['value'] is None
    assert r['summary']['known_subtotal']['value'] is None


def test_save_rejects_source_change_after_viewed_input(tmp_path,monkeypatch):
    ctrl,case,ev=make_controller(tmp_path)
    seen=ctrl.result(501)
    original=ctrl.sources
    first=True
    def changing(a):
        nonlocal first
        vector=original(a)
        if first:
            first=False
            changed=json.loads(json.dumps(ev))
            changed['sections']['costByResource']['records'][0]['Cost']=999
            (case/'Source'/'aicaf-evidence.json').write_text(json.dumps(changed),encoding='utf-8')
        return vector
    monkeypatch.setattr(ctrl,'sources',changing)
    with pytest.raises(web.HTTPConflict):
        ctrl.save(501,{'analysis_id':seen['analysis_id'],'revision':0,'options':seen['options']})
    assert ctrl.config(501)['revision']==0


def test_version_failure_does_not_commit_options(tmp_path,monkeypatch):
    ctrl,case,ev=make_controller(tmp_path)
    module=importlib.import_module('cost_finops_view')
    original=module.atomic
    def failing(path,value):
        if 'versions' in path.parts:
            raise OSError('unit-fixture write failure')
        return original(path,value)
    monkeypatch.setattr(module,'atomic',failing)
    seen=ctrl.result(501)
    with pytest.raises(OSError):
        ctrl.save(501,{'analysis_id':seen['analysis_id'],'revision':0,'options':{'dataset':'monthly'}})
    assert ctrl.config(501)['revision']==0


def test_corrupt_active_is_visible_and_does_not_prevent_rebuild(tmp_path):
    ctrl,case,ev=make_controller(tmp_path)
    result=ctrl.result(501)
    old=ctrl.create_job(501,result['analysis_id'],'unit-fixture',start_worker=False)
    (ctrl.job_dir(501,old['job_id'])/'job.json').write_text('{invalid',encoding='utf-8')
    assert ctrl.history(501)[0]['status']=='corrupt'
    new=ctrl.create_job(501,result['analysis_id'],'unit-fixture',start_worker=False)
    assert new['job_id']!=old['job_id']
    assert (ctrl.job_dir(501,old['job_id'])/'job.json').read_text(encoding='utf-8')=='{invalid'


def test_second_controller_does_not_claim_live_job_interrupted(tmp_path):
    ctrl,case,ev=make_controller(tmp_path)
    other=type(ctrl)(ctrl.deps)
    result=ctrl.result(501)
    old=ctrl.create_job(501,result['analysis_id'],'unit-fixture',start_worker=False)
    assert other.job(501,old['job_id'])['status']=='running'
    with pytest.raises(web.HTTPConflict):
        other.create_job(501,result['analysis_id'],'unit-fixture',start_worker=False)
