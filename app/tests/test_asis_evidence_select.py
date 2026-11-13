"""근거 선택(asis_evidence_select)과 저장소 find 시험. docs/EVIDENCE-STORE-DESIGN-V2.md 4·5절의 계약."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_evidence_select as sel
import asis_evidence_store as store
from test_asis_evidence_store import sample


def make(tmp_path, big_rows=0):
    """표본에 큰 목록형 구성 사실 하나를 더한다. 수집기와 같은 모양: facts의 값과 basis.datasets의 records가 같다."""
    s = sample()
    if big_rows:
        rows = [{'id': f'/subscriptions/x/subnets/sub-{i:04d}', 'name': f'SUB-{i:04d}', 'purpose': 'filler ' * 40} for i in range(big_rows)]
        base = s['facts'][0]
        s['facts'].append({**base, 'id': 'F_T_bigList', 'field_path': '/configurations/bigList', 'value': rows})
        s['basis']['datasets'].append({
            'id': 'DS_T_bigList', 'key': 'bigList', 'kind': 'configuration', 'source_file': 'topology.json',
            'source_pointer': '/configurations/bigList', 'source_status': 'complete', 'collection_status': 'collected',
            'scope_complete': True, 'observed_record_count': len(rows), 'declared_record_count': len(rows), 'error_count': 0,
            'errors': [], 'reason': '', 'records': rows, 'area_refs': ['network']})
    path = tmp_path / 'e.db'
    store.load(path, facts=s['facts'], basis=s['basis'], criteria=s['criteria'], assessments=s['assessments'], run=s['run'])
    return store.open_project(path), {f['id']: f for f in s['facts']}


def test_find_returns_record_ids_whose_values_contain_the_token(tmp_path):
    h, facts = make(tmp_path, big_rows=60)
    ds = h.fact('F_T_bigList')['dataset_id']
    ids = h.find(ds, 'sub-0007')
    assert len(ids) == 1
    assert list(h.record(ids).values())[0]['name'] == 'SUB-0007'


def test_find_is_bounded_and_scoped_to_the_dataset(tmp_path):
    h, facts = make(tmp_path, big_rows=60)
    ds = h.fact('F_T_bigList')['dataset_id']
    assert len(h.find(ds, 'filler', limit=5)) == 5
    other = h.fact('F_T_foundryAccountConfiguration')['dataset_id'] if h.fact('F_T_foundryAccountConfiguration') else None
    if other:
        assert h.find(other, 'sub-0007') == []


def test_find_treats_the_token_as_a_literal_not_as_a_search_expression(tmp_path):
    h, _ = make(tmp_path, big_rows=10)
    ds = h.fact('F_T_bigList')['dataset_id']
    # FTS 연산자·따옴표·별표가 들어와도 오류 없이 빈 결과 또는 리터럴 일치만 돌려준다
    for hostile in ('sub-0001" OR "1', '*', 'NEAR(a b)', '"', "x'; drop table records;--"):
        assert isinstance(h.find(ds, hostile), list)
    assert h.count(ds) == 10


def test_small_facts_are_sent_whole(tmp_path):
    h, facts = make(tmp_path)
    out = sel.select_fact(h, 'F_T_foundryAccountConfiguration', facts['F_T_foundryAccountConfiguration']['value'], 'anything')
    assert out['mode'] == 'whole'


def test_interview_facts_are_always_whole_even_when_large():
    out = sel.select_fact(None, 'F_IV_XA1-1_transcript', ['x' * 50_000], 'x')
    assert out['mode'] == 'whole'


def test_large_list_is_trimmed_to_a_summary_and_the_rows_the_candidate_names(tmp_path):
    h, facts = make(tmp_path, big_rows=400)
    big = facts['F_T_bigList']['value']
    assert sel._size(big) > sel.WHOLE_LIMIT
    out = sel.select_fact(h, 'F_T_bigList', big, '후보는 sub-0042 와 SUB-0300 을 다룬다')
    assert out['mode'] == 'summary_and_matched_rows'
    names = {r['name'] for r in out['rows']}
    assert {'SUB-0042', 'SUB-0300'} <= names
    assert out['total_rows'] == 400 and out['sent_rows'] == len(out['rows']) <= sel.ROW_LIMIT
    assert sel._size(out) < sel._size(big) / 5


def test_trimmed_result_reports_how_much_was_left_out(tmp_path):
    h, facts = make(tmp_path, big_rows=400)
    out = sel.select_facts(h, ['F_T_bigList'], facts, 'sub-0001')
    m = sel.measure(out)
    assert m['trimmed'] == ['F_T_bigList'] and m['rows_total_of_trimmed'] == 400 and m['rows_sent'] < 400


def test_candidate_without_identifiers_sends_summary_only_not_a_random_slice(tmp_path):
    h, facts = make(tmp_path, big_rows=400)
    out = sel.select_fact(h, 'F_T_bigList', facts['F_T_bigList']['value'], '일반적인 서술이며 식별자가 없다')
    assert out['mode'] == 'summary_and_matched_rows' and out['rows'] == [] and out['summary']['observed_count'] == 400


def test_selection_is_deterministic(tmp_path):
    h, facts = make(tmp_path, big_rows=400)
    a = sel.select_facts(h, ['F_T_bigList'], facts, 'sub-0010 sub-0020 sub-0030')
    b = sel.select_facts(h, ['F_T_bigList'], facts, 'sub-0010 sub-0020 sub-0030')
    assert a == b


def test_identifier_tokens_keep_only_identifier_like_words():
    t = sel.identifier_tokens('Application Gateway AG-HUB-01 은 WAF-01 과 firewallPolicyId, state, mode 를 쓴다')
    assert 'ag-hub-01' in t and 'waf-01' in t and 'firewallpolicyid' in t
    assert 'state' not in t and 'mode' not in t and 'application' not in t


def test_selection_reads_only_through_the_project_handle(tmp_path):
    h, facts = make(tmp_path, big_rows=400)
    log = tmp_path / 'log.jsonl'
    h2 = store.open_project(h.path, access_log=log, task='select')
    sel.select_facts(h2, ['F_T_bigList'], facts, 'sub-0042')
    fns = {json.loads(x)['function'] for x in log.read_text(encoding='utf-8').splitlines()}
    assert fns <= {'fact', 'find', 'record', 'summary'}


def test_row_cap_holds_when_the_candidate_names_far_more_rows_than_the_cap(tmp_path):
    """후보가 행 100개의 이름을 모두 말해도 모델 입력의 행은 ROW_LIMIT을 넘지 않는다."""
    h, facts = make(tmp_path, big_rows=400)
    text = ' '.join(f'sub-{i:04d}' for i in range(100))
    out = sel.select_fact(h, 'F_T_bigList', facts['F_T_bigList']['value'], text)
    assert out['mode'] == 'summary_and_matched_rows'
    assert out['sent_rows'] == len(out['rows']) == sel.ROW_LIMIT


def test_row_cap_argument_overrides_the_default(tmp_path):
    h, facts = make(tmp_path, big_rows=400)
    text = ' '.join(f'sub-{i:04d}' for i in range(30))
    out = sel.select_fact(h, 'F_T_bigList', facts['F_T_bigList']['value'], text, row_limit=7)
    assert out['sent_rows'] == 7
