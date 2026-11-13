"""전송 입력 축소 변환기(asis_input_reducer) 시험. 품질 비교 실험 조건 C·D와 같은 변환을 코드로 고정한다.

계약:
- 정본 입력은 변경하지 않는다(사본에서만 줄인다).
- C(run 반복 제거): 사실마다 같은 run 객체를 context.fact_run 한 번으로 올리고 사실의 run은 제거한다. 값은 한 글자도 잃지 않고 복원 가능하다.
- D(미인용 큰 사실 구조 요약): 직접 인용 대상이 아닌 큰 행 목록 사실만 행 수·키·대표 1행·원본 해시 요약으로 바꾼다.
  직접 인용 대상(protected)은 어느 조건에서도 값을 그대로 둔다. 작은 사실·객체형 사실은 건드리지 않는다.
- 요약으로 바뀐 사실은 id·source_id·field_path·scope_complete·collection_status를 그대로 유지하고 value에 reduced 표시와 요약만 둔다.
- 줄인 결과는 항상 원본보다 작거나 같다. 줄일 수 없으면 그대로 돌려준다.
"""
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_input_reducer as R

RUN = {'assessment_id': 24, 'case_id': 'X', 'track': 'existing', 'source_revision': 'r1', 'analysis_revision': 'a1', 'source_bundle_sha256': 'a' * 64}


def rows(n, pad=200):
    return [{'id': 'R%d' % i, 'name': 'res-%d' % i, 'rule': 'allow-%d' % i, 'blob': 'x' * pad} for i in range(n)]


def data():
    facts = [
        {'id': 'F_BIG_CITED', 'source_id': 's', 'field_path': '/a', 'run': deepcopy(RUN), 'value': rows(60), 'scope_complete': True, 'collection_status': 'collected'},
        {'id': 'F_BIG_OTHER', 'source_id': 's', 'field_path': '/b', 'run': deepcopy(RUN), 'value': rows(80), 'scope_complete': False, 'collection_status': 'partial'},
        {'id': 'F_SMALL', 'source_id': 's', 'field_path': '/c', 'run': deepcopy(RUN), 'value': rows(3, 10), 'scope_complete': True, 'collection_status': 'collected'},
        {'id': 'F_OBJ', 'source_id': 's', 'field_path': '/d', 'run': deepcopy(RUN), 'value': {'k': 'v' * 30000}, 'scope_complete': True, 'collection_status': 'collected'},
        {'id': 'F_TEXT', 'source_id': 's', 'field_path': '/e', 'run': deepcopy(RUN), 'value': '인터뷰 전사 ' * 500, 'scope_complete': True, 'collection_status': 'collected'},
    ]
    return {'run': deepcopy(RUN), 'context': {'customer': 'c'}, 'facts': facts, 'criteria': [], 'goals': []}


