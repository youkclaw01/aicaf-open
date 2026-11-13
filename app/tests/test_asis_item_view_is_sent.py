"""진단·과제 항목 호출이 줄인 전송 뷰를 실제로 받는지 고정하는 시험.

배경(평가 24 실측, 2026-10-16): asis_model_view.item_view가 항목 입력을 1.04MB로 줄이는데, render_prompt 251행의 구 투영 분기가
정본 data의 projection_schema가 없다는 이유로 줄인 뷰를 정본 전체(3.58MB)로 덮어써 왔다. 방향 작성에는 같은 종류의 시험이 있었으나
진단·과제 항목에는 없어 이 결함이 잡히지 않았다. 이 시험은 '모델에 가는 프롬프트'를 직접 잰다.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_model_view as mv
from asis_pipeline import Pipeline
from test_asis_model_view import item_scoped
from test_asis_parallel_synthesis import DirectionRuntime


def body(prompt):
    return json.loads(prompt[prompt.index('\n입력:\n') + len('\n입력:\n'):])


def test_the_prompt_for_an_item_author_is_built_from_the_view_not_the_canonical_input(tmp_path):
    data, scoped = item_scoped(tmp_path)
    pipe = Pipeline(DirectionRuntime({'directions': []}), tmp_path / 'run')
    sent = body(pipe.render_prompt(3, scoped)[1])
    assert sent['context'].get('model_view', {}).get('kind') == 'item_author', 'the model must receive the item view marker'
    for key in ('source_documents', 'collection_basis', 'criterion_assessments'):
        assert key not in sent['context'], key + ' is a background ledger the item model does not use'
    assert not any(row['id'].startswith('F_X_') for row in sent['facts']), 'facts nobody cited must not be sent'


def test_the_sent_prompt_is_far_smaller_than_the_canonical_render(tmp_path):
    data, scoped = item_scoped(tmp_path)
    pipe = Pipeline(DirectionRuntime({'directions': []}), tmp_path / 'run')
    old = pipe.render_prompt(3, scoped, model_view=False)[1]
    new = pipe.render_prompt(3, scoped)[1]
    assert len(new) < len(old) * 0.5, 'item prompts must use the smaller view (old %d, new %d)' % (len(old), len(new))


def test_the_reuse_key_render_for_an_item_is_unchanged_by_this_fix(tmp_path):
    """완료 작업 재사용 키는 정본 입력과 model_view=False 렌더만 쓴다. 전송 뷰를 존중하도록 고쳐도 이 렌더는 같아야 한다."""
    data, scoped = item_scoped(tmp_path)
    pipe = Pipeline(DirectionRuntime({'directions': []}), tmp_path / 'run')
    first = pipe.render_prompt(3, scoped, model_view=False)[1]
    assert pipe.render_prompt(3, scoped, model_view=False)[1] == first
    sent = body(first)
    assert 'model_view' not in sent['context'], 'the reuse-key render must stay on the old transport (no view marker)'
    assert any(row['id'].startswith('F_X_') for row in sent['facts']), 'the old transport keeps every fact'


def test_the_summary_projection_stays_as_it_was(tmp_path):
    """요약(4) 구 투영은 이 수정의 대상이 아니다(요약은 자체 뷰가 있고 이미 조건으로 분리돼 있다)."""
    import inspect
    source = inspect.getsource(Pipeline.render_prompt)
    assert 'stage==4' in source.replace(' ', '')


def test_a_view_the_model_view_layer_already_projected_is_not_projected_a_second_time(tmp_path):
    data, scoped = item_scoped(tmp_path)
    kind, view = mv.build(3, scoped)
    pipe = Pipeline(DirectionRuntime({'directions': []}), tmp_path / 'run')
    sent = body(pipe.render_prompt(3, scoped)[1])
    assert sent['facts'] == body(pipe.render_prompt(3, view, model_view=False)[1])['facts'], 'same facts as the layer-built view'
