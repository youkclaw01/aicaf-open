"""세 보고서에 추가된 실제 입력 파일과 누락 차단을 운영 지문에서 검증한다."""
from pathlib import Path
import pytest
import asis_runtime
from _sealed_app import copy_implementation_tree

APP = Path(asis_runtime.__file__).resolve().parent
CRITICAL = ('assessment_workflow.py', 'assessment_flow_view.py',
            'assessment_flow_artifacts.py', 'assessment_flow_interview.py',
            'static/assessment-workflow.js', 'static/index.html',
            'server.py', 'questions.py', 'scoring.py', 'interview_report.py', 'interview_template.pptx')


def test_every_three_report_dependency_is_in_the_actual_seal():
    actual = {p.relative_to(APP).as_posix() for p in asis_runtime.implementation_files(APP)}
    assert set(CRITICAL) <= actual
    assert len(actual) == len(asis_runtime.implementation_files(APP))


@pytest.mark.parametrize('name', CRITICAL)
def test_every_changed_dependency_invalidates_its_actual_seal(tmp_path, monkeypatch, name):
    copy_implementation_tree(APP, tmp_path)
    monkeypatch.setattr(asis_runtime, '__file__', str(tmp_path/'asis_runtime.py'))
    before = asis_runtime.implementation_fingerprint()
    assert before is not None
    target = tmp_path/name
    target.write_bytes(target.read_bytes()+b'\nunit-seal-mutation\n')
    assert asis_runtime.implementation_fingerprint() != before


def test_a_missing_required_file_is_not_accepted_as_a_smaller_valid_build(tmp_path, monkeypatch):
    copy_implementation_tree(APP, tmp_path)
    monkeypatch.setattr(asis_runtime, '__file__', str(tmp_path/'asis_runtime.py'))
    assert asis_runtime.implementation_fingerprint() is not None
    target = tmp_path/'static/assessment-workflow.js'
    target.rename(tmp_path/'static/assessment-workflow.unavailable.js')
    assert asis_runtime.implementation_fingerprint() is None
