"""방향 작성 응답 끝의 불필요한 '}' 가 파이프라인에서 보완 예산을 쓰지 않고 복구되는지 확인한다(합성 런타임, 모델 호출 없음).

운영 사고(2026-10-18): luna 응답 88건 중 3건이 완전한 JSON 뒤에 '}' 한 글자를 더 붙였고, 보완 3회를 같은 이유로 소진해 방향이 failed 가 됐다.
"""
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from asis_pipeline import Pipeline
from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture


class TrailingBraceRuntime(DirectionRuntime):
    """방향 작성 응답 끝에 '}' 를 붙이는 합성 런타임. 매 호출 붙이면 복구가 안 될 때 3회 소진으로 실패한다."""

    def __init__(self, plan, every_call=True):
        super().__init__(plan)
        self.every_call = every_call
        self.author_calls = 0

    async def ask(self, role, message, **kwargs):
        raw, receipt = await super().ask(role, message, **kwargs)
        if role == 'direction_author':
            self.author_calls += 1
            if self.every_call:
                raw = raw + '}'
        return raw, receipt


def test_a_trailing_brace_does_not_consume_the_revision_budget(tmp_path):
    data, plan = synthesis_fixture(tmp_path)
    runtime = TrailingBraceRuntime(plan)
    pipe = Pipeline(runtime, tmp_path / 'run')
    result = asyncio.run(pipe.reviewed_synthesis(data))
    assert len(result['directions']) == 2
    assert runtime.author_calls == 2, '방향마다 작성 1회로 끝나야 한다(보완 없음): %d' % runtime.author_calls


def test_the_raw_response_is_preserved_and_the_repair_is_recorded(tmp_path):
    data, plan = synthesis_fixture(tmp_path)
    runtime = TrailingBraceRuntime(plan)
    pipe = Pipeline(runtime, tmp_path / 'run')
    asyncio.run(pipe.reviewed_synthesis(data))
    repairs = sorted((tmp_path / 'run').rglob('response-repairs.json'))
    assert len(repairs) == 2, '방향 두 개 모두 정리 기록이 있어야 한다'
    for path in repairs:
        record = json.loads(path.read_text(encoding='utf-8'))
        assert record['repair'] == 'trailing_closers_removed' and record['removed'] == '}'
        raw = (path.parent / 'author-response.txt').read_text(encoding='utf-8')
        assert raw.endswith('}}') or raw.endswith('}'), '원문은 정리 전 그대로 남아야 한다'
        import hashlib
        assert record['raw_sha256'] == hashlib.sha256(raw.encode('utf-8')).hexdigest()
        assert record['parsed_sha256'] != record['raw_sha256']


def test_clean_responses_leave_no_repair_record(tmp_path):
    data, plan = synthesis_fixture(tmp_path)
    runtime = TrailingBraceRuntime(plan, every_call=False)
    pipe = Pipeline(runtime, tmp_path / 'run')
    asyncio.run(pipe.reviewed_synthesis(data))
    assert not list((tmp_path / 'run').rglob('response-repairs.json'))
