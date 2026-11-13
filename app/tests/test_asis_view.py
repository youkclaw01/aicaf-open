"""AS-IS 개정 저장소의 실제 생성·중복·실패보존 경계를 격리 경로에서 시험한다."""
from pathlib import Path
import importlib.util
import json
import sqlite3
import sys
import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from private_archive_source import SOURCE,PRIVATE_ARCHIVE_REQUIRED
pytestmark=PRIVATE_ARCHIVE_REQUIRED


def test_duplicate_nonce_and_failed_revision_preserve_prior_native_output(tmp_path):
    assert importlib.util.find_spec('asis_view') is not None, 'ASIS revision controller missing'
    import asis_view
    assessment=json.loads((SOURCE/'interview_E.json').read_text(encoding='utf-8'))['assessment']
    def connection():
        c=sqlite3.connect(tmp_path/'test.sqlite3');c.row_factory=sqlite3.Row
        c.execute('create table if not exists answers(assessment_id integer,qid text,transcript text,summary text,score text)')
        return c
    controller=asis_view.Controller(ROOT=tmp_path,assessment=lambda aid:assessment,
        infra_case_dir=lambda cid:SOURCE.parent,db=connection,source_folder=lambda a:SOURCE,
        test_analyzer=lambda data,**kw:__import__('visual_spec_stub').with_visual_specs(asis_view.asis_analysis.analyze(data,**kw)))
    current=controller.current(assessment['id'])
    body={'nonce':'unit-r1','input_sha256':current['input_sha256'],'requirements':'','parent_revision_id':None}
    first=controller.create(assessment['id'],body)
    again=controller.create(assessment['id'],body)
    assert first['revision_id']==again['revision_id']
    controller.generate(assessment['id'],first['revision_id'])
    prior=controller.current(assessment['id'])['candidate']['revision_id']
    assert prior==first['revision_id']
    second=controller.create(assessment['id'],dict(body,nonce='unit-r2',requirements='확인 범위 추가',parent_revision_id=prior))
    def fail(result,folder):
        raise RuntimeError('unit_failure_preservation_probe')
    controller.builder=fail
    controller.generate(assessment['id'],second['revision_id'])
    state=controller.current(assessment['id'])
    assert state['candidate']['revision_id']==prior
    assert state['latest']['status']=='failed'
    assert controller.job_dir(assessment['id'],prior).joinpath('ASIS-Report.pptx').is_file()
    with pytest.raises(Exception):
        controller.create(assessment['id'],dict(body,requirements='different-content-same-nonce'))
