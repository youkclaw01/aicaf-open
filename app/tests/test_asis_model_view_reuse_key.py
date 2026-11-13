"""새 전송 계층이 이미 완료된 작업의 재사용 키를 바꾸지 않는지: 재사용 키 계산은 정본 입력과 model_view=False 렌더만 쓴다.
운영에서 재개 후 완료 작업이 20개로 떨어진 적이 두 번 있었다(코드 해시를 키에 넣었기 때문)."""
from copy import deepcopy
from pathlib import Path
import json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_model_view as mv
from asis_pipeline import Pipeline, STAGE_REUSE_CODE_BINDING
from asis_model import digest
from asis_runtime import policy
from test_asis_model_view import heavy_data, first_slot
from asis_synthesis import direction_input
from test_asis_parallel_synthesis import DirectionRuntime


def reuse_key(pipe, scoped, stage):
    return digest({'input': scoped, 'stage': stage, 'code_binding': STAGE_REUSE_CODE_BINDING, 'model_policy': policy(),
                   'prompts': pipe.render_prompt(stage, scoped, model_view=False)})


def test_the_reuse_key_does_not_depend_on_the_model_view_so_finished_work_stays_reusable(tmp_path, monkeypatch):
    data, plan = heavy_data(tmp_path)
    scoped = direction_input(data, first_slot(plan, data))
    pipe = Pipeline(DirectionRuntime(plan), tmp_path / 'run')
    before = reuse_key(pipe, scoped, 2)
    # monkeypatch는 시험이 끝나면 원래 값으로 되돌린다. 모듈 전역을 직접 바꾸면 같은 프로세스의 뒤 시험이 오염된다.
    monkeypatch.setitem(mv.LIMITS, 'direction_author', 1)
    monkeypatch.setattr(mv, 'DROP_CONTEXT', ())
    monkeypatch.setattr(mv, 'LARGE_FACT_CHARS', 1)
    assert reuse_key(pipe, scoped, 2) == before, 'changing what the model is sent must never change the key of finished work'


def test_the_key_is_identical_to_the_one_computed_with_the_old_transport_by_the_pipeline(tmp_path):
    """파이프라인이 작업 재사용 키를 계산하는 식과 같은 식이다(이 식이 어긋나면 완료 작업이 전부 다시 쓰인다)."""
    import inspect
    from asis_pipeline import Pipeline as P
    source = inspect.getsource(P._reviewed_stage)
    assert "self.render_prompt(stage, data, model_view=False)" in source
    import asis_resume
    assert "pipe.render_prompt(stage,data,model_view=False)" in inspect.getsource(asis_resume.ResumeContext.stage)
