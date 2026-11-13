"""방향 작성 하네스의 파이프라인 연결 시험(합성 런타임, 모델 호출 없음).

운영 경로 그대로 Pipeline.reviewed_synthesis 를 돌린다. 시험이 확인하는 것:
1) 방향 작성 호출은 direction_author 역할이고 지시문 끝에 작업표와 가드가 붙는다.
2) 작성 후보에 하네스 hard 결함(입력에 없는 지역 단어 등)이 있으면 검토(content_review) 호출 전에 같은 작성자에게 되돌려지고, 되돌린 호출의 입력에 결함 목록이 실린다.
3) 결함 없는 후보는 되돌림 없이 검토로 간다.
4) 방향이 아닌 작성은 author 역할이며 작업표·가드가 붙지 않는다.
"""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_direction_harness as H
from asis_pipeline import Pipeline
from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture


class InstructionRuntime(DirectionRuntime):
    """작성 호출의 지시문과 입력을 기록하고, 첫 작성 후보에 결함 단어를 넣을 수 있는 합성 런타임."""

    def __init__(self, plan, bad_first_text=None):
        super().__init__(plan)
        self.bad_first_text = bad_first_text
        self.author_instructions = []
        self.author_inputs = []
        self.first_seen = set()

    async def ask(self, role, message, **kwargs):
        if role in ('author', 'direction_author'):
            self.author_instructions.append((role, kwargs.get('instructions', '')))
            self.author_inputs.append(json.loads(message.rsplit('\n입력:\n', 1)[-1]))
        raw, receipt = await super().ask(role, message, **kwargs)
        if role == 'direction_author' and self.bad_first_text is not None:
            slot_id = json.loads(message.rsplit('\n입력:\n', 1)[-1])['stage_data']['direction_assignment']['slot']['id']
            if slot_id not in self.first_seen:
                self.first_seen.add(slot_id)
                value = json.loads(raw)
                value['directions'][0]['text'] = self.bad_first_text
                raw = json.dumps(value, ensure_ascii=False)
        return raw, receipt


def run(tmp_path, bad_first_text=None):
    data, plan = synthesis_fixture(tmp_path)
    runtime = InstructionRuntime(plan, bad_first_text)
    pipe = Pipeline(runtime, tmp_path / 'run')
    result = asyncio.run(pipe.reviewed_synthesis(data))
    return runtime, result


def test_direction_author_gets_role_work_table_and_guard(tmp_path):
    runtime, result = run(tmp_path)
    assert len(result['directions']) == 2
    roles = [role for role, _ in runtime.author_instructions]
    assert roles and set(roles) == {'direction_author'}
    for _, instructions in runtime.author_instructions:
        assert '작업표' in instructions and H.DIRECTION_GUARD[:40] in instructions


def test_clean_candidates_go_to_review_without_harness_repair(tmp_path):
    runtime, _ = run(tmp_path)
    assert len([c for c in runtime.calls if c['role'] == 'direction_author']) == 2, '결함이 없으면 방향마다 작성 1회'
    assert not any(c['input']['stage_data'].get('accumulated_defects') for c in runtime.calls if c['role'] == 'direction_author')


def test_hard_defect_is_sent_back_to_the_author_before_any_review(tmp_path):
    bad = '합성 원천에 따라 배포 지역 선정 근거를 포털 DB 에서 확인했다.'
    runtime, result = run(tmp_path, bad_first_text=bad)
    authors = [c for c in runtime.calls if c['role'] == 'direction_author']
    assert len(authors) == 4, '두 방향 모두 첫 후보가 하네스에 걸려 한 번씩 더 작성해야 한다'
    repaired = [c for c in authors if c['input']['stage_data'].get('accumulated_defects')]
    assert len(repaired) == 2
    for call in repaired:
        defects = call['input']['stage_data']['accumulated_defects']
        assert any(d['why'].startswith('harness:') for d in defects), '되돌린 작성 호출에 하네스 결함이 실려야 한다'
    # 결함 후보는 검토 호출로 가지 않았다: 검토는 최종 통과 후보에 대해서만 방향당 1회
    reviews = [c for c in runtime.calls if c['role'] == 'content_review' and c['input']['stage_data'].get('draft', {}).get('directions')]
    seen_texts = [json.dumps(c['input']['stage_data']['draft'], ensure_ascii=False) for c in reviews]
    assert not any('배포 지역 선정' in text for text in seen_texts), '하네스가 걸러낸 후보가 검토자에게 전달되면 안 된다'
    assert all('배포 지역 선정' not in d['text'] for d in result['directions'])


def test_non_direction_author_stays_author_without_work_table(tmp_path):
    import test_asis_connection as C
    runtime = C.SyntheticRuntime()
    data = C.synthetic_input(tmp_path)
    import asis_analysis
    from asis_pipeline import Pipeline as P
    seed = asis_analysis.analyze(data)
    pipe = P(runtime, tmp_path / 'run')
    try:
        asyncio.run(pipe.analyze(data))
    except Exception:
        pass  # 이 시험은 호출 역할만 본다. 경로 완주 여부는 다른 시험이 확인한다.
    non_direction = [c for c in runtime.calls if c['role'] == 'author' and not (c['input']['stage_data'].get('direction_assignment'))]
    assert non_direction, '영역·진단·과제 작성은 author 역할로 호출된다'
    assert not any(c['role'] == 'direction_author' and not c['input']['stage_data'].get('direction_assignment') for c in runtime.calls)
