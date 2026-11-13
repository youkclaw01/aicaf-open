"""모델 뷰의 범위 분리는 표시가 아니라 실제 전송 배열의 투영이어야 한다."""
from copy import deepcopy
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_evidence_views import prompt_view


def test_explicit_plan_scope_filters_actual_fact_criterion_goal_arrays_without_mutating_source():
    source = {
        'facts': [{'id': 'F_NETWORK', 'value': {'setting': 'network'}, 'source_id': 'collector', 'field_path': '/network'},
                  {'id': 'F_COST', 'value': {'amount': 500}, 'source_id': 'collector', 'field_path': '/cost'}],
        'criteria': [{'id': 'C_NETWORK', 'quote': 'Network criterion'}, {'id': 'C_COST', 'quote': 'Cost criterion'}],
        'goals': [{'id': 'G_NETWORK', 'text': 'Network objective'}, {'id': 'G_COST', 'text': 'Cost objective'}],
        'context': {'source_documents': {'untouched': [1, 2, 3]},
                    'model_scope': {'projection_schema': 'asis-model-scope/1',
                                    'fact_refs': ['F_NETWORK'], 'criterion_refs': ['C_NETWORK'], 'goal_refs': ['G_NETWORK']}},
    }
    before = deepcopy(source)
    transmitted = prompt_view(source)
    assert [row['id'] for row in transmitted['facts']] == ['F_NETWORK'], 'scope labels still send unrelated canonical facts'
    assert [row['id'] for row in transmitted['criteria']] == ['C_NETWORK']
    assert [row['id'] for row in transmitted['goals']] == ['G_NETWORK']
    assert 'source_documents' not in transmitted['context']
    assert source == before, 'model projection changed the canonical inventory or evidence'


def test_excluded_canonical_payloads_are_not_copied_before_being_removed():
    class ExcludedPayload:
        def __deepcopy__(self, memo):
            raise AssertionError('excluded raw payload was copied before scope projection')
    source = {
        'facts': [{'id': 'F_KEEP', 'value': [], 'source_id': 'collector', 'field_path': '/keep'},
                  {'id': 'F_DROP', 'value': ExcludedPayload(), 'source_id': 'collector', 'field_path': '/drop'}],
        'criteria': [], 'goals': [],
        'context': {'source_documents': ExcludedPayload(),
                    'model_scope': {'projection_schema': 'asis-model-scope/1', 'fact_refs': ['F_KEEP'],
                                    'criterion_refs': [], 'goal_refs': []}},
    }
    assert [row['id'] for row in prompt_view(source)['facts']] == ['F_KEEP']


def test_projected_context_and_dependent_arrays_have_no_dangling_reference_rows():
    source = {
        'facts': [{'id': 'F_KEEP', 'value': [], 'source_id': 'collector', 'field_path': '/keep'},
                  {'id': 'F_DROP', 'value': [], 'source_id': 'collector', 'field_path': '/drop'}],
        'criteria': [{'id': 'C_KEEP'}, {'id': 'C_DROP'}], 'goals': [{'id': 'G_KEEP'}, {'id': 'G_DROP'}],
        'measurements': [{'id': 'M_KEEP', 'fact_refs': ['F_KEEP']}, {'id': 'M_DROP', 'fact_refs': ['F_DROP']}],
        'judgments': [{'id': 'J_KEEP', 'fact_refs': ['F_KEEP'], 'criterion_refs': ['C_KEEP']},
                      {'id': 'J_DROP', 'fact_refs': ['F_DROP'], 'criterion_refs': ['C_DROP']}],
        'context': {'model_scope': {'projection_schema': 'asis-model-scope/1', 'fact_refs': ['F_KEEP'],
                                    'criterion_refs': ['C_KEEP'], 'goal_refs': ['G_KEEP']},
                    'criterion_assessments': [{'criterion_id': 'C_KEEP', 'fact_refs': ['F_KEEP']},
                                              {'criterion_id': 'C_DROP', 'fact_refs': ['F_DROP']}],
                    'dataset_register': [{'fact_id': 'F_KEEP'}, {'fact_id': 'F_DROP'}],
                    'goal_interpretations': [{'goal_id': 'G_KEEP'}, {'goal_id': 'G_DROP'}]},
    }
    view = prompt_view(source)
    assert [row['id'] for row in view['measurements']] == ['M_KEEP']
    assert [row['id'] for row in view['judgments']] == ['J_KEEP']
    assert [row['criterion_id'] for row in view['context']['criterion_assessments']] == ['C_KEEP']
    assert [row['fact_id'] for row in view['context']['dataset_register']] == ['F_KEEP']
    assert [row['goal_id'] for row in view['context']['goal_interpretations']] == ['G_KEEP']
