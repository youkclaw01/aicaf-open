"""사건·지식 저장소: 출처 없는 기억은 저장하지 않고, 충돌은 병합하지 않으며, 고객 폴더 밖으로 나가지 않는다."""
from pathlib import Path
import json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from asis_incidents import Store, KnowledgeError


def make(tmp_path):
    source = tmp_path / 'revision'
    source.mkdir()
    (source / 'evidence.json').write_text('{"error":"cross_run_previous_stage"}', encoding='utf-8')
    return Store(tmp_path / 'customer-24', source_root=source), source


def incident(store, **overrides):
    base = dict(kind='run-binding', subject='area-cost', symptoms=['cross_run_previous_stage 로 개정이 실패했다'],
                steps_that_worked=['모델이 쓴 run 값을 입력의 run으로 코드가 되돌렸다'], root_cause=['64자리 해시를 옮겨 쓰다 한 글자를 바꿨다'],
                pitfalls=['호출 수를 늘리면 같은 오류가 더 자주 난다'], sources=[{'path': 'evidence.json'}], resolution='resolved_by_code')
    base.update(overrides)
    return store.record_incident(**base)


def test_incident_is_stored_with_four_fields_and_verified_source_hash(tmp_path):
    store, _ = make(tmp_path)
    record = incident(store)
    assert set(record) >= {'symptoms', 'steps_that_worked', 'root_cause', 'pitfalls', 'sources'}
    assert len(record['sources'][0]['sha256']) == 64
    assert list((tmp_path / 'customer-24' / 'incidents').glob('*.json'))


@pytest.mark.parametrize('sources,error', [
    ([], 'source_required'),
    ([{'path': 'missing.json'}], 'source_file_missing'),
    ([{'path': '../outside.json'}], 'source_file_missing'),
    ([{'path': 'evidence.json', 'sha256': '0' * 64}], 'source_hash_mismatch'),
    ([{'bogus': 1}], 'source_shape'),
])
def test_memory_without_a_real_matching_source_is_rejected(tmp_path, sources, error):
    store, _ = make(tmp_path)
    with pytest.raises(KnowledgeError, match=error):
        incident(store, sources=sources)
    assert not list((tmp_path / 'customer-24' / 'incidents').glob('*.json')) if (tmp_path / 'customer-24' / 'incidents').exists() else True


def test_human_decision_id_is_an_accepted_source(tmp_path):
    store, _ = make(tmp_path)
    assert incident(store, sources=[{'decision_id': 'dec-0001'}], resolution='resolved_by_human')


def test_resolved_incident_needs_a_cause_or_steps(tmp_path):
    store, _ = make(tmp_path)
    with pytest.raises(KnowledgeError, match='resolved_incident_needs_cause_or_steps'):
        incident(store, steps_that_worked=[], root_cause=[])
    assert incident(store, steps_that_worked=[], root_cause=[], resolution='unresolved')


def test_same_subject_incidents_come_first(tmp_path):
    store, _ = make(tmp_path)
    incident(store, subject='area-ai', kind='other')
    incident(store, subject='area-cost')
    rows = store.similar_incidents(kind='run-binding', subject='area-cost')
    assert rows[0]['subject'] == 'area-cost'


def test_topic_conflict_is_not_merged_and_is_kept_for_confirmation(tmp_path):
    store, _ = make(tmp_path)
    store.write_topic('run-binding', '# run 해시\n모델이 해시를 틀리게 옮기면 코드가 입력의 run으로 되돌린다.', sources=[{'path': 'evidence.json'}])
    with pytest.raises(KnowledgeError, match='topic_conflict_needs_confirmation'):
        store.write_topic('run-binding', '# run 해시\n전혀 다른 주장.', sources=[{'path': 'evidence.json'}])
    assert 'run' in (tmp_path / 'customer-24' / 'knowledge' / 'run-binding.md').read_text(encoding='utf-8')
    assert list((tmp_path / 'customer-24' / 'knowledge' / 'conflicts').glob('*.json'))
    import hashlib
    current = (tmp_path / 'customer-24' / 'knowledge' / 'run-binding.md').read_text(encoding='utf-8')
    store.write_topic('run-binding', '# run 해시\n갱신된 내용.', sources=[{'path': 'evidence.json'}],
                      replaces=hashlib.sha256(current.encode('utf-8')).hexdigest())
    assert '갱신된 내용' in (tmp_path / 'customer-24' / 'knowledge' / 'run-binding.md').read_text(encoding='utf-8')


def test_overview_is_a_budgeted_index_and_topic_names_cannot_escape(tmp_path):
    store, _ = make(tmp_path)
    for n in range(80):
        store.write_topic('topic-%02d' % n, '# 주제 %d\n내용' % n, sources=[{'path': 'evidence.json'}])
    assert len(store.read_overview()) <= 2000 + 2
    for bad in ('../escape', 'a/b', 'overview', ''):
        with pytest.raises(KnowledgeError, match='topic_name'):
            store.write_topic(bad, 'x', sources=[{'path': 'evidence.json'}])


def test_search_returns_incidents_and_notes_with_sources_and_is_customer_isolated(tmp_path):
    a, _ = make(tmp_path)
    incident(a)
    other_root = tmp_path / 'other'
    other_root.mkdir()
    b = Store(tmp_path / 'customer-99', source_root=other_root)
    hits = a.search('cross_run_previous_stage 해시')
    assert hits and hits[0]['type'] == 'incident' and hits[0]['sources']
    assert b.search('cross_run_previous_stage 해시') == [], 'another customer must never see this record'
