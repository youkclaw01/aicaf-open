"""종합 계획 캐시는 무관한 검토 함수 변경과 실제 계획 로직 변경을 구분한다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_synthesis as synthesis
from asis_pipeline import Pipeline
from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture


def test_completed_plan_survives_unrelated_review_source_change(tmp_path, monkeypatch):
    data, model_plan = synthesis_fixture(tmp_path)
    before = deepcopy(data)
    folder = tmp_path / 'run'
    runtime = DirectionRuntime(model_plan)
    first = asyncio.run(synthesis.plan_synthesis(Pipeline(runtime, folder), data))
    assert first and runtime.calls
    source = Path(synthesis.__file__).read_text(encoding='utf-8')
    changed = tmp_path / 'review_only_source.py'
    changed.write_text(source + '\n\ndef unrelated_review_helper():\n    return "new_review_only"\n', encoding='utf-8')
    monkeypatch.setattr(synthesis, '__file__', str(changed))

    class NoNewModel(DirectionRuntime):
        async def ask(self, *args, **kwargs):
            raise AssertionError('unrelated_review_change_recreated_approved_synthesis_plan')

    resumed = asyncio.run(synthesis.plan_synthesis(Pipeline(NoNewModel(model_plan), folder), data))
    assert resumed == first
    assert data == before


def _legacy_plan_fixture(tmp_path, monkeypatch):
    import ast
    from hashlib import sha256
    import asis_synthesis_reuse as reuse
    from asis_model import digest

    data, model_plan = synthesis_fixture(tmp_path)
    folder = tmp_path / 'run'
    source = Path(synthesis.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'plan_synthesis')
    assignment = next(node for node in function.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'binding' for target in node.targets))
    assignment.value = ast.parse("digest({'input': data, 'instruction': SYNTHESIS_PLAN_INSTRUCTION, 'system': system, 'policy': policy(), 'code': Path(__file__).read_bytes().hex()})", mode='eval').body
    function.body = [node for node in function.body if not (isinstance(node, ast.ImportFrom) and node.module == 'asis_synthesis_reuse')]
    legacy_source = ast.unparse(tree).encode('utf-8')
    legacy_path = tmp_path / 'legacy_synthesis.py'
    legacy_path.write_bytes(legacy_source)

    def legacy_binding(input_data, instruction, system, model_policy, code):
        return digest({'input': input_data, 'instruction': instruction, 'system': system,
                       'policy': model_policy, 'code': code.hex()})

    with monkeypatch.context() as producer:
        producer.setattr(synthesis, '__file__', str(legacy_path))
        producer.setattr(reuse, 'plan_cache_binding', legacy_binding)
        first = asyncio.run(synthesis.plan_synthesis(Pipeline(DirectionRuntime(model_plan), folder), data))
    accepted = next((folder / 'checkpoints' / 'synthesis-plan').glob('*/accepted.json'))
    original_bytes = accepted.read_bytes()
    history = folder / 'runtime-source-history' / 'synthesis-plan-v1'
    history.mkdir(parents=True)
    (history / (sha256(legacy_source).hexdigest() + '.py')).write_bytes(legacy_source)
    return data, model_plan, folder, accepted, original_bytes, first, history


def test_legacy_approved_plan_is_rebound_without_new_model_or_rewriting_original(tmp_path, monkeypatch):
    data, model_plan, folder, accepted, original_bytes, first, history = _legacy_plan_fixture(tmp_path, monkeypatch)

    class NoNewModel(DirectionRuntime):
        async def ask(self, *args, **kwargs):
            raise AssertionError('legacy_approved_plan_was_regenerated')

    resumed = asyncio.run(synthesis.plan_synthesis(Pipeline(NoNewModel(model_plan), folder), data))
    assert resumed == first
    assert accepted.read_bytes() == original_bytes
    records = list((folder / 'checkpoints' / 'synthesis-plan').glob('*/accepted.json'))
    assert len(records) == 2
    assert any(b'legacy_plan_checkpoint' in record.read_bytes() for record in records)


@pytest.mark.parametrize('damage,error', [
    ('source', 'legacy_planner_source_hash_mismatch'),
    ('candidate', 'legacy_synthesis_plan_checkpoint_binding_failed'),
    ('review', 'legacy_synthesis_plan_checkpoint_binding_failed'),
])
def test_legacy_plan_rejects_tampered_source_candidate_or_review(tmp_path, monkeypatch, damage, error):
    import json
    data, model_plan, folder, accepted, original_bytes, first, history = _legacy_plan_fixture(tmp_path, monkeypatch)
    if damage == 'source':
        previous = next(history.glob('*.py'))
        previous.write_bytes(previous.read_bytes() + b'\n# changed after capture\n')
    else:
        record = json.loads(accepted.read_text(encoding='utf-8'))
        if damage == 'candidate':
            record['candidate']['directions'][0]['reason'] += ' altered'
        else:
            record['review']['verdict'] = 'revise'
        accepted.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')

    class NoNewModel(DirectionRuntime):
        async def ask(self, *args, **kwargs):
            raise AssertionError('invalid_legacy_record_triggered_model_instead_of_failing_closed')

    with pytest.raises(ValueError, match=error):
        asyncio.run(synthesis.plan_synthesis(Pipeline(NoNewModel(model_plan), folder), data))


@pytest.mark.parametrize('function_name', ['validate_plan', '_plan_area_synthesis', '_merge_overlapping_directions'])
def test_actual_planning_logic_change_changes_component_binding(function_name):
    import ast
    from asis_synthesis_reuse import planner_component_sha256
    source = Path(synthesis.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name)
    function.body.insert(0, ast.parse("raise ValueError('changed_planner_contract')").body[0])
    changed = ast.unparse(tree)
    assert planner_component_sha256(source) != planner_component_sha256(changed)


@pytest.mark.parametrize('dimension', ['input', 'instruction', 'system', 'model_policy'])
def test_semantic_input_or_model_contract_change_never_reuses_same_binding(tmp_path, dimension):
    from asis_synthesis_reuse import plan_cache_binding
    from asis_runtime import policy
    data, model_plan = synthesis_fixture(tmp_path)
    source = Path(synthesis.__file__).read_bytes()
    values = [data, 'instruction', 'system', policy(), source]
    before = plan_cache_binding(*values)
    changed = deepcopy(values)
    if dimension == 'input':
        changed[0]['context']['cache_test_change'] = 'different_source_scope'
    elif dimension == 'instruction':
        changed[1] += ' changed'
    elif dimension == 'system':
        changed[2] += ' changed'
    else:
        changed[3]['models']['interpret'] += '-different'
    assert plan_cache_binding(*changed) != before
