"""종합 구간 검토·통합 검토가 줄인 전송 뷰를 실제로 받는지 고정하는 시험.

배경(평가 24 실측, 2026-10-17): asis_model_view.synthesis_shard_view는 구간 검토 입력을 줄이도록 구현돼 있었으나 kind_of가
'synthesis_shard_review'를 반환하지 않아 한 번도 호출되지 않았다. 구간 검토 501건이 정본 3.3~3.8MB(영수증 입력 중앙 786K 토큰)를
그대로 보냈다. 진단·과제 항목은 줄인 뷰가 덮어쓰였지만 종합 구간 검토는 연결 자체가 없었다.

계약:
- 종합 구간 검토(담당 방향이 있고 다른 방향은 omitted)는 담당 방향이 인용한 사실·기준·시사점만 받는다.
- 담당 방향이 인용한 사실·기준·시사점은 하나도 빠지지 않는다(누락 0).
- 통합 검토(전체 방향의 요약본)는 사실·관찰 본문을 받지 않는다. 방향 간 중복·상충·목표 연결만 본다.
- 재사용 키 렌더(model_view=False)는 이전과 같다.
"""
import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_model_view as mv
import asis_synthesis
from asis_pipeline import Pipeline
from test_asis_model_view import heavy_data
from test_asis_parallel_synthesis import DirectionRuntime


def body(prompt):
    return json.loads(prompt[prompt.index('\n입력:\n') + len('\n입력:\n'):])


def merged_for(data, count=60):
    areas = data['stage_data']['approved_areas']['areas']
    facts = [f['id'] for f in data['facts'] if f['id'].startswith('F_')]
    rows = []
    for i in range(count):
        area = areas[i % len(areas)]
        own = [facts[(i * 3 + k) % len(facts)] for k in range(2)]
        rows.append({'id': 'D_%04d' % i, 'title': '제목 %d' % i, 'text': '본문 %d ' % i * 80, 'customer_impact': '영향', 'change_direction': '방향',
                     'area_implication_refs': [area['implication']['id']], 'fact_refs': own, 'criterion_refs': [], 'goal_refs': []})
    return {'stage': 'synthesis', 'run': deepcopy(data['run']), 'directions': rows, 'edges': [], 'unresolved': []}


def review_input(data, merged, ids, summary=False):
    view = asis_synthesis._review_scope(data, [merged['directions'][i] for i in ids] if not summary else merged['directions'], summary=summary)
    draft = asis_synthesis._brief_directions(merged) if summary else asis_synthesis._sparse_directions(merged, ids)
    rd = deepcopy(view)
    rd['stage_data'].update(mode='synthesis-integration' if summary else 'synthesis-shard-' + str(ids[0]).zfill(4), draft=draft, candidate_sha256='h',
                            deterministic_check_result={'passed': True, 'candidate_sha256': 'h', 'actual_render': {}})
    return rd, draft


def project(tmp_path):
    data, plan = heavy_data(tmp_path)
    pipe = Pipeline(DirectionRuntime(plan), tmp_path / 'run')
    return data, pipe


