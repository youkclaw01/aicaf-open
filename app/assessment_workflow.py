"""프로젝트별 세 보고서의 공통 순서·공식 지침 파생 문항·근거 바인딩 계약."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from asis_model import digest

SCHEMA = 'assessment-workflow/1'
REPORTS = (
    ('current_state', 'AS-IS 현황분석 보고서'),
    ('interview', '인터뷰 보고서'),
    ('integrated', '현황분석 종합 및 시사점 및 개선과제 보고서'),
)
REPORT_ORDER = [key for key, _ in REPORTS]


def current_state_input(original, *, assessment):
    """현황 입력을 실제 프로젝트 원천으로 다시 결합하고 인터뷰 진술을 제외한다."""
    data = deepcopy(original)
    if assessment['id'] != data['run']['assessment_id']:
        raise ValueError('current_state_cross_assessment')
    source_hash = digest({'files': data['context']['input_files'], 'assessment': assessment, 'answers': []})
    data['run'].update(source_revision=source_hash, source_bundle_sha256=source_hash)
    data['facts'] = [f for f in data['facts'] if not f['id'].startswith('F_IV_')]
    for fact in data['facts']:
        fact['run'] = deepcopy(data['run'])
    data['goals'] = []
    data['context']['report_kind'] = 'current_state'
    data['context']['goals_status'] = 'not_confirmed'
    data['context']['current_operations_verified'] = False
    for key in ('collection_states',):
        data['context'][key] = {k: v for k, v in data['context'].get(key, {}).items()
                                if not k.startswith('F_IV_')}
    data['context'].pop('goal_interpretations', None)
    return data


def approved(record, kind, aid, source_sha256):
    """저장소에서 실물·검토 증거를 확인한 레코드만 현재 선행 보고서로 인정한다."""
    return bool(isinstance(record, dict)
                and record.get('kind') == kind
                and record.get('assessment_id') == aid
                and record.get('source_sha256') == source_sha256
                and record.get('status') == 'ready'
                and record.get('content_checked') is True
                and record.get('render_checked') is True
                and record.get('files_verified') is True
                and isinstance(record.get('sha256'), str) and len(record['sha256']) == 64)


def answer_binding(questions, answers):
    """해당 질문의 실제 답변만 결합한다. 다른 버전·기존 문항 답변은 섞지 않는다."""
    return digest({q['id']: {k: answers.get(q['id'], {}).get(k) for k in ('transcript', 'summary', 'score')}
                   for q in questions})


def unanswered(questions, answers):
    """점수의 높고 낮음이 아니라 실제 응답·명시적인 미확인의 유무를 확인한다."""
    return [q['id'] for q in questions if not (
        str(answers.get(q['id'], {}).get('transcript') or '').strip()
        and str(answers.get(q['id'], {}).get('score') if answers.get(q['id'], {}).get('score') is not None else '').strip() in
        ('0', '1', '2', '3', '4', '미확인', '해당없음'))]


def describe(assessment, *, source_sha256, current_state, questionnaire, interview, integrated, answers):
    """화면과 서버에서 동일하게 사용하는 순서·차단·현재 위치를 계산한다."""
    aid = assessment['id']
    state_ready = approved(current_state, 'current_state', aid, source_sha256)
    qs = questionnaire.get('questions', []) if isinstance(questionnaire, dict) else []
    questionnaire_ready = bool(state_ready and qs and questionnaire.get('assessment_id') == aid
        and questionnaire.get('source_sha256') == source_sha256
        and questionnaire.get('current_state_report', {}).get('sha256') == current_state.get('sha256'))
    interview_ready = bool(questionnaire_ready and approved(interview, 'interview', aid, source_sha256)
        and interview.get('questionnaire_sha256') == digest(questionnaire)
        and interview.get('answers_sha256') == answer_binding(qs, answers)
        and not unanswered(qs, answers))
    integrated_ready = bool(interview_ready and approved(integrated, 'integrated', aid, source_sha256)
        and integrated.get('upstream', {}).get('current_state') == current_state.get('sha256')
        and integrated.get('upstream', {}).get('interview') == interview.get('sha256'))
    ready = [state_ready, interview_ready, integrated_ready]
    missing = unanswered(qs, answers) if qs else []
    permitted = [bool(source_sha256), bool(questionnaire_ready and not missing), interview_ready]
    reasons = ['', '현황분석 보고서의 내용·실렌더 검토와 해당 근거의 질문 구성이 먼저 완료되어야 합니다.',
               '동일 현황·질문 버전의 실제 인터뷰와 인터뷰 보고서를 먼저 완료해야 합니다.']
    if questionnaire_ready and missing:
        reasons[1] = f'실제 인터뷰 답변이 필요한 문항이 {len(missing)}개 남아 있습니다.'
    records = [current_state, interview, integrated]
    reports = []
    for index, (kind, name) in enumerate(REPORTS):
        record = records[index]
        running = isinstance(record, dict) and (record.get('status') in ('queued', 'running')
                    or record.get('job', {}).get('status') in ('queued', 'running'))
        reports.append({'kind': kind, 'name': name, 'ready': ready[index],
                        'prerequisites_ready': bool(permitted[index]),
                        'can_generate': bool(permitted[index] and not running),
                        'blocked_reason': reasons[index] if not permitted[index] else '',
                        'status': 'ready' if ready[index] else record.get('status', 'pending') if isinstance(record, dict) else 'pending',
                        'record': deepcopy(record)})
    current = ('collection' if not source_sha256 else 'current_state' if not state_ready else
               'questions' if not questionnaire_ready else 'interview' if not interview_ready else
               'integrated' if not integrated_ready else 'complete')
    return {'schema': SCHEMA, 'assessment_id': aid, 'report_order': list(REPORT_ORDER),
            'reports': reports, 'current_step': current, 'source_sha256': source_sha256,
            'questionnaire_ready': questionnaire_ready, 'unanswered': unanswered(qs, answers) if qs else [],
            'complete': integrated_ready}


# 이 연결은 자체 질문 설계다. Microsoft 공식 설문·공식 점수로 표시하지 않는다.
TOPICS = {
    '1': ('CAF-AI-STR1', '업무 문제·사용 사례와 성공 조건을 구체화한다.'),
    '2': ('CAF-AI-STR2', '기능·데이터·기술 선택과 적용 제약을 연결한다.'),
    '3': ('CAF-AI-STR3', '책임 있는 AI의 위험과 허용 범위를 확인한다.'),
    '4': ('CAF-AI-STR4', '사용할 데이터의 위치·품질·권리와 준비 상태를 확인한다.'),
    '5': ('CAF-AI-STR5', '도입·확산의 책임·역량·채택 조건을 확인한다.'),
}
GROUPS = {
    'B1': (['WAF-AI-APP1', 'WAF-AI-APP11'], 'AI와 기존 시스템의 기능·비기능 요구 및 적용 대상을 확인한다.'),
    'B2': (['CAF-AI-RDY4', 'WAF-AI-APP2'], '공통 기반과 애플리케이션 구조가 실제 업무에 적합한지 확인한다.'),
    'B3': (['WAF-AI-SE4', 'CAF-AI-RDY2'], '관측된 네트워크 경계와 실제 허용·거부 경로를 구분한다.'),
    'B4': (['WAF-AI-RE1', 'WAF-AI-PE1'], '업무별 용량·안정성·품질과 복구 요구를 확인한다.'),
    'B5': (['WAF-AI-OE2', 'WAF-AI-OE4'], '관측·변경·장애 대응의 실제 운영과 책임을 확인한다.'),
    'C1': (['WAF-AI-SE3', 'WAF-AI-SE6'], '사람·프로그램의 신원과 실제 접근 권한을 확인한다.'),
    'C2': (['WAF-AI-SE2', 'CAF-AI-MNG5'], '민감도에 맞는 데이터 사용·보호·보존을 확인한다.'),
    'C3': (['CAF-AI-STR3', 'WAF-AI-RAI5'], '부적절한 입력·출력의 위험과 안전 조치의 효과를 확인한다.'),
    'C4': (['CAF-AI-GOV2', 'CAF-AI-GOV3'], '정책의 책임·승인·예외와 실제 집행을 확인한다.'),
    'C5': (['CAF-AI-GOV4', 'CAF-AI-SEC3'], '감사·위험·보안 사건의 탐지와 후속 대응을 확인한다.'),
    'D1': (['WAF-AI-CO1', 'CAF-AI-MNG4'], '모델·검색·데이터·운영 비용의 범위와 원인을 확인한다.'),
    'D2': (['WAF-AI-CO1', 'WAF-AI-PE3'], '토큰·업무량·재시도와 단위 비용의 대응을 확인한다.'),
    'D3': (['WAF-AI-CO2', 'WAF-AI-CO3'], '필요한 성능·품질을 유지하는 자원·과금 선택을 비교한다.'),
    'D4': (['CAF-AI-MNG4', 'CAF-AI-GOV3'], '예산·사용 한도·배부·초과 조치와 책임을 확인한다.'),
    'D5': (['CAF-AI-STR1', 'WAF-AI-CO1'], '같은 업무·기간·범위의 비용과 효과로 투자 판단을 확인한다.'),
}
FACT_AREAS = {
    'B1': {'compute', 'ai'}, 'B2': {'compute', 'ai', 'operating'}, 'B3': {'network'},
    'B4': {'compute', 'ai', 'quality'}, 'B5': {'telemetry', 'operating'},
    'C1': {'secgov', 'ai'}, 'C2': {'data'}, 'C3': {'quality', 'ai'},
    'C4': {'secgov'}, 'C5': {'telemetry', 'secgov'},
    'D1': {'cost'}, 'D2': {'cost', 'ai'}, 'D3': {'cost', 'ai'}, 'D4': {'cost'}, 'D5': {'cost'},
}
FOLLOWUP = {
    'A': '이 답변을 확인할 수 있는 업무 담당자·자료·실제 사례는 무엇인가요? 모르는 내용은 미확인으로 남기겠습니다.',
    'B': '현황분석에서 확인한 구성은 어느 업무에서 실제로 사용하나요? 선택한 이유와 정상·장애 상황의 확인 결과를 설명해 주세요.',
    'C': '현황분석에 보이는 설정이 실제 사람·업무 프로그램·문서에도 적용되나요? 담당자·예외·확인 결과를 함께 설명해 주세요.',
    'D': '현황분석의 비용과 같은 대상·기간·통화를 기준으로, 업무량과 효과를 누가 어떻게 확인하나요? 추정과 실측을 구분해 주세요.',
}


@lru_cache(maxsize=1)
def official_criteria():
    """인용이 공식 조회본과 해시에 일치하는 기존 기준 카탈로그만 사용한다."""
    from asis_analysis import criteria
    return {row['id']: row for row in criteria()}


def derive_questions(data, current_state_report, *, current_analysis=None):
    """공통 기준과 현황의 관련 근거에서 실제 질문·후속 질문·평가 목적을 파생한다."""
    import questions
    from asis_evidence_views import areas_for
    track = data['run']['track']
    bank = questions.questions_for(track)
    official = official_criteria()
    facts = [f for f in data.get('facts', []) if not f['id'].startswith('F_IV_')]
    fact_bindings = {f['id']: digest({k: f.get(k) for k in
                    ('id', 'value', 'collection_status', 'scope_complete')}) for f in facts}
    output = []
    for original in bank:
        q = deepcopy(original)
        topic = q['topic'].removeprefix('X')
        if q['area'] == 'A':
            cid, meaning = TOPICS[topic[-1]]
            criterion_ids = [cid]
        else:
            criterion_ids, meaning = GROUPS[topic]
        guides = [{**deepcopy(official[cid]), 'guidance_ko': meaning,
                   'interpretation_is_not_official_score': True} for cid in criterion_ids]
        relevant_areas = FACT_AREAS.get(topic, set())
        selected = [f for f in facts if relevant_areas.intersection(areas_for(f['id'][4:]))]
        # 같은 구성의 인증·공개 접근은 네트워크·신원·AI·비용 판단의 공통 선행 사실이다.
        if topic in ('B1', 'B3', 'B4', 'C1', 'C3', 'D2', 'D3'):
            for f in facts:
                if f['id'] == 'F_T_foundryAccountConfiguration' and f not in selected:
                    selected.append(f)
        refs = [f['id'] for f in selected]
        context = [{'fact_id': f['id'], 'status': f.get('collection_status', 'not_collected'),
                    'scope_complete': f.get('scope_complete') is True,
                    'observed_records': len(f['value']) if isinstance(f.get('value'), list) else None}
                   for f in selected]
        actual = any(f.get('collection_status') in ('collected', 'not_present') for f in selected)
        reason = (meaning + ' 구성으로 확인할 수 없는 목표·실제 사용·책임·성과는 고객 원문으로 확인한다.'
                  if not refs else meaning + ' 관련 관측과 실제 업무 적용·선택 이유·작동 증거 사이의 공백을 확인한다.')
        extra = FOLLOWUP[q['area']]
        if refs and not actual:
            extra = '이 항목은 수집 범위에서 확인되지 않았습니다. 실제 운영 여부와 확인 가능한 담당자·자료를 알려 주세요.'
        followups = list(q.get('followups', []))
        if extra not in followups:
            followups.append(extra)
        account_fact = next((f for f in selected if f['id'] == 'F_T_foundryAccountConfiguration'), None)
        if account_fact and account_fact.get('collection_status') == 'collected' and isinstance(account_fact.get('value'), list):
            rows = [r.get('properties', r) for r in account_fact['value'] if isinstance(r, dict)]
            if topic == 'C1':
                blocked = [r for r in rows if r.get('disableLocalAuth') is True]
                allowed = [r for r in rows if r.get('disableLocalAuth') is False]
                if blocked:followups.append(f'수집 범위에서 키 인증 차단 설정이 확인된 계정은 {len(blocked)}개입니다. 이 대상의 실제 업무 프로그램은 어떤 신원으로 접속하며 누가 권한을 관리하나요?')
                if allowed:followups.append(f'키 인증이 허용된 설정의 계정은 {len(allowed)}개입니다. 실제로 어떤 인증 방식을 사용하고, 접근 권한과 비밀값은 누가 어떻게 관리하나요?')
            if topic == 'B3':
                blocked = [r for r in rows if r.get('publicNetworkAccess') == 'Disabled']
                if blocked:followups.append(f'공개 네트워크 접근 차단 설정이 확인된 계정은 {len(blocked)}개입니다. 현재 업무 프로그램의 실제 접속 경로와 허용·거부 확인 결과를 설명해 주세요.')
        related_analysis = []
        if current_analysis:
            for area in current_analysis['stages'][0]['areas']:
                if area['area_id'] in relevant_areas:
                    related_analysis.append({'area_id': area['area_id'], 'headline': area['headline'],
                        'observation_refs': [o['id'] for o in area['observations'] if set(o['fact_refs']).intersection(refs)]})
        q.update(origin_id=q['id'], guidelines=guides, criterion_refs=criterion_ids,
                 fact_refs=refs, observed_context=context, derivation_reason=reason,
                 related_analysis=related_analysis,
                 evaluation_use='실제 답변과 해당 현황을 대조하여 '+meaning,
                 followups=followups, source_kind='official_guideline_derived_custom_question')
        binding = digest({'question': q, 'facts': {fid: fact_bindings[fid] for fid in refs}})
        q['id'] = 'WF_'+q['origin_id'].replace('-', '_')+'_'+binding[:12]
        q['question_binding_sha256'] = binding
        output.append(q)
    return {'schema': 'assessment-derived-questionnaire/1',
            'assessment_id': data['run']['assessment_id'], 'track': track,
            'source_sha256': data['run']['source_bundle_sha256'],
            'current_state_report': deepcopy(current_state_report),
            'guidance_catalog_sha256': digest(list(official.values())),
            'questions': output, 'official_questionnaire': False,
            'interpretation': '공식 원문과 현황 근거에서 파생한 자체 문항이며 Microsoft 공식 설문·공식 점수가 아니다.'}
