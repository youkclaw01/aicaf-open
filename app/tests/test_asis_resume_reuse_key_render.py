"""재개 경로의 보존 작업 키 계산이 전송 뷰와 분리되어 있는지 확인한다.

완료 작업의 재사용 키는 작성 시점부터 model_view=False 렌더로 계산된다. 승인 코드(legacy render)로 키를 다시 계산하는 재개 경로도
같은 방식을 써야 한다. 전송 뷰를 존중하도록 render_prompt를 고친 뒤 재개 시험 5건이 binding_changed로 실패한 사고를 고정한다.
"""
import ast
import hashlib
import inspect
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_resume
import asis_pipeline
from asis_model import encoded, strict_json


def legacy_of(source_bytes):
    node = asis_resume._function(source_bytes, 'render_prompt', 'Pipeline')
    namespace = {'deepcopy': deepcopy, 'ASSET': asis_pipeline.ASSET, 'encoded': encoded, 'json': json, 'strict_json': strict_json}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'legacy', 'exec'), namespace)
    return namespace['render_prompt']


def test_the_current_pipeline_render_declares_model_view_so_the_resume_path_can_use_it():
    source = (Path(asis_pipeline.__file__)).read_bytes()
    assert 'model_view' in inspect.signature(legacy_of(source)).parameters


def test_a_legacy_render_without_the_argument_is_still_callable_the_old_way():
    """모델 뷰 계층이 없던 과거 승인 코드는 인자를 받지 않는다. 재개 경로가 그 코드를 인자 없이 호출하는 분기가 필요하다."""
    old_source = b"""
class Pipeline:
    def render_prompt(self, stage, data):
        return 's', 'p'
"""
    legacy = legacy_of(old_source)
    assert 'model_view' not in inspect.signature(legacy).parameters
    assert legacy(None, 3, {}) == ('s', 'p')


def test_resume_context_chooses_the_call_form_from_the_legacy_signature():
    text = Path(asis_resume.__file__).read_text(encoding='utf-8')
    assert 'legacy_render_has_model_view' in text
    assert "model_view' in inspect.signature(self.legacy_render)" in text.replace('"', "'")
    assert 'self.legacy_render(pipe,stage,legacy,model_view=False)' in text
    assert 'self.legacy_render(pipe,stage,legacy)' in text
