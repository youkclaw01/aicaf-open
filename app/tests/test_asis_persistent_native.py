"""실제 네이티브 산출물의 항목 검토가 원래 작성·검토 쌍으로 연결되는지 확인한다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_asis_connection import SyntheticRuntime, synthetic_input
import asis_artifacts
import asis_service


def test_rendered_item_uses_original_pair_until_all_its_pages_pass(tmp_path):
    """PPT·Excel·렌더는 실제 생성하며 외부 모델 전송만 명시적 합성이다."""
    instances, builds = [], []

    class NativeRuntime(SyntheticRuntime):
        def __init__(self, **kwargs):
            super().__init__()
            self.rejected = False
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def ask(self, role, message, **kwargs):
            data = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if 'stage_data' not in data:
                # 영역별 진단·과제 계획 호출(소형 입력)은 기본 합성 대역이 처리한다.
                return await super().ask(role, message, **kwargs)
            sd = data['stage_data']; assignment = sd.get('item_assignment', {})
            visual = kwargs.get('image') is not None
            if visual:
                defect = None
                if assignment.get('item_id') == 'A_ITEM_0001' and not self.rejected:
                    self.rejected = True
                    row = sd['draft']['actions'][0]
                    defect = {'where': '/actions/0/title', 'quote': row['title'],
                        'why': '합성 시험: 실제 장표의 대상 범위를 명시한다.',
                        'fix': '합성 시험의 대상 범위를 제목에 명시한다.', 'evidence_refs': []}
                self.calls.append({'role': role, 'mode': 'item_visual' if assignment else 'visual',
                    'input': deepcopy(data), **kwargs})
                return json.dumps({'stage': 'review', 'run': data['run'],
                    'verdict': 'revise' if defect else 'pass', 'content_checked': True,
                    'render_checked': True, 'defects': [defect] if defect else [], 'unresolved': []},
                    ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs.get('session_id')}
            raw, receipt = await super().ask(role, message, **kwargs)
            if role in ('author', 'direction_author') and assignment.get('item_id') == 'A_ITEM_0001' and sd.get('prior_output'):
                candidate = deepcopy(sd['prior_output'])
                candidate['actions'][0]['title'] = '합성 대상 범위를 명시한 확인 과제'
                raw = json.dumps(candidate, ensure_ascii=False)
            return raw, receipt

    def builder(result, folder):
        builds.append(deepcopy(result))
        return asis_artifacts.build(__import__('visual_spec_stub').with_visual_specs(result), folder)

    result, manifest = asyncio.run(asis_service.execute(synthetic_input(tmp_path), tmp_path/'run',
        builder=builder, runtime_factory=NativeRuntime))
    runtime = instances[0]
    item_visuals = [call for call in runtime.calls if call['mode'] == 'item_visual']
    assert item_visuals, 'rendered slides never reached their own item reviewer'
    expected = {row['id'] for family in ('diagnoses', 'actions') for row in result['stages'][2][family]}
    quality = manifest['quality_review']['item_reviews']
    assert {row['item_id'] for row in quality} == expected
    assert all(row['verdict'] == 'pass' and row['reviewed_pages'] for row in quality)
    assert len(builds) == 2 and result['stages'][2]['actions'][0]['title'] == '합성 대상 범위를 명시한 확인 과제'
    target = [call for call in runtime.calls if call['input'].get('stage_data', {}).get('item_assignment', {}).get('item_id') == 'A_ITEM_0001']
    authors = [call for call in target if call['role'] in ('author', 'direction_author')]
    reviews = [call for call in target if call['role'] == 'content_review']
    assert len(authors) == 2 and len({row['session_id'] for row in authors}) == 1
    assert len({row['session_id'] for row in reviews}) == 1, 'rendering replaced the item reviewer'
    assert {row['session_id'] for row in authors}.isdisjoint({row['session_id'] for row in reviews})
    assert all(row['reviewed_native_pptx_sha256'] == manifest['quality_review']['pptx_sha256'] for row in quality)
    ledger=json.loads((tmp_path/'run/task-ledger.json').read_text(encoding='utf-8'))
    assert all(row['status'] in ('completed','superseded') for row in ledger['tasks'].values()), {k:v['status'] for k,v in ledger['tasks'].items() if v['status'] not in ('completed','superseded')}
    assert all(v['reports'] for v in ledger['tasks'].values() if v['status']=='superseded'), 'old review evidence must be preserved'
    for unit in ['diagnosis-'+r['id'] for r in result['stages'][2]['diagnoses']]+['action-'+r['id'] for r in result['stages'][2]['actions']]:
        row=ledger['tasks']['native-'+unit]
        assert set(row['reports'])=={'author','reviewer'}
        assert row['reports']['reviewer']['checks']['render_checked'] is True
        assert row['reports']['author']['session_id']==ledger['tasks'][unit]['pair']['author']
    assert any(n['event']=='tasks_invalidated' for n in ledger['notifications'])
