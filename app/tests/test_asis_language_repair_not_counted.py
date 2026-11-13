"""일본어·한자 혼입으로 되돌린 시도는 보완 횟수(3회)를 쓰지 않고, 같은 오류의 무한 반복만 별도 상한으로 막는다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime, synthetic_input


def test_issue_plan_area_survives_more_language_rejections_than_the_repair_budget(tmp_path):
    data = asis_analysis.analyze(synthetic_input(tmp_path))['input']
    calls = {'plan': 0}

    class Runtime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            stage = payload.get('stage_data', {})
            assignment = stage.get('plan_assignment')
            if role == 'interpret' and stage.get('mode') == 'issue_plan' and assignment and assignment['area_id'] == 'cost':
                calls['plan'] += 1
                # 처음 8번은 일본어 문자가 섞인 후보를 내고, 그 뒤에는 정상 후보를 낸다(보완 예산 6회보다 많다).
                bad = calls['plan'] <= 8
                return json.dumps({'issues': [{'area_refs': ['cost'], 'fact_refs': [payload['facts'][0]['id']],
                    'criterion_refs': [payload['criteria'][0]['id']] if payload['criteria'] else [], 'goal_refs': [],
                    'scope': '합성 비용 계획' + ('許' if bad else ''), 'reason': '제공된 근거 범위를 확인한다.'}]}, ensure_ascii=False), {'test_only': True}
            return await super().ask(role, message, **kwargs)

    planned = asyncio.run(Pipeline(Runtime(), tmp_path / 'run').plan_analysis(data))
    assert calls['plan'] == 9, 'language rejections must not consume the repair budget'
    assert any(row['scope'] == '합성 비용 계획' for row in planned['context']['analysis_plan']['issues'])


def test_endless_language_errors_still_stop_after_the_refund_limit(tmp_path):
    data = asis_analysis.analyze(synthetic_input(tmp_path))['input']

    class Runtime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            stage = payload.get('stage_data', {})
            assignment = stage.get('plan_assignment')
            if role == 'interpret' and stage.get('mode') == 'issue_plan' and assignment and assignment['area_id'] == 'cost':
                return json.dumps({'issues': [{'area_refs': ['cost'], 'fact_refs': [payload['facts'][0]['id']],
                    'criterion_refs': [payload['criteria'][0]['id']] if payload['criteria'] else [], 'goal_refs': [],
                    'scope': '합성 비용 許', 'reason': '근거 범위'}]}, ensure_ascii=False), {'test_only': True}
            return await super().ask(role, message, **kwargs)
    try:
        asyncio.run(Pipeline(Runtime(), tmp_path / 'run').plan_analysis(data))
        raise AssertionError('expected the endless repetition to stop')
    except RuntimeError as exc:
        assert 'issue_plan_repair_budget_exhausted:cost' in str(exc)


def test_reviewed_stage_budget_is_not_consumed_by_language_rejections(tmp_path):
    """작성·독립 검토 쌍(reviewed_stage)도 일본어·한자 반려로 보완 횟수를 쓰지 않는다."""
    import inspect
    import asis_pipeline
    source = inspect.getsource(asis_pipeline.Pipeline._reviewed_stage)
    assert 'language_refunds' in source and 'LANGUAGE_REPAIR_REFUND_LIMIT' in source
    assert "budget['remaining'] += 1" in source
    # 환급은 언어 검사 반려 분기 안에서만 일어나야 한다(내용·결정적 검사 반려는 횟수를 그대로 쓴다).
    branch = source[source.index("if language_defects:"):source.index("candidate_hash = digest(candidate)")]
    assert "budget['remaining'] += 1" in branch
    before_branch = source[:source.index("if language_defects:")]
    assert "budget['remaining'] += 1" not in before_branch
