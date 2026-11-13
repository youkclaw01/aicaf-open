"""프로젝트별 AS-IS 근거 정규화. 원본·미수집·부분수집을 보존하며 r2 run에 결합한다."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ASSET = Path(__file__).resolve().parent / 'asis_assets'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def strict_json(raw):
    return json.loads(raw.decode('utf-8-sig'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non_finite_json')))


def normalize(source, assessment, answers, revision, requirements='', classification='case_snapshot'):
    """실제 현재 필드와 수집 상태만 r2 입력으로 변환한다. 판정·고객 목표는 임의 보충하지 않는다."""
    aid = assessment['id']
    if isinstance(aid, bool) or not isinstance(aid, int) or aid < 1 or assessment.get('track') not in ('new', 'existing'):
        raise ValueError('invalid_assessment_scope')
    case_id = assessment.get('infra_case_id')
    if not isinstance(case_id, str) or not case_id:
        raise ValueError('unbound_case')
    source = Path(source)
    loaded, files, file_states = {}, {}, {}
    for key, name in [('topology', 'topology.json'), ('evidence', 'aicaf-evidence.json')]:
        path = source / name
        try:
            raw = path.read_bytes()
            value = strict_json(raw)
            if not isinstance(value, dict):
                raise ValueError('invalid_source_root')
            loaded[key] = value
            files[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
            file_states[key] = 'collected'
        except FileNotFoundError:
            loaded[key] = {}; file_states[key] = 'not_collected'
        except PermissionError:
            loaded[key] = {}; file_states[key] = 'permission_denied'
        except (ValueError, UnicodeError, OSError):
            loaded[key] = {}; file_states[key] = 'collection_failed'
    # 공식 수집 패키지는 출처/원문으로 보존하되 WARA 권고를 판정 권위로 가져오지 않는다.
    for path in sorted(source.glob('WARA-File*.json')):
        raw = path.read_bytes()
        value = strict_json(raw)
        loaded[path.name] = value
        files[path.name] = {'sha256':hashlib.sha256(raw).hexdigest(), 'bytes':len(raw),
                            'role':'collection_source_not_assessment_judgment'}
    if not files:
        raise ValueError('no_collected_source')
    vector = {'files': files, 'assessment': assessment, 'answers': answers}
    source_hash = digest(vector)
    run = {'assessment_id': aid, 'case_id': case_id, 'track': assessment['track'],
           'source_revision': source_hash, 'analysis_revision': str(revision), 'source_bundle_sha256': source_hash}
    facts, measurements, states = [], [], {}

    def add(fid, source_id, pointer, value, state, complete):
        if state in ('not_collected', 'permission_denied', 'collection_failed'):
            value = None; complete = False
        if state == 'not_present' and not complete:
            state = 'collection_failed'; value = None
        facts.append({'id': fid, 'source_id': source_id, 'field_path': pointer, 'run': deepcopy(run),
                      'value': value, 'collection_status': state, 'scope_complete': bool(complete)})
        states[fid] = {'status': state, 'complete': bool(complete)}
        if isinstance(value, list) and state in ('collected', 'not_present'):
            measurements.append({'id': 'M_' + fid, 'value': len(value), 'unit': 'observed_records', 'fact_refs': [fid]})

    topo, ev = loaded['topology'], loaded['evidence']
    resources = topo.get('resources')
    inv = (topo.get('verification') or {}).get('resourceCount', {})
    inv_complete = inv.get('complete') is True and isinstance(resources, list) and inv.get('collected') == len(resources) and inv.get('uniqueIds') == len(resources)
    add('F_T_resources', 'topology.json', '/resources', resources, 'collected' if isinstance(resources, list) else file_states['topology'] if file_states['topology'] != 'collected' else 'not_collected', inv_complete)
    cfg = topo.get('configurations') or {}
    details = (topo.get('coverage') or {}).get('detailDatasets') or {}
    for key, value in cfg.items():
        if not isinstance(value, list):
            continue
        coverage = details.get(key) or {}
        complete = coverage.get('status') in ('complete', 'observed-empty') and coverage.get('recordCount') == len(value)
        state = 'not_present' if complete and not value else 'collected'
        if coverage.get('status') in ('permission-denied', 'permission_denied'):
            state = 'permission_denied'
        elif coverage.get('status') in ('failed', 'collection_failed'):
            state = 'collection_failed'
        add('F_T_' + key, 'topology.json', '/configurations/' + key, value, state, complete)
    sections = ev.get('sections') or {}
    for key, section in sections.items():
        if not isinstance(section, dict):
            continue
        records = section.get('records')
        original_state = section.get('status')
        errors = section.get('errorCount', 0)
        rows_reconciled = original_state in ('collected', 'not_present') and errors == 0 and isinstance(records, list) and section.get('recordCount') == len(records)
        # 응답 행 수 일치는 페이지·부모·관측 기간의 전 범위 완전성이 아니다.
        complete = rows_reconciled and section.get('scope_complete') is True
        if any(section.get(k) for k in ('continuationRemaining', 'nextLink', 'truncated', 'rowLimitReached', 'parentEnumerationFailed')):
            complete = False
        api = str(section.get('api') or '')
        metric_empty = original_state == 'not_present' and not records and 'metrics' in api.casefold() and section.get('reason') != 'no-provisioned-deployments'
        if metric_empty:
            state = 'not_collected'; complete = False
        elif original_state in ('permission_denied', 'collection_failed'):
            state = original_state
        elif not isinstance(records, list):
            state = 'not_collected'
        elif errors and not records:
            state = 'collection_failed'
        elif original_state == 'not_present' and complete:
            state = 'not_present'
        else:
            state = 'collected'
        add('F_E_' + key, 'aicaf-evidence.json', '/sections/' + key + '/records', records, state, complete)
        if isinstance(section.get('parentCount'), int) and not isinstance(section['parentCount'], bool):
            add('F_E_' + key + '_parentCount', 'aicaf-evidence.json', '/sections/' + key + '/parentCount', section['parentCount'], 'collected', complete)
        states['F_E_' + key].update(original_status=original_state, error_count=errors, reason=section.get('reason') or '',
                                   api=api, purpose=section.get('purpose') or '', rows_reconciled=rows_reconciled,
                                   declared_scope_complete=section.get('scope_complete'))
    dataset_index = []
    for fid, source_id, pointer, value in [('F_T_resources', 'topology.json', '/resources', resources),
            ('F_T_relationships', 'topology.json', '/relationships', topo.get('relationships'))]:
        if fid == 'F_T_relationships' and isinstance(value, list):
            add(fid, source_id, pointer, value, 'collected', topo.get('verification', {}).get('referencesValid') is True)
    for fact in facts:
        state = states[fact['id']]
        dataset_index.append({'fact_id':fact['id'], 'source_id':fact['source_id'], 'pointer':fact['field_path'],
            'observed_records':len(fact['value']) if isinstance(fact['value'],list) else None,
            'status':fact['collection_status'], 'scope_complete':fact['scope_complete'],
            'metadata':deepcopy(state)})
    answer_rows = list(answers.values()) if isinstance(answers, dict) else list(answers)
    for row in answer_rows:
        qid = row.get('qid')
        if not isinstance(qid, str) or not qid:
            continue
        # 구두 원문과 점수는 별도 사실이다. 목표/조직 운영의 단정으로 자동 승격하지 않는다.
        for key in ('transcript', 'summary', 'score'):
            value = row.get(key)
            add('F_IV_' + qid + '_' + key, 'interview_snapshot', '/answers/' + qid + '/' + key,
                value, 'collected' if value not in (None, '') else 'not_collected', value not in (None, ''))
    context = {'customer': assessment['customer'], 'title': assessment.get('title') or '',
               'classification': classification, 'requirements': requirements, 'input_files': files,
               'file_states': file_states, 'collection_states': states, 'dataset_index': dataset_index,
               'source_documents':deepcopy(loaded), 'cost_period':deepcopy(ev.get('costPeriod') or {}),
               'topology_collected_at': (topo.get('collection') or {}).get('completedAtUtc'),
               'evidence_collected_at': ev.get('completedAtUtc'),
               'topology_scope': (topo.get('collection') or {}).get('subscriptions') or [],
               'evidence_subscription': ev.get('subscriptionId'),
               'known_limitations': (topo.get('coverage') or {}).get('knownLimitations') or [],
               'goals_status': 'not_confirmed', 'current_operations_verified': False}
    if ev.get('assessmentBasis') is not None or ev.get('assessmentBasisWorkbook') is not None:
        from asis_collection_basis import extract
        if file_states.get('topology')!='collected':raise ValueError('collection_basis_source_binding')
        context.update(extract(ev,(source/'topology.json').read_bytes()))
    subscriptions = {row.get('id', '').lower() for row in context['topology_scope'] if isinstance(row, dict)}
    if subscriptions and context['evidence_subscription'] and context['evidence_subscription'].lower() not in subscriptions:
        raise ValueError('cross_subscription_source')
    data = {'run': run, 'context': context, 'goals': [], 'facts': facts, 'measurements': measurements,
            'criteria': [], 'judgments': [], 'allocated_ids': {}, 'stage_data': {}}
    from asis_evidence_views import enrich
    data = enrich(data)
    # 인터뷰 원문·요약·점수의 기존 순서를 보존하며 집계 사실은 앞에 둔다.
    data['facts'] = [f for f in data['facts'] if not f['id'].startswith('F_IV_')] + [f for f in data['facts'] if f['id'].startswith('F_IV_')]
    return data