def J(v):
    return len(json.dumps(v, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def test_the_input_is_never_modified():
    d = data(); before = json.dumps(d, ensure_ascii=False, sort_keys=True)
    R.reduce_input(d, level='D', protected={'F_BIG_CITED'})
    assert json.dumps(d, ensure_ascii=False, sort_keys=True) == before


def test_level_A_returns_the_same_content():
    d = data()
    assert R.reduce_input(d, level='A', protected=set()) == d


def test_level_C_moves_the_repeated_run_to_one_place_and_loses_nothing():
    d = data()
    out = R.reduce_input(d, level='C', protected=set())
    assert all('run' not in f for f in out['facts'])
    assert out['context']['fact_run'] == RUN
    # 복원하면 원본과 같다
    restored = R.restore_run(out)
    assert restored['facts'] == d['facts']
    assert J(out) < J(d)


def test_level_C_keeps_a_run_that_differs_from_the_common_one_on_that_fact():
    d = data(); d['facts'][2]['run'] = {**RUN, 'source_revision': 'other'}
    out = R.reduce_input(d, level='C', protected=set())
    assert out['facts'][2]['run']['source_revision'] == 'other', 'a fact whose run differs must keep its own run'
    assert R.restore_run(out)['facts'] == d['facts']


def test_level_D_summarizes_only_big_uncited_row_lists():
    d = data()
    out = R.reduce_input(d, level='D', protected={'F_BIG_CITED'})
    by = {f['id']: f for f in out['facts']}
    assert by['F_BIG_CITED']['value'] == d['facts'][0]['value'], 'cited facts keep their value'
    assert by['F_SMALL']['value'] == d['facts'][2]['value'] and by['F_OBJ']['value'] == d['facts'][3]['value'] and by['F_TEXT']['value'] == d['facts'][4]['value']
    other = by['F_BIG_OTHER']['value']
    assert other['reduced'] is True and other['row_count'] == 80 and other['sample_row'] == d['facts'][1]['value'][0]
    assert set(other['row_keys']) == {'id', 'name', 'rule', 'blob'}
    assert other['canonical_value_sha256'] and J(by['F_BIG_OTHER']) < J(d['facts'][1]) / 5


def test_a_summarized_fact_keeps_its_identity_and_completeness_fields():
    d = data()
    out = R.reduce_input(d, level='D', protected=set())
    f = next(x for x in out['facts'] if x['id'] == 'F_BIG_OTHER')
    assert (f['source_id'], f['field_path'], f['scope_complete'], f['collection_status']) == ('s', '/b', False, 'partial')


def test_the_summary_hash_identifies_the_canonical_value():
    d = data()
    from asis_model import digest
    out = R.reduce_input(d, level='D', protected=set())
    f = next(x for x in out['facts'] if x['id'] == 'F_BIG_OTHER')
    assert f['value']['canonical_value_sha256'] == digest(d['facts'][1]['value'])


def test_the_reduced_input_is_never_larger_than_the_original_and_unchanged_when_nothing_applies():
    d = data()
    for level in ('A', 'C', 'D'):
        assert J(R.reduce_input(d, level=level, protected={'F_BIG_CITED'})) <= J(d)
    small = {'run': deepcopy(RUN), 'context': {}, 'facts': [{'id': 'F1', 'source_id': 's', 'field_path': '/x', 'run': deepcopy(RUN), 'value': 1, 'scope_complete': True, 'collection_status': 'collected'}], 'criteria': [], 'goals': []}
    assert R.reduce_input(small, level='D', protected=set())['facts'][0]['value'] == 1


def test_an_unknown_level_is_refused():
    try:
        R.reduce_input(data(), level='Z', protected=set())
    except ValueError:
        return
    raise AssertionError('unknown level must be refused')


def test_protected_ids_are_never_summarized_at_any_level():
    d = data()
    ids = {f['id'] for f in d['facts']}
    out = R.reduce_input(d, level='D', protected=ids)
    assert [f['value'] for f in out['facts']] == [f['value'] for f in d['facts']]


def test_a_list_with_few_rows_is_never_summarized_even_when_its_bytes_are_large():
    """행이 적고 한 행이 큰 목록(예: 13행에 6만 바이트인 네트워크 보안 구성)은 한 행 안에 규칙·정책 같은 판단 근거가 있다.
    행 수가 임계 미만이면 바이트가 커도 구조 요약으로 바꾸지 않는다(요약은 대표 1행만 남겨 나머지 행의 근거를 잃는다)."""
    d = data()
    d['facts'].append({'id': 'F_FEW_FAT', 'source_id': 's', 'field_path': '/f', 'run': deepcopy(RUN),
                       'value': [{'name': 'nsg-%d' % i, 'rules': 'allow-ssh-from-anywhere ' * 400} for i in range(13)],
                       'scope_complete': True, 'collection_status': 'collected'})
    assert J(d['facts'][-1]['value']) > R.BIG_ROWS_MIN_BYTES and len(d['facts'][-1]['value']) < R.BIG_ROWS_MIN_ROWS
    out = R.reduce_input(d, level='D', protected=set())
    f = next(x for x in out['facts'] if x['id'] == 'F_FEW_FAT')
    assert f['value'] == d['facts'][-1]['value'], 'few-row lists keep every row'


def test_a_list_with_many_rows_but_small_bytes_is_never_summarized():
    d = data()
    d['facts'].append({'id': 'F_MANY_THIN', 'source_id': 's', 'field_path': '/g', 'run': deepcopy(RUN),
                       'value': [{'id': i} for i in range(300)], 'scope_complete': True, 'collection_status': 'collected'})
    assert len(d['facts'][-1]['value']) >= R.BIG_ROWS_MIN_ROWS and J(d['facts'][-1]['value']) < R.BIG_ROWS_MIN_BYTES
    out = R.reduce_input(d, level='D', protected=set())
    assert next(x for x in out['facts'] if x['id'] == 'F_MANY_THIN')['value'] == d['facts'][-1]['value']
