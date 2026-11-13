"""종합 관계선은 서술 참조로부터 코드가 생성하며 기존 검증 계약은 유지한다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import synthetic_input


def test_synthesis_codegen_prevents_repeated_criterion_and_goal_edge_rejection(tmp_path):
    """실제 장애의 추가 supports 관계를 주입해 같은 내용을 재작성하지 않는지 확인한다."""
    seed = asis_analysis.analyze(synthetic_input(tmp_path))
    data = deepcopy(seed['input'])
    data['stage_data'] = {'mode': 'synthesis', 'approved_areas': deepcopy(seed['stages'][0]),
                          'direction_slots': data['allocated_ids']['directions']}
    draft = deepcopy(seed['stages'][1])
    direction = draft['directions'][0]
    draft['edges'].append({'from_id': direction['criterion_refs'][0],
                           'to_id': direction['id'], 'relation': 'supports'})
    with pytest.raises(ValueError, match='edge_reference_mismatch'):
        asis_analysis.contract().validate_output(data, draft)
    calls = []

    class Replay:
        async def ask(self, role, message, **kwargs):
            calls.append(role)
            value = draft if role in ('author', 'direction_author') else {
                'stage': 'review', 'run': data['run'], 'verdict': 'pass',
                'content_checked': True, 'render_checked': False, 'defects': [], 'unresolved': []}
            return json.dumps(value, ensure_ascii=False), {'test_only': True, 'role': role}

    pipe = Pipeline(Replay(), tmp_path / 'run')
    result = asyncio.run(pipe.reviewed_stage(data, 2, 'synthesis'))
    assert result['directions'] == draft['directions']
    assert result['edges'] == seed['stages'][1]['edges']
    assert calls == ['author', 'content_review']
    asis_analysis.contract().validate_output(data, result)
    assert (tmp_path / 'run/checkpoints/synthesis/round-001/author-response.txt').read_text(
        encoding='utf-8') == json.dumps(draft, ensure_ascii=False)
    record = json.loads((tmp_path / 'run/checkpoints/synthesis/round-001/graph-projection.json').read_text(encoding='utf-8'))
    assert record['generated_by'] == 'code_from_direction_refs'
    assert record['raw_graph_authority'] is False
