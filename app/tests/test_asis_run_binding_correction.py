"""모델이 run 해시를 한 글자 틀리게 옮겨 써도 코드가 입력의 run으로 바로잡고, 다른 필드 변경은 보정하지 않는다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copy import deepcopy
import pytest
from asis_pipeline import Pipeline
from test_asis_item_agents import ItemRuntime, item_fixture


def _run(tmp_path, mutate):
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan

    class Mutating(ItemRuntime):
        async def ask(self, role, message, **kwargs):
            raw, receipt = await super().ask(role, message, **kwargs)
            if role in ('author', 'direction_author'):
                value = json.loads(raw)
                mutate(value)
                raw = json.dumps(value, ensure_ascii=False)
            return raw, receipt

    data = allocate_plan(item_fixture(), metadata_plan(), 'synthetic-plan')
    scoped = item_input(data, 'diagnosis', data['context']['action_item_plan']['diagnoses'][0])
    runtime = Mutating()
    return scoped, runtime, asyncio.run(Pipeline(runtime, tmp_path / 'run').reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001'))


def _flip_one_hash_character(value):
    run = value['run']
    text = run['source_revision']
    run['source_revision'] = text[:5] + ('f' if text[5] != 'f' else 'e') + text[6:]


def test_one_character_hash_typo_in_run_is_corrected_by_code_and_recorded(tmp_path):
    scoped, runtime, candidate = _run(tmp_path, _flip_one_hash_character)
    assert candidate['run'] == scoped['run']
    records = list((tmp_path / 'run' / 'checkpoints').rglob('run-binding-correction.json'))
    assert records
    record = json.loads(records[0].read_text(encoding='utf-8'))
    assert record['changed_fields'] == ['source_revision'] and record['corrected_by'] == 'code_from_input_run'
    assert len([c for c in runtime.calls if c['role'] in ('author', 'direction_author')]) == 1, 'the typo must not cost an author retry'


def test_structurally_different_run_is_not_corrected(tmp_path):
    def drop_field(value):
        value['run'].pop('track')
    import pytest as _p
    with _p.raises(Exception):
        _run(tmp_path, drop_field)
