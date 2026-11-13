"""blocked를 병합 검토로 넘겨 통과시킨 방향은 후보만 반환되는 것이 아니라 작업 원장도 완료로 확정되어야 한다.
운영에서 방향 D_P0032가 accepted.json을 저장했는데도 원장이 failed로 끝난 결함의 회귀 시험이다. 실제 방향 파이프라인(analyze)을 통과한다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copy import deepcopy
import asis_pipeline
from asis_pipeline import Pipeline
from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture
from test_asis_connection import synthetic_input  # noqa: F401

CROSS = {'fact_refs': [], 'topic': '공통 변경 통제의 영역 간 연결과 중복 해소 확인',
         'reason': '제공된 draft에는 이 방향만 있다. D_P0019·D_P0121 방향의 실제 내용과 관련 영역 참조가 제공되지 않아 통합 또는 역할 구분을 대조할 수 없다.',
         'required_evidence': '동일 run의 D_P0019·D_P0121 본문, 관련 영역별 승인 시사점과 통합 결정 자료.'}


class BlockingDirections(DirectionRuntime):
    """첫 번째 방향의 첫 검토만 다른 방향과의 대조 요구로 blocked를 낸다. 조정자 호출은 지정 조치를 반환한다."""
    def __init__(self, plan, arbiter_action=None):
        super().__init__(plan)
        self.blocked_once = False
        self.arbiter_action = arbiter_action
        self.arbiter_calls = 0

    async def ask(self, role, message, **kwargs):
        if role == 'content_review' and kwargs.get('session_id', '').startswith('asis-arbiter-'):
            self.arbiter_calls += 1
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            items = payload['blocked_items']
            return json.dumps({'decisions': [{'index': i, 'action': self.arbiter_action, 'evidence_ids': [],
                'quote': str(row['reason'])[:20], 'reason': '합성 판정'} for i, row in enumerate(items)]}, ensure_ascii=False), {'test_only': True, 'arbiter': True}
        raw, receipt = await super().ask(role, message, **kwargs)
        if role == 'content_review' and not self.blocked_once:
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            stage_data = payload.get('stage_data', {})
            # 방향 하나를 검토하는 호출만 막는다: 대상 후보가 방향 1개이고 방향 배정이 있는 입력이다.
            draft = stage_data.get('draft') or {}
            directions = draft.get('directions') if isinstance(draft, dict) else None
            if isinstance(directions, list) and len(directions) == 1 and stage_data.get('direction_assignment'):
                review = json.loads(raw)
                if review.get('verdict') == 'pass':
                    self.blocked_once = True
                    review.update(verdict='blocked', defects=[], content_checked=True, unresolved=[dict(CROSS)])
                    raw = json.dumps(review, ensure_ascii=False)
        return raw, receipt


def run(tmp_path, runtime_factory):
    data, plan = synthesis_fixture(tmp_path)
    runtime = runtime_factory(plan)
    pipe = Pipeline(runtime, tmp_path / 'run')
    result = asyncio.run(pipe.reviewed_synthesis(data))
    return pipe, runtime, result


def completed_direction_rows(pipe):
    return {k: v for k, v in pipe.tasks.ledger['tasks'].items() if k.startswith('direction-D_')}


def test_code_classified_cross_item_block_completes_the_direction_task_in_the_ledger(tmp_path):
    pipe, runtime, _ = run(tmp_path, lambda plan: BlockingDirections(plan))
    assert runtime.blocked_once, 'the fixture must actually exercise the blocked path'
    rows = completed_direction_rows(pipe)
    assert rows and all(v['status'] == 'completed' for v in rows.values()), {k: v['status'] for k, v in rows.items()}
    deferred = [v for v in rows.values() if v.get('reports', {}).get('reviewer', {}).get('checks', {}).get('deferred_to_merge_review')]
    assert deferred, 'one direction must carry the deferred-to-merge review in its ledger report'
    assert deferred[0]['reports']['reviewer']['checks']['verdict'] == 'pass'


def test_arbiter_defer_to_merge_completes_the_direction_task_in_the_ledger(tmp_path):
    # 조정자 경로는 코드 분류가 잡지 못하는 사유일 때만 쓰이므로, 사유를 대조 요구가 아닌 문장으로 바꿔 조정자로 보낸다.
    CROSS_BACKUP = dict(CROSS)
    CROSS.update(reason='제공된 자료만으로는 전체 계획의 일관성을 대조할 수 없다.', required_evidence='전체 계획 원장 대조 자료.')
    try:
        pipe, runtime, _ = run(tmp_path, lambda plan: BlockingDirections(plan, arbiter_action='defer_to_merge'))
    finally:
        CROSS.clear(); CROSS.update(CROSS_BACKUP)
    assert runtime.blocked_once and runtime.arbiter_calls >= 1, 'the arbiter path must be exercised'
    rows = completed_direction_rows(pipe)
    assert rows and all(v['status'] == 'completed' for v in rows.values()), {k: v['status'] for k, v in rows.items()}
