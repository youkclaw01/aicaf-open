"""AS-IS 원장 경계 시험: 보관 실측 사본과 메모리 내 오류 변형만 사용한다."""
from pathlib import Path
import importlib.util
import json
import sys

APP = Path(__file__).resolve().parents[1]
from private_archive_source import SOURCE,PRIVATE_ARCHIVE_REQUIRED
pytestmark=PRIVATE_ARCHIVE_REQUIRED
sys.path.insert(0, str(APP))


def test_collection_errors_cannot_become_confirmed_absence():
    assert importlib.util.find_spec('asis_model') is not None, 'portable normalizer missing'
    import asis_model
    assessment = json.loads((SOURCE/'interview_E.json').read_text(encoding='utf-8'))['assessment']
    data = asis_model.normalize(SOURCE, assessment, [], 'r-test', classification='archived_test_snapshot')
    fact = next(f for f in data['facts'] if f['id'] == 'F_E_aiCapabilityHosts')
    assert fact['collection_status'] == 'collection_failed'
    assert fact['value'] is None
    assert fact['scope_complete'] is False
    assert data['run']['assessment_id'] == assessment['id']
    assert data['run']['track'] == assessment['track']
    assert data['context']['classification'] == 'archived_test_snapshot'
    assert all(f['run'] == data['run'] for f in data['facts'])


def test_analysis_candidate_uses_real_graph_and_does_not_award_quality_pass():
    import asis_model
    assessment = json.loads((SOURCE/'interview_E.json').read_text(encoding='utf-8'))['assessment']
    data = asis_model.normalize(SOURCE, assessment, [], 'r-test', classification='archived_test_snapshot')
    assert importlib.util.find_spec('asis_analysis') is not None, 'r2 analysis adapter missing'
    import asis_analysis
    result = asis_analysis.analyze(data)
    assert result['quality_status'] == 'not_independently_reviewed'
    assert result['model_call_performed'] is False
    assert len(result['stages']) == 4
    assert result['stages'][0]['run'] == data['run']
    assert result['stages'][0]['areas']
    facts = {f['id'] for f in data['facts']}
    assert all(set(d['fact_refs']) <= facts for d in result['stages'][1]['directions'])
    assert result['stages'][2]['actions']
    assert any(a['change_type'] == 'verify' for a in result['stages'][2]['actions'])
    assert any('UDR' in o['text'] for a in result['stages'][0]['areas'] for o in a['observations'])


def test_changed_configuration_changes_implication_not_only_counts():
    import copy
    import asis_model, asis_analysis
    assessment = json.loads((SOURCE/'interview_E.json').read_text(encoding='utf-8'))['assessment']
    data = asis_model.normalize(SOURCE, assessment, [], 'r-test', classification='archived_test_snapshot')
    changed = copy.deepcopy(data)
    for fact in changed['facts']:
        if fact['id'] == 'F_T_foundryAccountConfiguration':
            for row in fact['value']:
                row['publicNetworkAccess'] = 'Enabled'
    first = asis_analysis.analyze(data)['stages'][0]['areas'][0]['implication']['text']
    second = asis_analysis.analyze(changed)['stages'][0]['areas'][0]['implication']['text']
    assert first != second, 'same conclusion for changed access boundary'
