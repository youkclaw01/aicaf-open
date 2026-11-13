"""근거 저장소(evidence.db) 적재·조회 시험. docs/EVIDENCE-STORE-DESIGN-V2.md 3~4절의 계약을 고정한다.

입력은 실제 수집 구조(평가 24의 facts·collection_basis)와 같은 모양의 작은 표본이다. 운영 데이터 대조는 별도 실측으로 한다.
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_evidence_store as store

RID = '/subscriptions/s/resourceGroups/rg/providers/microsoft.cognitiveservices/accounts/fd-a'


def canon(value):
    """수집기(PowerShell)와 같은 정규 직렬화. 포털이 레코드 ID를 재현하는 기준이다."""
    if value is None: return 'null'
    if value is True: return 'true'
    if value is False: return 'false'
    if isinstance(value, int): return str(value)
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15: return str(int(value))
        text = repr(value)
        if 'e' in text:
            m, e = text.split('e'); sign = '-' if e.startswith('-') else '+'
            return m + 'e' + sign + (e.lstrip('+-').lstrip('0') or '0')
        return text
    if isinstance(value, str):
        escapes = {'"': '\\"', '\\': '\\\\'}
        return '"' + ''.join(escapes.get(ch) or ('\\u%04x' % ord(ch) if ord(ch) < 0x20 else ch) for ch in value) + '"'
    if isinstance(value, list): return '[' + ','.join(canon(x) for x in value) + ']'
    return '{' + ','.join(canon(k) + ':' + canon(value[k]) for k in sorted(value, key=lambda s: s.encode('utf-8'))) + '}'


def rec_id(dataset_id, record):
    return 'R:' + dataset_id + ':' + hashlib.sha256(canon(record).encode('utf-8')).hexdigest()[:16]


def sample():
    run = {'assessment_id': 24, 'case_id': 'c', 'track': 'existing', 'source_revision': 'x', 'analysis_revision': '1', 'source_bundle_sha256': 'x'}
    foundry = [{'id': RID, 'name': 'fd-a', 'publicNetworkAccess': 'Disabled', 'localAuthenticationDisabled': True},
               {'id': RID + '2', 'name': 'fd-b', 'publicNetworkAccess': 'Enabled', 'localAuthenticationDisabled': False}]
    prices = [{'armRegionName': 'koreacentral', 'skuName': 'gpt-a', 'retailPrice': 0.000459},
              {'armRegionName': 'koreacentral', 'skuName': 'gpt-b', 'retailPrice': 1.516e-05},
              {'armRegionName': 'swedencentral', 'skuName': 'gpt-a', 'retailPrice': 7500.0}]
    ds = lambda i, kind, key, recs, **k: {'id': i, 'key': key, 'kind': kind, 'source_file': 'topology.json' if kind == 'configuration' else 'aicaf-evidence.json',
        'source_pointer': '/configurations/' + key if kind == 'configuration' else '/sections/' + key + '/records', 'source_status': 'complete',
        'collection_status': k.get('status', 'collected'), 'scope_complete': k.get('complete', True), 'observed_record_count': len(recs),
        'declared_record_count': len(recs), 'error_count': 0, 'errors': [], 'reason': '', 'records': recs, 'metadata': {'api': 'x'}, 'area_refs': k.get('areas', ['ai'])}
    datasets = [ds('DS_T_foundryAccountConfiguration', 'configuration', 'foundryAccountConfiguration', foundry, areas=['ai', 'network']),
                ds('DS_E_pricesModels', 'evidence', 'pricesModels', prices, areas=['cost']),
                ds('DS_E_alertRules', 'evidence', 'alertRules', [], status='not_collected', complete=False, areas=['telemetry'])]
    fact = lambda fid, src, ptr, val, st='collected', c=True: {'id': fid, 'source_id': src, 'field_path': ptr, 'run': run, 'value': val, 'collection_status': st, 'scope_complete': c}
    facts = [fact('F_T_foundryAccountConfiguration', 'topology.json', '/configurations/foundryAccountConfiguration', foundry),
             fact('F_E_pricesModels', 'aicaf-evidence.json', '/sections/pricesModels/records', prices),
             fact('F_E_alertRules', 'aicaf-evidence.json', '/sections/alertRules/records', None, 'not_collected', False),
             fact('F_IV_XA1-1_transcript', 'interview_snapshot', '/answers/XA1-1/transcript', '[고객] 서버가 관리 ID로 모델을 호출합니다.'),
             fact('F_IV_XA1-1_summary', 'interview_snapshot', '/answers/XA1-1/summary', '관리 ID로 호출'),
             fact('F_IV_XA1-1_score', 'interview_snapshot', '/answers/XA1-1/score', '3'),
             fact('F_IV_XB2-3_transcript', 'interview_snapshot', '/answers/XB2-3/transcript', '[고객] 공용 접근은 차단하고 사설 연결만 씁니다.')]
    basis = {'schema': 'ai-assessment-basis/v2', 'datasets': datasets, 'criterionMatches': [
        {'criterion_id': 'AILZ-I-R6', 'area': 'secgov', 'unit': 'resource', 'target_id': RID, 'target_name': 'fd-a', 'assessment_status': 'not_evaluated',
         'target_discovery_status': 'observed', 'dataset_refs': ['DS_T_foundryAccountConfiguration'],
         'record_refs': [{'dataset_id': 'DS_T_foundryAccountConfiguration', 'record_index': 0, 'source_pointer': '/configurations/foundryAccountConfiguration/0', 'association': 'explicit_resource_id_reference'}]}]}
    assessments = [{'id': 'CJ_1', 'criterion_id': 'AILZ-I-R6', 'status': 'partial', 'applicability_confirmed': True, 'fact_refs': ['F_T_foundryAccountConfiguration'],
                    'conclusion': '키 인증 비활성 확인', 'area_refs': ['secgov']}]
    criteria = [{'id': 'AILZ-I-R6', 'quote': 'Disable key based access', 'source_url': 'https://x', 'modality': 'should',
                 'mapping': {'area': 'secgov', 'unit': 'resource', 'configuration_keys': ['foundryAccountConfiguration'], 'evidence_sections': []}}]
    return {'run': run, 'facts': facts, 'basis': basis, 'criteria': criteria, 'assessments': assessments}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'evidence.db'
    s = sample()
    store.load(path, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    return path


def test_load_stores_every_dataset_record_fact_and_interview(db):
    s = sample()
    con = sqlite3.connect(db)
    assert con.execute('select count(*) from datasets').fetchone()[0] == len(s['basis']['datasets'])
    assert con.execute('select count(*) from records').fetchone()[0] == 5
    assert con.execute('select count(*) from facts').fetchone()[0] == len(s['facts'])
    assert con.execute('select count(*) from interview_items').fetchone()[0] == 4
    assert con.execute('select count(*) from interviews').fetchone()[0] == 2
    assert con.execute('select count(*) from criteria').fetchone()[0] == 1


def test_record_id_is_content_hash_of_canonical_json_and_matches_the_collector_rule(db):
    s = sample()
    con = sqlite3.connect(db)
    for d in s['basis']['datasets']:
        got = [r[0] for r in con.execute('select record_id from records where dataset_id=? order by seq', (d['id'],))]
        assert got == [rec_id(d['id'], r) for r in d['records']]


def test_collector_record_ids_are_used_when_present_and_must_agree_with_content(tmp_path):
    s = sample()
    d = s['basis']['datasets'][0]
    d['record_ids'] = [rec_id(d['id'], r) for r in d['records']]
    store.load(tmp_path / 'a.db', facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    d['record_ids'][0] = 'R:DS_T_foundryAccountConfiguration:0000000000000000'
    with pytest.raises(ValueError, match='record_id_mismatch'):
        store.load(tmp_path / 'b.db', facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])


def test_duplicate_content_gets_distinct_ids(tmp_path):
    s = sample()
    d = s['basis']['datasets'][1]
    d['records'] = [d['records'][0], d['records'][1], d['records'][0]]
    d['observed_record_count'] = d['declared_record_count'] = 3
    store.load(tmp_path / 'a.db', facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    ids = [r[0] for r in sqlite3.connect(tmp_path / 'a.db').execute("select record_id from records where dataset_id='DS_E_pricesModels' order by seq")]
    assert len(set(ids)) == 3 and ids[2].endswith('#2')


def test_load_rejects_a_fact_whose_record_count_disagrees_with_its_dataset(tmp_path):
    s = sample()
    s['facts'][1]['value'] = s['facts'][1]['value'][:2]
    with pytest.raises(ValueError, match='fact_dataset_count_mismatch:F_E_pricesModels'):
        store.load(tmp_path / 'a.db', facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])


def test_not_collected_fact_keeps_status_and_has_no_records(db):
    h = store.open_project(db)
    f = h.fact('F_E_alertRules')
    assert f['collection_status'] == 'not_collected' and f['scope_complete'] is False and f['record_count'] in (0, None)
    assert h.rows('F_E_alertRules', ['skuName'], {}, limit=5)['rows'] == []


def test_rows_returns_only_requested_fields_and_filters(db):
    h = store.open_project(db)
    out = h.rows('F_E_pricesModels', ['skuName', 'retailPrice'], {'armRegionName': ('=', 'koreacentral')}, limit=10)
    assert out['total'] == 2 and out['returned'] == 2
    assert all(set(r) == {'record_id', 'skuName', 'retailPrice'} for r in out['rows'])
    assert {r['skuName'] for r in out['rows']} == {'gpt-a', 'gpt-b'}


def test_rows_respects_the_limit_and_says_how_many_were_not_returned(db):
    h = store.open_project(db)
    out = h.rows('F_E_pricesModels', ['skuName'], {}, limit=1)
    assert out['returned'] == 1 and out['total'] == 3 and out['truncated'] is True


def test_rows_rejects_unknown_operators_and_fields_not_flattened(db):
    h = store.open_project(db)
    with pytest.raises(ValueError, match='operator_not_allowed'):
        h.rows('F_E_pricesModels', ['skuName'], {'skuName': ('; drop table records', 'x')}, limit=5)
    with pytest.raises(ValueError, match='unknown_field'):
        h.rows('F_E_pricesModels', ['no_such_field'], {}, limit=5)


def test_summary_gives_counts_and_value_distribution_without_the_rows(db):
    h = store.open_project(db)
    out = h.summary('DS_E_pricesModels')
    assert out['observed_count'] == 3 and out['collection_status'] == 'collected'
    assert out['fields']['armRegionName']['distinct'] == 2
    assert dict(out['fields']['armRegionName']['top']) == {'koreacentral': 2, 'swedencentral': 1}
    assert 'rows' not in out and 'records' not in out


def test_record_returns_the_original_body_by_id(db):
    h = store.open_project(db)
    s = sample()
    rid = rec_id('DS_E_pricesModels', s['basis']['datasets'][1]['records'][1])
    assert h.record([rid])[rid]['skuName'] == 'gpt-b'
    assert h.record(['R:DS_E_pricesModels:ffffffffffffffff']) == {}, 'unknown ids are reported by absence, not guessed'


def test_count_with_condition(db):
    h = store.open_project(db)
    assert h.count('DS_E_pricesModels', {'armRegionName': ('=', 'koreacentral')}) == 2
    assert h.count('DS_E_pricesModels', {'armRegionName': ('in', ['koreacentral', 'swedencentral'])}) == 3


def test_interview_returns_the_three_parts_and_search_finds_by_text(db):
    h = store.open_project(db)
    iv = h.interview('XA1-1')
    assert iv['transcript'].startswith('[고객]') and iv['summary'] == '관리 ID로 호출' and iv['score'] == '3'
    hits = h.search_interviews('공용 접근', limit=5)
    assert [x['interview_id'] for x in hits] == ['XB2-3']
    assert h.interview('NOPE') is None


def test_refs_returns_matches_with_record_ids_of_the_linked_rows(db):
    h = store.open_project(db)
    s = sample()
    out = h.refs('AILZ-I-R6')
    assert out['matches'][0]['target_id'] == RID
    assert out['matches'][0]['record_ids'] == [rec_id('DS_T_foundryAccountConfiguration', s['basis']['datasets'][0]['records'][0])]
    assert h.assessment('AILZ-I-R6')['status'] == 'partial'


def test_every_read_is_recorded_with_function_arguments_and_returned_size(tmp_path):
    s = sample()
    path = tmp_path / 'evidence.db'
    store.load(path, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    log = tmp_path / 'access-w1.jsonl'
    h = store.open_project(path, access_log=log, task='direction-D_P0001')
    h.rows('F_E_pricesModels', ['skuName'], {}, limit=2)
    h.interview('XA1-1')
    lines = [json.loads(x) for x in log.read_text(encoding='utf-8').splitlines()]
    assert [x['function'] for x in lines] == ['rows', 'interview']
    assert lines[0]['task'] == 'direction-D_P0001' and lines[0]['returned'] == 2 and lines[0]['chars'] > 0 and lines[0]['args']['fact_id'] == 'F_E_pricesModels'


def test_the_handle_cannot_reach_another_projects_database(tmp_path):
    s = sample()
    a, b = tmp_path / 'p1' / 'evidence.db', tmp_path / 'p2' / 'evidence.db'
    for p in (a, b):
        p.parent.mkdir()
        store.load(p, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    h = store.open_project(a)
    assert not hasattr(h, 'path') or h.path == a
    for forbidden in ('attach', 'database', 'connection', 'execute', 'sql', 'query'):
        assert not hasattr(h, forbidden), forbidden
    with pytest.raises(TypeError):
        h.rows('F_E_pricesModels', ['skuName'], {}, limit=1, project=str(b))


def test_the_database_is_read_only_after_load(db):
    con = sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("insert into facts(fact_id) values('X')")
    h = store.open_project(db)
    assert h.meta()['sha256'] == hashlib.sha256(Path(db).read_bytes()).hexdigest()


def test_loading_the_same_input_twice_produces_the_same_database_hash(tmp_path):
    s = sample()
    hashes = []
    for name in ('a', 'b'):
        p = tmp_path / (name + '.db')
        store.load(p, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
        hashes.append(hashlib.sha256(p.read_bytes()).hexdigest())
    assert hashes[0] == hashes[1], 'the recorded hash is the reuse key, so the load must be deterministic'


def test_loading_refuses_to_overwrite_an_existing_database(db):
    s = sample()
    with pytest.raises(FileExistsError):
        store.load(db, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])


def test_a_single_read_never_returns_more_than_the_row_cap_even_if_a_larger_limit_is_asked(tmp_path):
    s = sample()
    d = s['basis']['datasets'][1]
    d['records'] = [{'armRegionName': 'koreacentral', 'skuName': 'sku-%04d' % i, 'retailPrice': float(i)} for i in range(store.MAX_ROWS + 50)]
    d['observed_record_count'] = d['declared_record_count'] = len(d['records'])
    s['facts'][1]['value'] = d['records']
    path = tmp_path / 'big.db'
    store.load(path, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    out = store.open_project(path).rows('F_E_pricesModels', ['skuName'], {}, limit=10_000)
    assert out['returned'] == store.MAX_ROWS and out['total'] == store.MAX_ROWS + 50 and out['truncated'] is True


def test_the_handle_opens_the_file_read_only_so_it_cannot_write_even_by_mistake(db):
    h = store.open_project(db)
    with pytest.raises(sqlite3.OperationalError):
        h._con.execute("insert into facts(fact_id) values('X')")


def sample_with_orphan_facts():
    """수집기는 resources·relationships 같은 사실을 데이터셋 없이 사실로만 둔다(평가 24 실측: 사실 3개, 34만 자)."""
    s = sample()
    run = s['run']
    s['facts'].append({'id': 'F_T_resources', 'source_id': 'topology.json', 'field_path': '/resources', 'run': run, 'collection_status': 'collected',
                       'scope_complete': True, 'value': [{'id': RID, 'type': 'microsoft.cognitiveservices/accounts', 'location': 'koreacentral'},
                                                          {'id': RID + '2', 'type': 'microsoft.cognitiveservices/accounts', 'location': 'swedencentral'}]})
    s['facts'].append({'id': 'F_E_diagnosticSettingsExtended_parentCount', 'source_id': 'aicaf-evidence.json', 'field_path': '/sections/x/parentCount',
                       'run': run, 'collection_status': 'collected', 'scope_complete': True, 'value': 2})
    return s


def test_facts_without_a_dataset_are_still_stored_and_queryable(tmp_path):
    s = sample_with_orphan_facts()
    path = tmp_path / 'o.db'
    store.load(path, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    h = store.open_project(path)
    f = h.fact('F_T_resources')
    assert f['record_count'] == 2 and f['dataset_id'] == 'FACT:F_T_resources'
    out = h.rows('F_T_resources', ['location'], {'location': ('=', 'koreacentral')}, limit=10)
    assert out['total'] == 1 and out['rows'][0]['location'] == 'koreacentral'
    assert h.record([out['rows'][0]['record_id']])[out['rows'][0]['record_id']]['id'] == RID


def test_scalar_fact_value_is_kept_and_readable(tmp_path):
    s = sample_with_orphan_facts()
    path = tmp_path / 'o.db'
    store.load(path, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    assert store.open_project(path).fact_value('F_E_diagnosticSettingsExtended_parentCount') == 2


def test_every_fact_value_can_be_read_back_unchanged_from_the_database(tmp_path):
    s = sample_with_orphan_facts()
    path = tmp_path / 'o.db'
    store.load(path, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    h = store.open_project(path)
    for f in s['facts']:
        assert h.fact_value(f['id']) == f['value'], f['id']


def test_a_datetime_rewritten_by_the_collector_is_a_different_record_even_though_it_is_the_same_instant():
    """평가 24 실측: keyVaultApiVersionObservations의 coverageStart는 사실에서 '2026-07-13T17:32:22.4254680+00:00',
    수집기 데이터셋에서 '2026-07-14T02:32:22.425468+09:00'(PowerShell이 DateTime으로 파싱해 다시 씀)이다.
    같은 시각이지만 문자열이 다르므로 레코드 해시가 달라진다. 저장소는 두 표현을 합치지 않고 각각 보존한다.
    """
    a = {'coverageStart': '2026-07-13T17:32:22.4254680+00:00', 'eventCount': 2}
    b = {'coverageStart': '2026-07-14T02:32:22.425468+09:00', 'eventCount': 2}
    assert store.record_hash(a) != store.record_hash(b)
    assert rec_id('DS_T_x', a) == 'R:DS_T_x:' + store.record_hash(a), 'the test canonical form agrees with the store'


def test_summary_and_count_accept_the_dataset_id_returned_by_fact_for_fact_based_datasets(tmp_path):
    """평가 24 실측에서 발견: fact()가 돌려준 dataset_id('FACT:F_T_resources')를 summary()에 그대로 넘기면 실패했다."""
    s = sample_with_orphan_facts()
    path = tmp_path / 'o.db'
    store.load(path, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    h = store.open_project(path)
    dataset_id = h.fact('F_T_resources')['dataset_id']
    assert h.summary(dataset_id)['observed_count'] == 2
    assert h.count(dataset_id, {'location': ('=', 'koreacentral')}) == 1
