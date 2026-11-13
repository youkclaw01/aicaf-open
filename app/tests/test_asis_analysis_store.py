"""analysis.db(분석 산출물 저장소) 시험. docs/EVIDENCE-STORE-DESIGN-V2.md 6절의 계약.

입력은 평가 24 승인 산출물과 같은 모양(stage_data: approved_areas / approved_directions / approved_actions)의 작은 표본이다.
정본은 DB이고, 기존 JSON 산출물은 DB에서 다시 만들어 낸 것이 원본과 같아야 한다(왕복 동일성).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_analysis_store as astore

RUN = {'analysis_revision': 'r-1', 'assessment_id': 24, 'case_id': 'c', 'source_bundle_sha256': 'x', 'source_revision': 'x', 'track': 'existing'}


def stage_data():
    areas = {'stage': 'area', 'run': RUN, 'areas': [
        {'area_id': 'network', 'headline': '네트워크 헤드라인', 'implication': '네트워크 시사점',
         'observations': [
             {'id': 'O_P0001', 'text': 'AG-HUB-01은 WAF-01을 참조한다.', 'fact_refs': ['F_T_applicationGatewayConfiguration', 'F_IV_XB3-4_transcript'],
              'criterion_refs': ['AILZ-N-R5'], 'goal_refs': ['G_1'], 'judgment_refs': []},
             {'id': 'O_P0002', 'text': '서브넷 11개.', 'fact_refs': ['F_T_subnets'], 'criterion_refs': [], 'goal_refs': [], 'judgment_refs': []}]},
        {'area_id': 'cost', 'headline': '비용 헤드라인', 'implication': '비용 시사점',
         'observations': [{'id': 'O_P0003', 'text': '가격표 관측.', 'fact_refs': ['F_E_pricesModels'], 'criterion_refs': [], 'goal_refs': [], 'judgment_refs': []}]}],
        'unresolved': [{'fact_refs': ['F_T_subnets'], 'reason': '서브넷 용도 미확인', 'required_evidence': '배치 문서'}]}
    dirs = {'stage': 'synthesis', 'run': RUN, 'directions': [
        {'id': 'D_P0001', 'title': '방향 하나', 'text': '방향 본문', 'customer_impact': '영향', 'change_direction': '변경',
         'area_implication_refs': ['I_network', 'I_cost'], 'criterion_refs': ['AILZ-N-R5'], 'fact_refs': ['F_T_subnets', 'F_E_pricesModels'], 'goal_refs': ['G_1']}],
        'edges': [{'from_id': 'F_T_subnets', 'relation': 'supports', 'to_id': 'D_P0001'}, {'from_id': 'F_E_pricesModels', 'relation': 'supports', 'to_id': 'D_P0001'}],
        'unresolved': []}
    actions = {'stage': 'actions', 'run': RUN, 'diagnoses': [
        {'id': 'J_ITEM_0001', 'text': '진단 본문', 'impact': '진단 영향', 'direction_refs': ['D_P0001'], 'criterion_refs': ['AILZ-N-R5'],
         'fact_refs': ['F_T_subnets'], 'goal_refs': ['G_1']}],
        'actions': [{'id': 'A_ITEM_0001', 'title': '과제', 'change_type': 'modify', 'deliverable': '산출물', 'detailed_implementation_ref': None,
                     'diagnosis_refs': ['J_ITEM_0001'], 'direction_refs': ['D_P0001'], 'criterion_refs': ['AILZ-N-R5'], 'fact_refs': ['F_T_subnets'],
                     'goal_refs': ['G_1'], 'improvement_points': ['점1', '점2'], 'prerequisites': ['선행'], 'priority_basis': '근거', 'priority_level': 'high',
                     'rationale': '이유', 'target': '대상', 'verification_condition': '검증 조건'}],
        'edges': [{'from_id': 'D_P0001', 'relation': 'diagnoses', 'to_id': 'J_ITEM_0001'}, {'from_id': 'J_ITEM_0001', 'relation': 'actions', 'to_id': 'A_ITEM_0001'}],
        'unresolved': []}
    return {'approved_areas': areas, 'approved_directions': dirs, 'approved_actions': actions}


def build(tmp_path):
    path = tmp_path / 'analysis.db'
    astore.load(path, stage_data(), run=RUN)
    return astore.open_analysis(path), path


def test_roundtrip_regenerates_the_original_stage_json_exactly(tmp_path):
    h, _ = build(tmp_path)
    original = stage_data()
    assert h.export_stage('approved_areas') == original['approved_areas']
    assert h.export_stage('approved_directions') == original['approved_directions']
    assert h.export_stage('approved_actions') == original['approved_actions']


def test_same_input_produces_the_same_file_bytes(tmp_path):
    a = tmp_path / 'a.db'
    b = tmp_path / 'b.db'
    astore.load(a, stage_data(), run=RUN)
    astore.load(b, stage_data(), run=RUN)
    import hashlib
    assert hashlib.sha256(a.read_bytes()).hexdigest() == hashlib.sha256(b.read_bytes()).hexdigest()


def test_every_reference_in_an_item_becomes_an_edge_row(tmp_path):
    h, _ = build(tmp_path)
    refs = h.refs('D_P0001')
    assert {('fact', 'F_T_subnets'), ('fact', 'F_E_pricesModels'), ('criterion', 'AILZ-N-R5'), ('goal', 'G_1'), ('implication', 'I_network'), ('implication', 'I_cost')} <= {(r['kind'], r['id']) for r in refs['out']}


def test_reverse_lookup_finds_what_depends_on_a_fact(tmp_path):
    h, _ = build(tmp_path)
    users = h.used_by('F_T_subnets')
    assert {'O_P0002', 'D_P0001', 'J_ITEM_0001', 'A_ITEM_0001'} <= {u['id'] for u in users}
    assert {u['stage'] for u in users} == {'observation', 'direction', 'diagnosis', 'action'}


def test_chain_walks_from_an_action_down_to_its_facts(tmp_path):
    h, _ = build(tmp_path)
    chain = h.chain('A_ITEM_0001')
    ids = {c['id'] for c in chain}
    assert {'J_ITEM_0001', 'D_P0001', 'F_T_subnets', 'F_E_pricesModels'} <= ids


def test_get_returns_one_item_without_loading_the_whole_stage(tmp_path):
    h, _ = build(tmp_path)
    item = h.get('D_P0001')
    assert item['title'] == '방향 하나' and item['stage'] == 'direction'
    assert h.get('NOPE') is None


def test_list_is_bounded_and_filtered_by_stage_and_area(tmp_path):
    h, _ = build(tmp_path)
    assert [o['id'] for o in h.list('observation', area='network')] == ['O_P0001', 'O_P0002']
    assert [o['id'] for o in h.list('observation', area='cost')] == ['O_P0003']
    with pytest.raises(ValueError):
        h.list('nonsense')


def test_unresolved_items_are_kept_with_their_references(tmp_path):
    h, _ = build(tmp_path)
    u = h.unresolved('approved_areas')
    assert len(u) == 1 and u[0]['reason'] == '서브넷 용도 미확인' and u[0]['fact_refs'] == ['F_T_subnets']


def test_search_finds_text_across_stages(tmp_path):
    h, _ = build(tmp_path)
    assert {r['id'] for r in h.search('WAF-01')} == {'O_P0001'}
    assert h.search('"; drop table items;--') == []


def test_validation_reports_dangling_references_instead_of_hiding_them(tmp_path):
    data = stage_data()
    data['approved_actions']['actions'][0]['direction_refs'] = ['D_MISSING']
    path = tmp_path / 'x.db'
    astore.load(path, data, run=RUN)
    h = astore.open_analysis(path)
    rep = h.validate()
    assert {'item': 'A_ITEM_0001', 'ref': 'D_MISSING', 'kind': 'direction'} in rep['dangling']
    assert rep['ok'] is False


def test_validation_passes_on_consistent_data(tmp_path):
    h, _ = build(tmp_path)
    assert h.validate()['ok'] is True


def test_read_handle_cannot_write(tmp_path):
    h, path = build(tmp_path)
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        h._con.execute("insert into items(item_id) values('x')")


def test_a_run_mismatch_is_refused(tmp_path):
    data = stage_data()
    data['approved_areas']['run'] = dict(RUN, analysis_revision='other')
    with pytest.raises(ValueError, match='run_mismatch'):
        astore.load(tmp_path / 'y.db', data, run=RUN)


def test_duplicate_ids_are_refused(tmp_path):
    data = stage_data()
    data['approved_directions']['directions'].append(dict(data['approved_directions']['directions'][0]))
    with pytest.raises(ValueError, match='duplicate_id'):
        astore.load(tmp_path / 'z.db', data, run=RUN)


def test_access_is_logged_per_call(tmp_path):
    _, path = build(tmp_path)
    log = tmp_path / 'log.jsonl'
    h = astore.open_analysis(path, access_log=log, task='t')
    h.get('D_P0001')
    h.chain('A_ITEM_0001')
    fns = [json.loads(x)['function'] for x in log.read_text(encoding='utf-8').splitlines()]
    assert fns == ['get', 'chain']


def test_area_implication_ids_resolve_to_their_area_item(tmp_path):
    """실제 산출물에서 방향은 영역을 'I_network'처럼 영역 시사점 ID로 가리킨다. 영역 항목의 ID는 area_id(network)다."""
    h, _ = build(tmp_path)
    assert h.validate()['ok'] is True
    users = h.used_by('I_network')
    assert [u['id'] for u in users] == ['D_P0001']
    assert h.get('I_network')['area_id'] == 'network' and h.get('I_network')['stage'] == 'area'


def test_both_the_area_id_and_the_implication_id_find_the_area_item(tmp_path):
    h, _ = build(tmp_path)
    assert h.get('network')['headline'] == '네트워크 헤드라인'
    assert h.get('I_network')['headline'] == '네트워크 헤드라인'


def test_chain_from_a_direction_reaches_the_area_it_consolidates(tmp_path):
    h, _ = build(tmp_path)
    assert {'I_network', 'I_cost'} <= {c['id'] for c in h.chain('D_P0001')}


def test_reference_kinds_come_from_the_id_prefix(tmp_path):
    """I_ 접두는 영역 시사점이다. 종류를 잘못 읽으면 영역에서 방향으로의 사슬이 끊긴다."""
    h, _ = build(tmp_path)
    kinds = {r['id']: r['kind'] for r in h.refs('D_P0001')['out']}
    assert kinds['I_network'] == 'implication' and kinds['F_T_subnets'] == 'fact' and kinds['G_1'] == 'goal'
    edge_kinds = {c['id']: c['kind'] for c in h.chain('J_ITEM_0001')}
    assert edge_kinds['D_P0001'] == 'direction'


def test_read_handle_refuses_writes_through_its_own_connection(tmp_path):
    """연결 자체가 읽기 전용이어야 한다. 파일 권한(0444)만으로 막으면 파일을 쓸 수 있는 계정에서 뚫린다."""
    import sqlite3
    _, path = build(tmp_path)
    path.chmod(0o644)
    h = astore.open_analysis(path)
    with pytest.raises(sqlite3.OperationalError):
        h._con.execute("insert into meta values('x','y')")


def test_chain_terminates_on_a_reference_cycle(tmp_path):
    data = stage_data()
    # 방향이 진단을, 진단이 방향을 가리키는 순환
    data['approved_directions']['directions'][0]['diagnosis_refs'] = ['J_ITEM_0001']
    path = tmp_path / 'cycle.db'
    astore.load(path, data, run=RUN)
    h = astore.open_analysis(path)
    import signal

    def _timeout(signum, frame):
        raise TimeoutError('chain_did_not_terminate')
    old = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    try:
        ids = [c['id'] for c in h.chain('D_P0001')]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert len(ids) == len(set(ids)) and 'D_P0001' not in ids and 'J_ITEM_0001' in ids


def test_list_and_chain_never_return_more_than_the_cap(tmp_path):
    data = stage_data()
    many = [{'id': f'D_X{i:04d}', 'title': 't', 'text': 'x', 'customer_impact': 'x', 'change_direction': 'x', 'area_implication_refs': [],
             'criterion_refs': [], 'fact_refs': [f'F_T_{i}'], 'goal_refs': []} for i in range(astore.MAX_ROWS + 50)]
    data['approved_directions']['directions'] = many
    data['approved_directions']['edges'] = []
    data['approved_actions']['diagnoses'][0]['direction_refs'] = [m['id'] for m in many]
    data['approved_actions']['edges'] = []
    path = tmp_path / 'many.db'
    astore.load(path, data, run=RUN)
    h = astore.open_analysis(path)
    assert len(h.list('direction', limit=10_000)) == astore.MAX_ROWS
    assert len(h.chain('J_ITEM_0001', limit=10_000)) <= astore.MAX_ROWS
    assert len(h.used_by('G_1', limit=10_000)) <= astore.MAX_ROWS


def test_approved_relations_keep_the_kind_of_their_target(tmp_path):
    """승인된 관계(edges)는 'I_network consolidates D_P0001'처럼 영역 시사점에서 방향으로 간다. 도착지 종류는 ID 접두에서 정한다."""
    data = stage_data()
    data['approved_directions']['edges'] = [{'from_id': 'I_network', 'relation': 'consolidates', 'to_id': 'D_P0001'},
                                            {'from_id': 'F_T_subnets', 'relation': 'supports', 'to_id': 'D_P0001'}]
    path = tmp_path / 'rel.db'
    astore.load(path, data, run=RUN)
    import sqlite3
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    rows = {(r[0], r[2]): r[3] for r in con.execute("select from_id, relation, to_id, to_kind from edges where relation like 'edge:%'")}
    assert rows[('I_network', 'D_P0001')] == 'direction' and rows[('F_T_subnets', 'D_P0001')] == 'direction'
    assert astore._kind_of('I_network') == 'implication' and astore._kind_of('F_T_subnets') == 'fact' and astore._kind_of('X_1') == 'other'