def test_the_shard_review_input_is_classified_so_the_reduced_view_can_apply(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    rd, draft = review_input(data, merged, list(range(24, 48)))
    assert mv.kind_of(5, rd) == 'synthesis_shard_review'


def test_an_ordinary_document_review_is_not_mistaken_for_a_shard_review(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    rd, draft = review_input(data, merged, list(range(0, 24)))
    rd['stage_data']['mode'] = 'synthesis'
    rd['stage_data']['draft'] = {'stages': [merged]}
    assert mv.kind_of(5, rd) != 'synthesis_shard_review'


def test_the_shard_prompt_is_built_from_the_reduced_view_and_is_far_smaller(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    rd, draft = review_input(data, merged, list(range(24, 48)))
    old = pipe.render_prompt(5, rd, model_view=False)[1]
    new = pipe.render_prompt(5, rd)[1]
    assert len(new) < len(old) * 0.6, 'shard review must use the reduced view (old %d, new %d)' % (len(old), len(new))
    sent = body(new)
    assert sent['context'].get('model_view', {}).get('kind') == 'synthesis_shard_review'


def test_every_fact_and_criterion_the_shard_directions_cite_still_arrives(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    ids = list(range(24, 48))
    rd, draft = review_input(data, merged, ids)
    sent = body(pipe.render_prompt(5, rd)[1])
    need_f = {ref for i in ids for ref in merged['directions'][i]['fact_refs']}
    known = {f['id'] for f in data['facts']}
    assert (need_f & known) <= {f['id'] for f in sent['facts']}, 'cited facts must never be dropped'
    sent_impl = {a['implication']['id'] for a in sent['stage_data']['approved_areas']['areas']}
    need_impl = {ref for i in ids for ref in merged['directions'][i]['area_implication_refs']}
    assert need_impl <= sent_impl


def test_the_integration_review_receives_no_fact_bodies_and_is_not_treated_as_a_shard(tmp_path):
    """통합 검토는 호출부(_review_scope summary=True)가 사실·기준을 이미 비워 보낸다. 전송 뷰 계층이 이 호출을 구간 검토로 오분류하면 안 된다."""
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    rd, draft = review_input(data, merged, list(range(0, 24)), summary=True)
    assert mv.kind_of(5, rd) != 'synthesis_shard_review'
    sent = body(pipe.render_prompt(5, rd)[1])
    assert sent['facts'] == [], 'the integration review sees summaries only'
    assert all('text' not in row or len(row['text']) <= 240 for row in sent['stage_data']['draft']['directions'])


def test_the_reuse_key_render_of_a_shard_review_is_unchanged(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    rd, draft = review_input(data, merged, list(range(0, 24)))
    first = pipe.render_prompt(5, rd, model_view=False)[1]
    assert pipe.render_prompt(5, rd, model_view=False)[1] == first
    assert 'model_view' not in body(first)['context']


def test_the_real_shard_review_calls_carry_the_reduced_view(tmp_path):
    """실제 호출 경로(_review_merged_synthesis)에서 모델이 받는 구간 검토 메시지가 정본보다 작아야 한다."""
    data, pipe = project(tmp_path)
    merged = {**merged_for(data, 60), 'run': data['run']}
    seen = []

    class Runtime(DirectionRuntime):
        async def ask(self, role, message, **kwargs):
            payload = body(message)
            mode = str(payload['stage_data'].get('mode'))
            if role == 'content_review' and mode.startswith('synthesis-'):
                seen.append((mode, len(message.encode('utf-8')), payload['context'].get('model_view', {}).get('kind')))
            return await super().ask(role, message, **kwargs)

    runtime = Runtime({'directions': []})
    pipe = Pipeline(runtime, tmp_path / 'run2')
    asyncio.run(asis_synthesis._review_merged_synthesis(pipe, data, merged))
    shard = [row for row in seen if row[0].startswith('synthesis-shard-')]
    assert shard, seen
    assert all(kind == 'synthesis_shard_review' for _, _, kind in shard), seen
    canonical = len(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    assert max(size for _, size, _ in shard) < canonical * 0.6, (seen, canonical)


def wide_input(data, merged, ids):
    """호출부가 줄이지 않은 상태(정본 사실·영역 전체)에서 구간 검토 입력을 만든다. 뷰 필터가 실제로 일해야 하는 조건이다."""
    rd, draft = review_input(data, merged, ids)
    rd['facts'] = deepcopy(data['facts'])
    rd['criteria'] = deepcopy(data['criteria'])
    rd['stage_data']['approved_areas'] = deepcopy(data['stage_data']['approved_areas'])
    rd['context'].pop('model_scope', None)
    return rd, draft


def test_the_view_keeps_only_the_facts_the_shard_directions_cite(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    ids = list(range(24, 48))
    rd, draft = wide_input(data, merged, ids)
    assert len(rd['facts']) > 60, 'the fixture must carry facts nobody in this shard cites'
    kind, view = mv.build(5, rd)
    cited = {ref for i in ids for ref in merged['directions'][i]['fact_refs']}
    implication = {fid for a in rd['stage_data']['approved_areas']['areas'] if a['implication']['id'] in
                   {ref for i in ids for ref in merged['directions'][i]['area_implication_refs']} for fid in a['implication']['fact_refs']}
    sent = {f['id'] for f in view['facts']}
    assert sent == (cited | implication) & {f['id'] for f in data['facts']}, 'only cited facts (direction + linked implication) are sent'


def test_the_view_keeps_only_the_implications_the_shard_directions_link(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    ids = list(range(24, 30))
    # 이 프로젝트는 영역이 3개라 방향이 영역을 돌려 가며 연결하면 전부 연결된다. 담당 방향을 한 영역에만 묶어 '연결 안 된 시사점'을 만든다.
    only = data['stage_data']['approved_areas']['areas'][0]['implication']['id']
    for i in ids:
        merged['directions'][i]['area_implication_refs'] = [only]
    rd, draft = wide_input(data, merged, ids)
    total = len(rd['stage_data']['approved_areas']['areas'])
    kind, view = mv.build(5, rd)
    kept = {a['implication']['id'] for a in view['stage_data']['approved_areas']['areas']}
    assert total >= 3 and kept == {only}, 'unlinked implications must not be sent (total %d, kept %s)' % (total, sorted(kept))
    assert all(a['observations'] == [] for a in view['stage_data']['approved_areas']['areas'])


def test_facts_cited_by_a_linked_implication_are_kept_even_if_the_direction_does_not_cite_them(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    ids = list(range(24, 30))
    rd, draft = wide_input(data, merged, ids)
    for i in ids:
        merged['directions'][i]['fact_refs'] = []
    rd, draft = wide_input(data, merged, ids)
    kind, view = mv.build(5, rd)
    linked = {ref for i in ids for ref in merged['directions'][i]['area_implication_refs']}
    need = {fid for a in rd['stage_data']['approved_areas']['areas'] if a['implication']['id'] in linked for fid in a['implication']['fact_refs']}
    assert need and need <= {f['id'] for f in view['facts']}, 'an implication the shard links must keep its own cited facts'


def test_the_view_keeps_only_the_criteria_the_shard_directions_and_linked_implications_cite(tmp_path):
    data, pipe = project(tmp_path)
    merged = merged_for(data)
    ids = list(range(24, 30))
    # 기준이 여러 개 있는 프로젝트를 만들고, 담당 방향 한 곳만 기준 하나를 인용하게 한다.
    data['criteria'] = [{**deepcopy(data['criteria'][0]), 'id': 'C_%d' % n} for n in range(6)]
    for a in data['stage_data']['approved_areas']['areas']:
        a['implication']['criterion_refs'] = []
    for i in ids:
        merged['directions'][i]['criterion_refs'] = []
    merged['directions'][ids[0]]['criterion_refs'] = ['C_2']
    rd, draft = wide_input(data, merged, ids)
    assert len(rd['criteria']) == 6
    kind, view = mv.build(5, rd)
    assert [c['id'] for c in view['criteria']] == ['C_2'], 'criteria nobody in this shard cites must not be sent'
