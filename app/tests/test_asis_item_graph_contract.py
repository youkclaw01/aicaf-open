"""단일 항목의 의미 참조를 유지하며 관계선은 코드가 생성하는 생산 계약."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_pipeline import Pipeline
from test_asis_item_agents import ItemRuntime, item_fixture


class GraphlessWriter(ItemRuntime):
    """모델 경계만 합성이며 실제 원장·검토·구조 검사를 실행한다."""
    async def ask(self, role, message, **kwargs):
        raw, receipt = await super().ask(role, message, **kwargs)
        if role in ('author', 'direction_author'):
            candidate = json.loads(raw)
            candidate['edges'] = []
            raw = json.dumps(candidate, ensure_ascii=False)
        return raw, receipt


def test_single_item_graph_is_projected_without_spending_author_revision_budget(tmp_path):
    data = item_fixture()
    before = deepcopy(data)
    runtime = GraphlessWriter()
    pipe = Pipeline(runtime, tmp_path/'run')
    prepared, merged = asyncio.run(pipe.reviewed_actions(data))
    assert data == before
    assert len([v for v in runtime.calls if v['role'] in ('author', 'direction_author')]) == 5
    expected = [{'from_id':ref,'to_id':row['id'],'relation':relation}
                for family,field,relation in [('diagnoses','direction_refs','diagnoses'),('actions','diagnosis_refs','addresses')]
                for row in merged[family] for ref in row[field]]
    assert merged['edges'] == expected
    for call in runtime.calls:
        if call['role'] == 'content_review' and call['input']['stage_data'].get('item_assignment'):
            draft = call['input']['stage_data']['draft']
            assert draft['edges'], 'reviewer must see the same code-owned graph that deterministic checks consume'
    asis_contract = pipe.validator
    asis_contract.validate_output(prepared, merged)
    projections = list((tmp_path/'run/checkpoints').rglob('graph-projection.json'))
    assert len(projections) == 5


def test_reviewer_cannot_send_code_owned_graph_reversal_to_the_writer(tmp_path):
    """정상 관계선을 반대로 바꾸라는 지시는 검토 계약 오류이며 작성 예산을 소비하지 않는다."""
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan

    class ReverseGraphReviewer(ItemRuntime):
        def __init__(self):
            super().__init__()
            self.reviews = 0

        async def ask(self, role, message, **kwargs):
            raw, receipt = await super().ask(role, message, **kwargs)
            data = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if role == 'content_review':
                self.reviews += 1
                if self.reviews == 1:
                    raw = json.dumps({'stage':'review','run':data['run'],'verdict':'revise',
                        'content_checked':True,'render_checked':False,'unresolved':[],
                        'defects':[{'where':'/edges/0/relation','quote':data['stage_data']['draft']['edges'][0]['relation'],
                            'why':'합성 반례: 올바른 관계선의 방향을 잘못 판정한다.',
                            'fix':'관계선의 from_id와 to_id를 반대로 바꾼다.','evidence_refs':[]}]},ensure_ascii=False)
            return raw, receipt

    data = allocate_plan(item_fixture(), metadata_plan(), 'synthetic-plan')
    scoped = item_input(data,'diagnosis',data['context']['action_item_plan']['diagnoses'][0])
    runtime = ReverseGraphReviewer()
    pipe = Pipeline(runtime,tmp_path/'run')
    candidate = asyncio.run(pipe.reviewed_stage(scoped,3,'diagnosis-J_ITEM_0001'))
    assert len([v for v in runtime.calls if v['role'] in ('author', 'direction_author')]) == 1, 'a conflicting reviewer instruction was forwarded to the writer'
    reviews = [v for v in runtime.calls if v['role']=='content_review']
    assert len(reviews) == 2
    assert reviews[0]['input']['stage_data']['draft'] == reviews[1]['input']['stage_data']['draft'] == candidate
    assert reviews[1]['input']['stage_data']['review_contract_error'] == 'item_review_code_owned_graph_conflict'


def test_writer_and_reviewer_receive_the_same_item_scope_and_author_schema(tmp_path):
    """검토자에게도 단일 항목 출력 범위·외부 승인 노드·정방향 관계 계약을 실제 전달한다."""
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan
    data = allocate_plan(item_fixture(), metadata_plan(), 'synthetic-plan')
    scoped = item_input(data,'diagnosis',data['context']['action_item_plan']['diagnoses'][0])
    runtime = ItemRuntime()
    pipe = Pipeline(runtime,tmp_path/'run')
    asyncio.run(pipe.reviewed_stage(scoped,3,'diagnosis-J_ITEM_0001'))
    packets = [v['input']['stage_data'] for v in runtime.calls if v['role'] in ('author','content_review')]
    assert all('item_contract' in packet for packet in packets), 'the reviewer has no shared single-item output contract'
    assert packets[0]['item_contract'] == packets[1]['item_contract']
    contract = packets[0]['item_contract']
    assert contract['item_id'] == 'J_ITEM_0001'
    assert contract['empty_sibling'] == 'actions'
    assert contract['graph_authority'] == 'code_from_declared_semantic_references'
    assert contract['expected_edges'] == [{'from_id':ref,'to_id':'J_ITEM_0001','relation':'diagnoses'}
                                           for ref in scoped['stage_data']['item_assignment']['slot']['direction_refs']]
    assert contract['external_nodes_are_reference_only'] is True
    schema = json.loads(contract['author_output_schema'])
    assert schema['properties']['stage']['const'] == 'actions'
    assert '$ref' not in json.dumps(schema) or schema.get('$defs'), 'schema references must have their actual definitions'


def test_native_item_review_cannot_change_correct_allocated_parent_references(tmp_path):
    """시각 검토에서도 정상 선행 참조 변경 요구를 검토자에게 반환하고 같은 후보를 유지한다."""
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan

    class ParentChangeReviewer(ItemRuntime):
        def __init__(self):
            super().__init__()
            self.visual_reviews = 0

        async def ask(self, role, message, **kwargs):
            raw, receipt = await super().ask(role,message,**kwargs)
            if kwargs.get('image') is not None:
                self.visual_reviews += 1
                data = json.loads(message.rsplit('\n입력:\n',1)[-1])
                review = json.loads(raw)
                review['render_checked'] = True
                if self.visual_reviews == 1:
                    refs = data['stage_data']['draft']['actions'][0]['diagnosis_refs']
                    review.update(verdict='revise',defects=[{'where':'/actions/0/diagnosis_refs',
                        'quote':json.dumps(refs,ensure_ascii=False),'why':'합성 반례: 정상 할당된 선행 진단을 잘못 변경하라고 요구한다.',
                        'fix':'다른 진단을 추가하고 재작성한다.','evidence_refs':[]}])
                raw = json.dumps(review,ensure_ascii=False)
            return raw,receipt

    runtime = ParentChangeReviewer()
    pipe = Pipeline(runtime,tmp_path/'run')

    async def exercise():
        prepared, merged = await pipe.reviewed_actions(item_fixture())
        slot = prepared['context']['action_item_plan']['actions'][0]
        scoped = item_input(prepared,'action',slot,{row['id']:row for row in merged['diagnoses']})
        row = merged['actions'][0]
        draft = {'stage':'actions','run':prepared['run'],'diagnoses':[],'actions':[row],
                 'unresolved':[],'edges':[edge for edge in merged['edges'] if edge['to_id']==row['id']]}
        before = deepcopy(draft)
        author_count = len([call for call in runtime.calls if call['role'] in ('author', 'direction_author')])
        image = b'synthetic-provider-image-boundary'
        from hashlib import sha256
        review = await pipe.independent_review(scoped,draft,'synthetic-native-reference-check',
            item_key='action-'+slot['id'],image=image,
            render_record={'verified':True,'artifact_sha256':sha256(b'synthetic-test-artifact').hexdigest(),
                           'preview_sha256':sha256(image).hexdigest(),'classification':'synthetic_contract_only'})
        assert review['verdict'] == 'pass', 'an invalid ownership instruction escaped native review contract checking'
        assert runtime.visual_reviews == 2
        assert len([call for call in runtime.calls if call['role'] in ('author', 'direction_author')]) == author_count
        assert draft == before

    asyncio.run(exercise())
