"""새 종합 소비 모듈의 변경은 기존 운영 준비 증거를 재사용할 수 없다."""
from pathlib import Path
import shutil
import importlib.util
import sys

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))


def test_synthesis_module_is_sealed_by_production_implementation_fingerprint(tmp_path):
    from _sealed_app import copy_implementation_tree
    copy_implementation_tree(APP,tmp_path)
    spec=importlib.util.spec_from_file_location('sealed_test_runtime',tmp_path/'asis_runtime.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    before=module.implementation_fingerprint()
    assert before is not None
    source=tmp_path/'asis_synthesis.py'
    source.write_bytes(source.read_bytes()+b'\n# synthetic sealing mutation\n')
    assert module.implementation_fingerprint()!=before, 'new synthesis module excluded from readiness seal'


def test_failed_scope_resume_module_is_sealed_by_production_implementation_fingerprint(tmp_path):
    """실패 범위 재개 구현의 변경은 이전 코드 검증 영수증을 무효화해야 한다."""
    from _sealed_app import copy_implementation_tree
    copy_implementation_tree(APP,tmp_path)
    spec=importlib.util.spec_from_file_location('sealed_resume_test_runtime',tmp_path/'asis_runtime.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    before=module.implementation_fingerprint()
    assert before is not None
    source=tmp_path/'asis_resume.py'
    source.write_bytes(source.read_bytes()+'\n# 재개 코드 봉인 검증 변경\n'.encode('utf-8'))
    assert module.implementation_fingerprint()!=before


def test_synthesis_reuse_module_is_sealed_by_production_implementation_fingerprint(tmp_path):
    """계획 재사용의 코드·원본 바인딩을 바꾸면 이전 준비 증거를 쓸 수 없다."""
    from _sealed_app import copy_implementation_tree
    copy_implementation_tree(APP, tmp_path)
    source = tmp_path / 'asis_synthesis_reuse.py'
    assert source.is_file(), 'planner reuse implementation excluded from the production seal'
    spec = importlib.util.spec_from_file_location('sealed_plan_reuse_runtime', tmp_path / 'asis_runtime.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = module.implementation_fingerprint()
    assert before is not None
    source.write_bytes(source.read_bytes() + '\n# 계획 재사용 봉인 검증\n'.encode('utf-8'))
    assert module.implementation_fingerprint() != before
