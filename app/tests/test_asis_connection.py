"""MAF 문서 경로와 포털 호출 연결 시험. 모델 응답은 명시적 합성 전송이다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import asis_analysis
import asis_model
import asis_pipeline


def synthetic_input(tmp_path):
    source = tmp_path / 'synthetic-source'
    source.mkdir()
    topology = {'resources': [], 'verification': {'resourceCount': {'complete': True, 'collected': 0, 'uniqueIds': 0}},
                'configurations': {'subnets': []},
                'coverage': {'detailDatasets': {'subnets': {'status': 'observed-empty', 'recordCount': 0}}}}
    (source / 'topology.json').write_text(json.dumps(topology), encoding='utf-8')
    (source / 'aicaf-evidence.json').write_text(json.dumps({'sections': {}}), encoding='utf-8')
    assessment = {'id': 999999, 'track': 'new', 'infra_case_id': 'SYNTHETIC_CONNECTION_ONLY',
                  'customer': '합성 연결 시험', 'title': '제품 계약 회귀'}
    answer = {'qid': 'Q1', 'transcript': 'AI로 내부 문서 검색을 지원하고 접근 권한은 현재 체계를 유지하려고 합니다.'}
    data = asis_model.normalize(source, assessment, [answer], 'synthetic-maf', classification='synthetic_unit_test')
    data['context']['synthetic_contract_only'] = True
    return data


class SyntheticRuntime:
    """실제 모델을 대신하는 시험 경계. 생산 경로에는 사용할 수 없다."""
    def __init__(self):
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def ask(self, role, message, **kwargs):
        data = json.loads(message.rsplit('\n입력:\n', 1)[-1])
        if role == 'interpret' and 'stage_data' not in data and 'area' in data and 'issues' in data:
            # 영역별 진단·과제 계획 호출: 입력이 이슈·방향 제목과 ID뿐인 소형 입력이다.
            from area_item_plan_stub import area_item_plan_answer
            self.calls.append({'role': role, 'mode': 'action_item_plan_area', 'input': deepcopy(data), **kwargs})
            return json.dumps(area_item_plan_answer(data), ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs['session_id']}
        if role == 'interpret' and 'stage_data' not in data and 'candidates' in data and 'limit' in data:
            # 통합 호출(진단 주제·과제 통합): 입력이 후보 번호·제목·이유뿐이다.
            from area_item_plan_stub import consolidation_answer
            self.calls.append({'role': role, 'mode': 'consolidation', 'input': deepcopy(data), **kwargs})
            return json.dumps(consolidation_answer(data), ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs['session_id']}
        if role == 'interpret' and 'stage_data' not in data and 'topics' in data and 'layers' in data:
            # 장표 시각 명세 호출: 영역의 진단 주제마다 계약을 통과하는 명세를 돌려준다(인용 사실이 있으면 chart, 없으면 architecture).
            self.calls.append({'role': role, 'mode': 'visual_specs', 'input': deepcopy(data), **kwargs})
            visuals = {}
            for topic in data['topics']:
                visuals[topic['topic_id']] = {'form': 'architecture', 'headline': '구성 개요', 'conclusion': '핵심 결론 한 줄',
                                              'data': {'focus_layers': ['network'], 'gap_layers': ['network']}}
            return json.dumps({'visuals': visuals}, ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs['session_id']}
        if role == 'interpret' and 'stage_data' not in data and 'overall_100' in data and 'areas' in data:
            # 성숙도 스파이더 차트 평가 의견 호출
            self.calls.append({'role': role, 'mode': 'maturity_spider', 'input': deepcopy(data), **kwargs})
            area_opinions = {r['area_id']: f"{r['name']} 영역 인터뷰 평가 및 실측 기반 성숙도 의견입니다." for r in data['areas']}
            return json.dumps({
                'headline': '핵심 통제 영역의 성숙도 격차 해소가 선결 과제입니다',
                'verdict_banner': '판정  |  인터뷰 종합 점수와 실측 구성을 교차 대조하여 기준선 미달 영역을 우선 보완합니다.',
                'area_opinions': area_opinions,
            }, ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs['session_id']}
        if role == 'interpret' and 'stage_data' not in data and 'goal_interpretations' in data and 'interview_topics' in data:
            # 목표 및 정책 분석 장표 AI 의견 호출
            import asis_goal_policy as gp
            self.calls.append({'role': role, 'mode': 'goal_policy_analysis', 'input': deepcopy(data), **kwargs})
            return json.dumps({'goal_policy_analysis': gp.fallback_goal_policy_spec({})}, ensure_ascii=False), {'test_only': True, 'role': role, 'session_id': kwargs['session_id']}
        mode = data['stage_data'].get('mode')
        self.calls.append({'role': role, 'mode': mode, 'input': deepcopy(data), **kwargs})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(.002)
        try:
            if role == 'interpret' and mode == 'synthesis_second_pass_grouping':
                return json.dumps({'groups': []}), {'test_only': True, 'role': role}
            if role == 'interpret' and mode == 'synthesis_cross_area_grouping':
                # 통합 계획 호출: 시험 기본 응답은 묶을 후보가 없다는 답이다.
                return json.dumps({'groups': []}, ensure_ascii=False), {'test_only': True, 'role': role}
            if role == 'interpret' and mode == 'issue_plan' and data['stage_data'].get('plan_assignment'):
                # 영역별 계획 호출: 배정 영역의 이슈만, 제공된 근거 범위 안에서 반환한다.
                key = data['stage_data']['plan_assignment']['area_id']
                value = {'issues': [{'area_refs': [key],
                    'fact_refs': [data['facts'][0]['id']], 'criterion_refs': [data['criteria'][0]['id']],
                    'goal_refs': [], 'scope': key + ' synthetic_scope',
                    'reason': 'Synthetic transport follows the declared approved domain baseline.'}]}
            elif role == 'interpret' and mode == 'issue_plan':
                seed = asis_analysis.analyze(data)
                value = {'issues':[{'area_refs':[area['area_id']], 'fact_refs':area['implication']['fact_refs'],
                    'criterion_refs':area['implication']['criterion_refs'], 'goal_refs':[],
                    'scope':area['area_id']+' synthetic_scope',
                    'reason':'Synthetic transport follows the declared approved domain baseline.'}
                    for area in seed['stages'][0]['areas']]}
            elif role == 'interpret' and mode == 'synthesis_plan':
                areas = data['stage_data']['approved_areas']['areas']
                issues = data['context']['analysis_plan']['issues']
                value = {'directions': [dict(scope=area['implication']['text'],
                    reason='합성 종합 범위 배정', area_implication_refs=[area['implication']['id']],
                    issue_refs=[issue['id'] for issue in issues if area['area_id'] in issue['area_refs']],
                    **{key: deepcopy(area['implication'][key]) for key in ('fact_refs','criterion_refs','goal_refs')})
                    for area in areas]}
            elif role == 'interpret' and mode == 'action_item_plan':
                issues = data['context']['analysis_plan']['issues']
                directions = data['stage_data']['approved_directions']['directions']
                slots = []
                for direction in directions:
                    linked = [issue['id'] for issue in issues if issue['direction_id'] == direction['id']]
                    slots.append({'scope': direction['title'], 'reason': '합성 항목별 계획 시험',
                        'direction_refs': [direction['id']], 'issue_refs': linked,
                        **{key: deepcopy(direction[key]) for key in ('fact_refs', 'criterion_refs', 'goal_refs')}})
                value = {'diagnoses': slots, 'actions': [dict(deepcopy(slot), diagnosis_indices=[i])
                    for i, slot in enumerate(slots)], 'coverage': [
                    {'issue_id': issue['id'], 'diagnosis_indices': [i], 'action_indices': [i],
                     'disposition': 'included', 'reason': '합성 항목 배정', 'required_evidence': ''}
                    for i, issue in enumerate(issues)]}
            elif role == 'interpret':
                value = {'goals': [{'source_fact_id': 'F_IV_Q1_transcript', 'quote': next(f['value'] for f in data['facts'] if f['id'] == 'F_IV_Q1_transcript'),
                    'interpretation': '현재 접근 권한 체계를 유지하는 내부 문서 검색 도입',
                    'area_refs': ['data', 'secgov', 'ai'], 'ambiguity': ''}], 'requirements': []}
            elif role in ('author', 'direction_author'):
                seed = asis_analysis.analyze(data)
                if mode == 'area_issue':
                    # 이슈 1개 호출: 할당된 관찰 1건만 반환한다(영역 합본은 코드가 만든다).
                    selected = data['stage_data']['area_slots'][0]['area_id']
                    item = data['stage_data']['issue_assignment']['item_id']
                    value = seed['stages'][0]
                    area = next(a for a in value['areas'] if a['area_id'] == selected)
                    template = deepcopy(area['observations'][0])
                    template.update(id=item, fact_refs=deepcopy(data['stage_data']['planned_observation_slots'][0]['fact_refs']),
                                    criterion_refs=deepcopy(data['stage_data']['planned_observation_slots'][0]['criterion_refs']))
                    area['observations'] = [template]
                    value['areas'] = [area]
                elif mode == 'area_summary':
                    selected = data['stage_data']['area_slots'][0]['area_id']
                    value = seed['stages'][0]
                    area = next(a for a in value['areas'] if a['area_id'] == selected)
                    area['observations'] = []
                    value['areas'] = [area]
                elif mode == 'area':
                    selected = data['stage_data']['area_slots'][0]['area_id']
                    value = seed['stages'][0]
                    value['areas'] = [a for a in value['areas'] if a['area_id'] == selected]
                elif mode == 'synthesis' and data['stage_data'].get('direction_assignment'):
                    slot = data['stage_data']['direction_assignment']['slot']
                    area = data['stage_data']['approved_areas']['areas'][0]
                    seed_direction = next(item for item in seed['stages'][1]['directions']
                                          if area['implication']['id'] in item['area_implication_refs'])
                    row = dict(id=slot['id'], title=seed_direction['title'], text=area['implication']['text'],
                        customer_impact=area['implication']['impact'], change_direction=area['implication']['change_direction'],
                        **{key: deepcopy(slot[key]) for key in ('area_implication_refs','fact_refs','criterion_refs','goal_refs')})
                    value = {'stage':'synthesis','run':deepcopy(data['run']), 'directions':[row],'edges':[],'unresolved':[]}
                elif mode == 'actions' and data['stage_data'].get('item_assignment'):
                    assignment = data['stage_data']['item_assignment']; slot = assignment['slot']
                    family = 'diagnoses' if assignment['kind'] == 'diagnosis' else 'actions'
                    # 합성 시드는 영역당 방향 1개 기준이다. 슬롯이 연결한 방향이 시드에 없으면(이슈가 여러 개인 영역) 같은 종류의 첫 항목을 틀로 쓴다.
                    row = deepcopy(next((r for r in seed['stages'][2][family] if slot['direction_refs'][0] in r['direction_refs']),
                                        seed['stages'][2][family][0]))
                    row.update(id=assignment['item_id'], **{key: deepcopy(slot[key]) for key in
                        ('direction_refs', 'fact_refs', 'criterion_refs', 'goal_refs')})
                    if family == 'actions': row['diagnosis_refs'] = deepcopy(slot['diagnosis_refs'])
                    value = {'stage': 'actions', 'run': deepcopy(data['run']), 'diagnoses': [],
                             'actions': [], 'unresolved': [], 'edges': []}
                    value[family] = [row]
                    field, relation = ('direction_refs', 'diagnoses') if family == 'diagnoses' else ('diagnosis_refs', 'addresses')
                    value['edges'] = [{'from_id': ref, 'to_id': row['id'], 'relation': relation} for ref in row[field]]
                else:
                    value = seed['stages'][{'synthesis': 1, 'actions': 2, 'summary': 3}[mode]]
                    if mode == 'summary' and data['stage_data'].get('approved_actions'):
                        value['summary']['action_refs'] = [row['id'] for row in data['stage_data']['approved_actions']['actions']]
                        value['summary']['direction_refs'] = [row['id'] for row in data['stage_data']['approved_directions']['directions']]
                # 합성 코드는 소비자의 할당 계약에 맞춘다. 고객 결과로 사용하지 않는다.
            else:
                value = {'stage': 'review', 'run': data['run'], 'verdict': 'pass',
                         'content_checked': True, 'render_checked': kwargs.get('image') is not None,
                         'defects': [], 'unresolved': []}
            return json.dumps(value, ensure_ascii=False), {'test_only': True, 'role': role,
                'session_id': kwargs.get('session_id'), 'actual_model': 'synthetic_not_a_provider'}
        finally:
            self.active -= 1


def test_full_semantic_path_uses_separate_parallel_lanes_and_summary_last(tmp_path):
    data = synthetic_input(tmp_path)
    runtime = SyntheticRuntime()
    events = []
    engine = asis_pipeline.Pipeline(runtime, tmp_path / 'run', progress=events.append)
    assert hasattr(engine, 'analyze'), 'portal analysis entrypoint is missing'
    result = asyncio.run(engine.analyze(data, revision_context={'requirements': '', 'prior_output': None}))
    assert [s['stage'] for s in result['stages']] == ['area', 'synthesis', 'actions', 'summary']
    assert result['model_call_performed'] is True
    assert result['quality_status'] == 'content_review_passed'
    assert result['input']['goals'][0]['quote'] == data['facts'][-3]['value']
    assert result['input']['context']['current_operations_verified'] is False
    assert runtime.max_active >= 2
    modes = [c['mode'] for c in runtime.calls if c['role'] in ('author', 'direction_author')]
    assert modes[-1] == 'summary' and modes.index('synthesis') < modes.index('actions')
    assert modes.count('actions') == len(result['stages'][2]['diagnoses']) + len(result['stages'][2]['actions'])
    # 이 합성 계획은 영역당 이슈가 정확히 1개인 이전 방식이라 영역 단위 호출을 유지한다. 이슈가 여러 개인 영역은
    # area_issue(이슈 1개당 1호출)와 area_summary(영역 시사점 1호출)로 나뉘며 test_asis_area_per_issue_agents.py가 검증한다.
    assert modes.count('area') + modes.count('area_issue') >= len(asis_analysis.AREA_NAMES)
    review_modes = {c['mode'] for c in runtime.calls if c['role'] == 'content_review'}
    # 12단계는 아키텍처 관점 4단위다(영석님 정의 2026-10-19). 영역·요약·공통 샤드는 더 이상 별도 호출이 아니다.
    assert {'document-integration', 'document-shard-arch-area', 'document-shard-arch-direction', 'document-shard-arch-action'} <= review_modes
    assert not any(mode.startswith('document-shard-area-') or mode in ('document-shard-summary', 'document-shard-shared') for mode in review_modes)
    for c in runtime.calls:
        assert c['input']['run'] == data['run']
    assert all(a['run'] == data['run'] for a in result['input']['facts'])


def test_connected_controller_generates_native_pair_and_requires_real_render_review(tmp_path):
    import sqlite3
    import asis_view
    data = synthetic_input(tmp_path)
    source = tmp_path/'synthetic-source'
    transcript = next(f['value'] for f in data['facts'] if f['id'] == 'F_IV_Q1_transcript')
    assessment = {'id':999999, 'track':'new', 'infra_case_id':'SYNTHETIC_CONNECTION_ONLY',
                  'customer':'합성 연결 시험', 'title':'제품 계약 회귀'}
    def db():
        conn = sqlite3.connect(tmp_path/'fixture.sqlite3')
        conn.row_factory = sqlite3.Row
        conn.execute('create table if not exists answers(assessment_id integer,qid text,transcript text,summary text,score integer)')
        if not conn.execute('select count(*) from answers').fetchone()[0]:
            conn.execute('insert into answers values (?,?,?,?,?)',(999999,'Q1',transcript,'',None))
            conn.commit()
        return conn
    observed = []
    class FixtureRuntime(SyntheticRuntime):
        def __init__(self, **kwargs):
            super().__init__(); observed.append(self)
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return None
        async def ask(self, role, message, **kwargs):
            if role == 'visual_review':
                assert kwargs['image'], 'image bytes must reach the review transport'
                payload=json.loads(message.rsplit('\n입력:\n',1)[-1])
                assert payload['stage_data']['deterministic_check_result']['actual_render']['verified']
                value={'stage':'review','run':payload['run'],'verdict':'pass',
                       'content_checked':True,'render_checked':True,'defects':[],'unresolved':[]}
                self.calls.append({'role':role,'mode':'visual','input':payload,**kwargs})
                return json.dumps(value,ensure_ascii=False), {'test_only':True,'role':role,'actual_model':'synthetic_not_a_provider'}
            return await super().ask(role,message,**kwargs)
    controller = asis_view.Controller(ROOT=tmp_path/'portal', assessment=lambda aid:assessment,
        infra_case_dir=lambda cid:source.parent, db=db, source_folder=lambda a:source,
        runtime_factory=FixtureRuntime)
    current=controller.current(999999)
    job=controller.create(999999, {'nonce':'connected-unit', 'input_sha256':current['input_sha256'],
                                 'parent_revision_id':None, 'requirements':''})
    outcome=controller.generate(999999,job['revision_id'])
    assert outcome['status']=='generated_candidate', outcome.get('error')
    assert outcome['quality_status']=='automated_review_passed'
    assert outcome['model_call_performed'] is True
    manifest=controller.manifest(999999,job['revision_id'])
    assert manifest['quality_status']=='automated_review_passed'
    assert manifest['quality_review']['all_render_pages_reviewed'] is True
    assert manifest['quality_review']['reviewed_pages']==manifest['render']['page_count']
    assert observed and any(c['role']=='visual_review' for c in observed[0].calls)
    for kind in ('pptx','xlsx','pdf'):
        raw,_=controller.artifact(999999,job['revision_id'],kind)
        assert len(raw)>100
    phases={unit['phase'] for unit in outcome['progress']['units']}
    assert {'content_review','visual_review','consistency'}.issubset(phases)


def test_native_pair_checker_rejects_changed_backdata_fact(tmp_path):
    import pytest
    import asis_artifacts
    from openpyxl import load_workbook
    assert hasattr(asis_artifacts,'verify_pair'), 'native pair content verification is missing'
    data=synthetic_input(tmp_path)
    result=asis_analysis.analyze(data)
    folder=tmp_path/'native-pair'
    manifest=asis_artifacts.build(result,folder)
    by_role={item['role']:folder/item['name'] for item in manifest['artifacts']}
    ppt=by_role['asis_report_pptx'];book=by_role['asis_backdata_xlsx']
    verified=asis_artifacts.verify_pair(result,ppt,book)
    assert verified['fact_count']==len(result['input']['facts'])
    from io import BytesIO
    from zipfile import ZipFile
    import xml.etree.ElementTree as ET
    with ZipFile(book) as src:
        payload={entry.filename:(entry,src.read(entry.filename)) for entry in src.infolist()}
    doc=ET.fromstring(payload['xl/workbook.xml'][1])
    sheet=next(e for e in doc.iter() if e.attrib.get('name')=='사실원장')
    rid=next(v for k,v in sheet.attrib.items() if k.endswith('}id'))
    rels=ET.fromstring(payload['xl/_rels/workbook.xml.rels'][1])
    target=next(e.attrib['Target'] for e in rels if e.attrib['Id']==rid)
    target=target.lstrip('/') if target.startswith('/') else 'xl/'+target
    xml=ET.fromstring(payload[target][1]);ns=xml.tag.split('}')[0]+'}'
    cell=next(e for e in xml.iter(ns+'c') if e.attrib.get('r')=='F7')
    attrs=dict(cell.attrib);cell.clear();cell.attrib.update(attrs);cell.set('t','inlineStr')
    ET.SubElement(ET.SubElement(cell,ns+'is'),ns+'t').text='tampered'
    output=BytesIO()
    with ZipFile(output,'w') as dst:
        for name,(entry,raw) in payload.items():
            dst.writestr(entry,ET.tostring(xml,encoding='utf-8',xml_declaration=True) if name==target else raw)
    book.write_bytes(output.getvalue())
    with pytest.raises(ValueError,match='backdata_fact_value_mismatch'):
        asis_artifacts.verify_pair(result,ppt,book)
