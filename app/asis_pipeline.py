"""AS-IS 제품 작성 파이프라인. 코드 원장과 MAF 의미 해석·독립 검토를 분리한다."""
from __future__ import annotations

from copy import deepcopy
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re

import asis_analysis
from asis_model import ASSET, digest, encoded, strict_json


DOMAINS = tuple(asis_analysis.AREA_NAMES)


def bind_intents(original, payload):
    """실제 인터뷰의 연속 인용과 추가 요구만 결합하고 운영 사실은 변경하지 않는다."""
    if not isinstance(payload, dict) or set(payload) != {'goals', 'requirements'}:
        raise ValueError('invalid_intent_response')
    data = deepcopy(original)
    facts = {row['id']: row for row in data['facts']}
    goals, interpretations = [], []
    for row in payload['goals']:
        if not isinstance(row, dict):
            raise ValueError('invalid_goal')
        source = facts.get(row.get('source_fact_id'))
        quote = row.get('quote')
        if (not source or not source['id'].startswith('F_IV_') or not source['id'].endswith('_transcript')
                or source['collection_status'] != 'collected' or not isinstance(source['value'], str)
                or not isinstance(quote, str) or not quote.strip() or quote not in source['value']):
            raise ValueError('goal_quote_not_in_source')
        areas = row.get('area_refs')
        if not isinstance(areas, list) or set(areas) - set(DOMAINS):
            raise ValueError('invalid_goal_area')
        interpretation = row.get('interpretation')
        if not isinstance(interpretation, str) or not interpretation.strip():
            raise ValueError('empty_goal_interpretation')
        identifier = 'G_' + digest({'source': source['id'], 'quote': quote})[:16]
        if any(item['id'] == identifier for item in goals):
            raise ValueError('duplicate_goal_quote')
        goals.append({'id': identifier, 'source_id': source['id'], 'quote': quote})
        interpretations.append({'goal_id': identifier, 'interpretation': interpretation,
            'area_refs': areas, 'ambiguity': row.get('ambiguity', ''),
            'source_fact_id': source['id'], 'operating_fact_verified': False})
    raw = data['context'].get('requirements', '')
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    requirements = []
    for row in payload['requirements']:
        if not isinstance(row, dict) or row.get('quote') not in lines:
            raise ValueError('requirement_quote_not_in_source')
        areas = row.get('area_refs')
        if not isinstance(areas, list) or set(areas) - set(DOMAINS):
            raise ValueError('invalid_requirement_area')
        if row.get('status') not in ('applied_as_analysis_constraint', 'requires_interpretation'):
            raise ValueError('invalid_requirement_status')
        if not isinstance(row.get('interpretation'), str) or not row['interpretation'].strip():
            raise ValueError('empty_requirement_interpretation')
        requirements.append({'id': 'REQ_'+str(len(requirements)+1), 'quote': row['quote'],
            'area_refs': areas, 'status': row['status'], 'interpretation': row['interpretation'],
            'action_refs': []})
    if sorted(item['quote'] for item in requirements) != sorted(lines):
        raise ValueError('requirement_coverage_mismatch')
    data['goals'] = goals
    data['context'].update(current_operations_verified=False,
        goals_status='interpreted_from_interview_not_operationally_verified' if goals else 'not_confirmed',
        goal_interpretations=interpretations,
        requirements_application={'schema': 'asis-requirements-application/2',
            'source_sha256': digest(raw), 'customer_fact_promoted': False, 'items': requirements})
    return data


# 작성·검토 단계 재사용 키의 코드 바인딩 버전. 작성 결과의 의미가 달라지는 변경(프롬프트 외의 검증 규칙 변경 등)을 했을 때만 올린다.
STAGE_REUSE_CODE_BINDING = 'stage-reuse-v1'


EVIDENCE_REFS_RULE = ('결함의 evidence_refs에는 입력으로 제공된 facts·criteria·goals·judgments의 ID만 쓴다. '
                      '종합 방향(D_로 시작)·영역 시사점(I_로 시작)·진단·과제 ID는 근거 ID가 아니므로 evidence_refs에 쓰지 않고, '
                      '그 방향을 where의 위치와 why의 설명으로만 가리킨다. 근거로 쓸 ID가 없으면 evidence_refs는 빈 배열 []로 둔다.')


def strip_unknown_evidence_refs(data, review, folder):
    """검토 응답의 defects[*].evidence_refs 중 입력(facts·criteria·goals·judgments·measurements)에 없는 ID를 제거한다. 제거 내역은 저장한다."""
    if not isinstance(review, dict) or not isinstance(review.get('defects'), list):
        return review
    known = {row['id'] for key in ('facts', 'criteria', 'goals', 'judgments') for row in data.get(key, []) if isinstance(row, dict) and isinstance(row.get('id'), str)}
    removed = []
    cleaned = deepcopy(review)
    for position, defect in enumerate(cleaned['defects']):
        refs = defect.get('evidence_refs') if isinstance(defect, dict) else None
        if not isinstance(refs, list):
            continue
        kept = [ref for ref in refs if isinstance(ref, str) and ref in known]
        dropped = [ref for ref in refs if not (isinstance(ref, str) and ref in known)]
        if dropped:
            removed.append({'defect': position, 'where': defect.get('where'), 'dropped': dropped})
            defect['evidence_refs'] = kept
    if removed:
        from asis_model import encoded
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / 'evidence-refs-stripped.json').write_bytes(encoded({'schema': 'asis-evidence-refs-stripped/1', 'removed': removed}))
    return cleaned


def history_path(path):
    """Windows 설정을 변경하지 않고 긴 보완 원장 경로를 네이티브 파일 I/O에 전달한다."""
    value = os.path.abspath(os.fspath(path))
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = ('\\\\?\\UNC\\' + value[2:]) if value.startswith('\\\\') else ('\\\\?\\' + value)
    return Path(value)


class ReadyGate:
    """동시 실행 수를 고정하지 않고 입력 준비 전부터 같은 중앙 자원 예약을 사용한다."""
    def __init__(self, admission=None):
        self.admission = admission
        self.contexts = {}

    async def __aenter__(self):
        work_unit = getattr(self.admission, 'work_unit', None)
        if callable(work_unit):
            context = work_unit('analysis-review')
            value = await context.__aenter__()
            self.contexts[asyncio.current_task()] = context
            return value
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        context = self.contexts.pop(asyncio.current_task(), None)
        if context is not None:
            return await context.__aexit__(exc_type, exc_value, traceback)
        return False


LANGUAGE_REPAIR_REFUND_LIMIT = 8
ARBITER_ROUNDS_LIMIT = 2


_CROSS_ITEM_MARKERS = ('다른 방향', '방향 간', '영역 간', '공통 방향', '통합 또는 독립', '중복 해소', '중복·상충', '관계 검토',
                       '제공되지 않아', '대조할 수 없')


def _cross_item_only(row, key):
    """미확인 항목이 담당 항목 밖의 다른 방향·영역 본문 대조만을 요구하는지 판정한다. 사실 ID가 입력에 없다는 이유는 포함하지 않는다."""
    text = ' '.join(str(row.get(field, '')) for field in ('topic', 'reason', 'required_evidence'))
    other_ids = re.findall(r'D_P\d{4}', text)
    others = [ident for ident in other_ids if ident != key.replace('direction-', '')]
    needs_other_directions = bool(others) and any(marker in text for marker in _CROSS_ITEM_MARKERS)
    wants_facts_only = bool(re.search(r'원문|수집 값|사실 값|transcript|원장 값', row.get('required_evidence', '')))
    return needs_other_directions and not wants_facts_only




def is_direction_input(stage, data):
    """종합 방향 한 건을 쓰는 작성 입력인지(방향 슬롯 배정이 있는 stage 2)."""
    return bool(stage == 2 and isinstance(data, dict) and (data.get('stage_data') or {}).get('direction_assignment'))


def author_plan(stage, data):
    """작성 호출의 (역할, 추가 지시문). 방향 작성만 direction_author(luna high)와 작업표·가드를 쓰고 나머지는 author(sol xhigh) 그대로다."""
    if not is_direction_input(stage, data):
        return 'author', ''
    import asis_direction_harness as harness
    return 'direction_author', '\n\n' + harness.work_table_for(data) + '\n\n' + harness.DIRECTION_GUARD


LUNA_PROMPT_TOKEN_CEILING = 100_000   # luna 문맥 128K에서 출력 예약분을 뺀 안전 입력 상한
LUNA_TOKEN_PER_BYTE_HIGH = 0.42       # 영수증 실측 토큰/바이트 비의 높은 쪽(asis_input_budget.DEFAULT_RATIO_HIGH)


def role_for_prompt_size(role, prompt):
    """방향 작성 입력이 luna 문맥 한도를 넘을 수 있으면 코드가 sol 작성 역할(문맥 큼)로 보낸다. 운영 2026-10-17 D_P0030: 147258>128000 실패."""
    if role == 'direction_author' and len(prompt.encode('utf-8')) * LUNA_TOKEN_PER_BYTE_HIGH > LUNA_PROMPT_TOKEN_CEILING:
        return 'author'
    return role



def _chars_of(value):
    return len(json.dumps(value, ensure_ascii=False))


class Pipeline:
    """완결 후보·독립 검토·누적 보완을 기존 r2 계약과 원장에 결합한다."""

    def __init__(self, runtime, folder, *, progress=None, rounds=3, author_workers=None, review_workers=None):
        self.runtime = runtime
        self.folder = history_path(folder)
        self.progress = progress or (lambda event: None)
        self.rounds = max(1, int(rounds))
        self.author_gate = ReadyGate() if author_workers is None else asyncio.Semaphore(max(1, int(author_workers)))
        self.review_gate = ReadyGate() if review_workers is None else asyncio.Semaphore(max(1, int(review_workers)))
        self.item_gate = ReadyGate(getattr(runtime,'gate',None)) if author_workers is None else asyncio.Semaphore(max(1, int(author_workers)))
        self.validator = asis_analysis.contract()
        self.tasks = None
        self.chat = None            # 분석 단계 채팅(없으면 사람 에스컬레이션 없이 기존처럼 실패 처리)
        self.incident_store = None  # 고객별 사건·지식 저장소(없으면 기록하지 않음)
        from asis_resume import ResumeContext
        self.resume = ResumeContext.active(self.folder)
        if self.resume and self.resume.approval.get('session_restores'):
            self.runtime.restore_existing_sessions(self.resume.approval['session_restores'])

    def task_manager(self, data):
        """작성 호출 전에 실행 원장을 만든다. 원장은 같은 파이프라인 수명 동안 유지한다."""
        from asis_tasks import TaskManager
        if self.tasks is None:
            self.tasks = TaskManager(self.folder, data['run'], specs=[], progress=self.progress,runtime=self.runtime)
            self.tasks.preserved_completed = self.resume.preserved if self.resume else set()
        if self.tasks.run != data['run']:
            raise ValueError('task_run_binding_changed')
        return self.tasks

    def pair_task(self, data, key, *, scope='content'):
        from asis_tasks import task
        manager = self.task_manager(data)
        if key not in manager.ledger['tasks']:
            manager.register([task(key, key, executor='model', pair_key=key)])
        manager.bind_pair(key,
            author_session='asis-author-'+digest({'run':data['run'],'key':key})[:24],
            reviewer_session='asis-review-'+digest({'run':data['run'],'key':key})[:24], scope=scope)
        return manager

    def report_pair(self, data, key, role, candidate, evidence, checks, summary):
        manager = self.pair_task(data, key)
        if self.resume and key in self.resume.preserved and manager.ledger['tasks'][key]['status']=='completed':
            return
        manager.report(key, role, session_id=manager.ledger['tasks'][key]['pair'][role],
            candidate_sha256=digest(candidate), evidence=evidence, checks=checks, summary=summary)

    async def run_task(self,data,key,operation,*,title=None):
        from asis_tasks import task
        manager = self.task_manager(data)
        if key not in manager.ledger['tasks']:
            manager.register([task(key,title or key)])
        async def execute():
            value = operation()
            if __import__('inspect').isawaitable(value):
                value = await value
            row = manager.ledger['tasks'][key]
            if row['executor'] == 'model' and not row.get('pair') and not (self.resume and key in self.resume.preserved):
                # 원래 체크포인트는 유지하고 이번 결과·입력·근거 색인만 불변 복사로 결합한다.
                input_hash,result_hash = digest(data),digest(value)
                from uuid import uuid4
                root = self.folder/'task-output'/key/digest({'input':input_hash,'result':result_hash})/uuid4().hex
                self.save(root/'input.json',data)
                self.save(root/'result.json',value)
                checkpoints = self.folder/'checkpoints'/key
                source_files = sorted(checkpoints.rglob('*.json')) if checkpoints.is_dir() else []
                index = []
                for source in source_files:
                    index.append({'path':source.relative_to(self.folder).as_posix(),
                                  'sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
                self.save(root/'checkpoint-index.json',index)
                manager.report_output(key,input_sha256=input_hash,result_sha256=result_hash,
                    result_path=root/'result.json',evidence=[root/'input.json',root/'result.json',root/'checkpoint-index.json'])
            return value
        return await manager.perform(key,execute)

    def save(self, path, value):
        # 원장 교체는 운영과 같은 원자 쓰기·Windows 재시도 계약을 사용한다.
        from asis_view import atomic
        atomic(history_path(path), value)

    def render_prompt(self,stage,data,*,model_view=True,summary_level=0):
        """model_view=False는 재사용 키·보존 검증용 렌더로, 이미 만든 작업의 키를 바꾸지 않는 기존 전송 방식이다.
        모델에 실제로 보내는 호출은 기본값(True)으로 공통 전송 계층(asis_model_view)을 거친다."""
        from asis_evidence_views import prompt_view
        # 체크포인트 JSON의 키 정렬이 바뀌어도 동일 입력의 프롬프트 바이트는 유지한다.
        view=data
        if model_view:
            import asis_model_view
            kind,projected=asis_model_view.build(stage,data,summary_level=summary_level)
            if kind is not None:
                view=projected
        # 요약(4)의 공통 전송 뷰는 이미 입력 범위를 줄였으므로 아래 기존 투영을 다시 적용하지 않는다. 진단·과제(3)는 기존 투영 규칙을 그대로 쓴다(재사용 키 보존).
        summary_projected = stage == 4 and view is not data and view.get('context', {}).get('model_scope', {}).get('projection_schema') == 'asis-model-scope/1'
        # 공통 전송 계층이 이미 항목 뷰를 만들었다면(view is not data) 구 투영으로 덮어쓰지 않는다.
        # 운영 실측 2026-10-16: 진단 항목 입력이 1.04MB로 줄어든 뷰를 이 분기가 정본 3.58MB로 되돌려 호출당 704K 토큰이 전송됐다.
        # 재사용 키용 렌더(model_view=False)는 view가 data 그대로라 이 분기를 계속 통과한다(키 불변).
        layer_projected = view is not data and view.get('context', {}).get('model_scope', {}).get('projection_schema') == 'asis-model-scope/1'
        if stage in (3,4) and not summary_projected and not layer_projected and data.get('context',{}).get('model_scope',{}).get('projection_schema')!='asis-model-scope/1':
            # 진단·과제·요약 작성은 승인된 상위 결과가 인용한 근거만 필요하다. 전송만 투영하고 검증은 정본 전체 기준이다.
            view=deepcopy(data); self.project_review_scope(view,view['stage_data'])
            if stage==4:
                # 요약은 승인된 방향·과제의 ID와 핵심 문장으로 연결한다. 정본 승인 결과는 그대로이고 전송 범위만 줄인다.
                keep=lambda rows,fields:[{k:deepcopy(r[k]) for k in fields if k in r} for r in rows]
                sd=view['stage_data']
                if isinstance(sd.get('approved_areas'),dict):
                    sd['approved_areas']={**{k:v for k,v in sd['approved_areas'].items() if k!='areas'},
                        'areas':[{'area_id':a['area_id'],'headline':a['headline'],'implication':deepcopy(a['implication'])} for a in sd['approved_areas']['areas']]}
                if isinstance(sd.get('approved_directions'),dict):
                    sd['approved_directions']={**{k:v for k,v in sd['approved_directions'].items() if k not in('directions','edges')},
                        'directions':keep(sd['approved_directions']['directions'],('id','title','text','customer_impact','change_direction','area_implication_refs'))}
                if isinstance(sd.get('approved_actions'),dict):
                    sd['approved_actions']={**{k:v for k,v in sd['approved_actions'].items() if k not in('diagnoses','actions','edges')},
                        'diagnoses':keep(sd['approved_actions']['diagnoses'],('id','text','impact','direction_refs')),
                        'actions':keep(sd['approved_actions']['actions'],('id','title','target','diagnosis_refs','priority'))}
        system,prompt=self.validator.render_prompt(stage,strict_json(encoded(prompt_view(view))))
        if stage == 1 and data['stage_data'].get('area_slots') == []:
            system += ('\n이번 area 호출은 영역 공통 unresolved 항목만 보완한다. areas=[]를 반환하고 '
                       'approved_areas의 승인 영역·관찰·시사점을 재작성하지 않는다. '
                       'prior_output의 공통 미확인 항목과 literal 지적을 함께 사용한다. '
                       '고객용 reason·required_evidence에는 내부 생성 코드·필드 연결·렌더 수정 지시를 넣지 않는다. '
                       '관측값·미확인 상태·근거 참조는 보존하며 코드 결함을 고객 운영 결함으로 전환하지 않는다.')
        if stage == 1 and data['stage_data'].get('planned_observation_slots'):
            system += ('\nplanned_observation_slots의 각 이슈는 해당 observation_id로 하나씩 작성한다. '
                'code_observations는 참고 원문이며 이미 계획 이슈에 포함된 중복 관찰을 다시 만들지 않는다. '
                '계획 이슈의 원천 필드·기준·대상과 확인 사실을 누락하지 않고 관찰에 연결한다. '
                'scope/reason은 분석 계획이지 고객 사실이 아니다. 관찰은 전달된 facts 값으로 직접 대조하며 '
                '미수집 값·기준적용·사용/품질·성과를 임의 확정하지 않는다. '
                '필드 명시 false와 null은 다르다. 부분 수집은 관측된 개별 속성의 해석까지 금지하지 않는다. '
                '기준 미확정 사항은 조건부 방향·판단한계로 연결하고 확인된 강점은 유지한다.')
        if stage == 1 and data['stage_data'].get('issue_assignment'):
            system += ('\n이번 호출은 issue_assignment의 계획 이슈 한 건만 작성한다. areas에는 해당 영역 한 건만 넣고 observations에는 '
                '할당된 observation ID 한 건만 작성한다. headline은 이 이슈의 핵심 판단 한 문장, implication은 이 이슈가 영역에 주는 영향의 짧은 문장으로 쓰되 '
                '코드가 영역 합본에서 영역 전체의 headline·implication으로 대체하므로 할당된 area_implication ID 사용은 요구하지 않는다. '
                '미확인은 unresolved에 기록하고 다른 이슈는 작성하지 않는다. planned_observation_slots의 scope/reason은 분석 계획이지 고객 사실이 아니다.')
        if stage == 1 and data['stage_data'].get('approved_observations') is not None and data['stage_data'].get('mode') == 'area_summary':
            system += ('\n이번 호출은 승인된 관찰(approved_observations)만 근거로 영역의 headline과 implication을 작성한다. '
                'observations는 []로 반환한다. 관찰에 없는 사실을 새로 단정하지 않고 관찰이 인용한 fact_refs·criterion_refs 범위 안에서만 참조한다. '
                '영역 전체를 대표하는 headline 한 문장과 implication(영향·변경 방향)을 작성한다. 확인이 필요한 항목은 unresolved에 기록한다.')
        if stage == 2 and data['stage_data'].get('direction_assignment'):
            system += ('\n이번 호출은 direction_assignment의 종합 방향 한 건만 작성한다. '
                       'direction_slots의 해당 ID만 사용하며 다른 종합 방향은 작성하지 않는다. '
                       'scope/reason은 의미 범위 계획이지 고객 사실이 아니다. '
                       '참조는 해당 범위의 실제 근거·시사점에 결합하고 edges=[]로 반환한다. '
                       '관계선은 코드가 fact_refs/area_implication_refs에서 생성한다. '
                       '직전 후보와 전체 지적을 같은 작성자에서 한 번에 보완한다.')
        if stage == 3 and data['stage_data'].get('item_assignment'):
            system += ('\n이번 호출은 item_assignment의 항목 하나만 작성한다. diagnosis이면 diagnoses에 해당 ID 한 건·actions=[], '
                'action이면 actions에 해당 ID 한 건·diagnoses=[]를 반환한다. approved_diagnoses는 승인 근거이며 재작성하지 않는다. '
                '직전 후보와 누적 지적을 함께 사용하여 모든 지적을 한 번에 반영한다. 항목의 설명량과 장표 수는 가변이다.')
        if stage==5:
            system += ('\n검토 JSON의 verdict=pass는 defects=[]와 unresolved=[]일 때만 유효하다. '
                       '내용 검토의 render_checked=false는 정상적인 단계 경계이며 렌더 미제공을 보류 항목에 추가하지 않는다. '
                       '작성 후보의 정직한 미확인은 그대로 유지한다. 실제 내용 결함은 revise, 검토 자체를 완료할 수 없는 경우만 blocked로 반환한다.')
            if data['stage_data'].get('item_assignment'):
                system += ('\nitem_assignment의 담당 항목 전체를 독립 검토한다. 구조·근거 검사에서 지적을 발견해도 '
                    '검토 가능한 본문·근거·판정·관계를 끝까지 확인하고 모든 actionable 지적을 모아 반환한다. '
                    '렌더 이미지가 있으면 제공된 담당 항목의 실제 장표 전체도 검토하고 render_checked=true로 반환한다. '
                    '내용 지적은 draft의 /diagnoses/0/... 또는 /actions/0/... 원문, 배치 지적은 /pages/n/texts/m 원문 위치다. '
                    '이전 지적은 현재 후보에서 다시 확인하며 다른 항목을 재작성·평가하지 않는다.')
        expected={1:'area',2:'synthesis',3:'actions',4:'summary',5:'review'}[stage]
        schema=strict_json((ASSET/'contract/output.schema.json').read_bytes())
        branch=next(node for node in schema['oneOf'] if node['properties']['stage']['const']==expected)
        prompt=prompt.replace('\n입력:\n','\n출력 JSON schema:\n'+encoded(branch).decode('utf-8')+'\n입력:\n',1)
        return system,prompt

    def event(self, key, phase, status, **fields):
        self.progress({'unit': key, 'phase': phase, 'status': status, **fields})

    def instructions(self, system):
        style = ('\n[최상위 분석 목적 및 Excel-first 계약]\n'
            '고객의 현재 환경과 업무 목표를 근거 중심으로 분석한다. AI 미사용은 도입 방향·조건, 기사용은 활용 효과·운영 수준 개선을 제시한다. '
            '단순 인프라 점검·미확인 목록으로 대체하지 않는다. 확인된 강점은 유지하고 근거가 충분한 차이는 변경·추가 과제로 제안한다. '
            '비용·경제성은 보안·거버넌스·인프라와 동등한 필수 축이다. 기간·통화·실측/견적/가정·초기/반복·할인·배부·포함/제외를 구분한다. '
            '동일 업무·처리량·품질·보안·서비스 수준에서 비AI 포함 대안을 비교한다. 시간 확보는 현금절감이 아니며 효과를 중복 합산하지 않는다. '
            '수요·채택·재시도·검토율·가격·용량의 민감도와 보류·중단 조건을 설명하되 자료가 없으면 실제값을 만들지 않는다. '
            '구조화 작성·내용 검토 결과를 Excel 현황·기준대조·과제·시사점 원장으로 먼저 생성·재계산·대조·동결한다. PPT는 그 동일 분석 개정만 소비한다. '
            '기준·관측 사실·판정·과제·시사점의 다대다 관계와 미확인을 보존하며 가상 인터뷰·샘플 데이터를 고객 사실로 전용하지 않는다. '
            '승인 보고서 전체 페이지 역할·설명 구조·표·그림을 보존한다. 별도 이미지의 영역/Fact Finding→시사점 종합 및 진단→Action Item·개선 포인트는 서로 다른 표현 계약이다. '
            '문서 작성 규칙 정본은 ~/.hermes/shared/DOCUMENT-AUTHORING-RULES.md, 적용 기준은 DOCUMENT-PRO-001이다. '
            '\n[스킬 적용: DOCUMENT-PRO-001 / humanizer / ai-ref-deck]\n'
            '전문 컨설턴트 문체로 사실·근거·판단·영향·개선 방향을 구분한다. '
            '감상·허구 사례·의인화·구어체·챗봇 안내·근거 없는 수사·상투적 대구를 넣지 않는다. '
            'AI와 Microsoft 서비스의 공식 명칭은 유지한다. 원문 인용과 해설은 구분한다. '
            'v18/승인 AS-IS의 색·폰트·여백·제목·푸터·페이지 번호·관계는 코드 소유다. '
            '새 카드·제목·과제 개수를 고정하거나 설명을 줄여 디자인에 맞추지 않는다. '
            '실제 렌더가 없는 검토를 시각 검토 완료라고 하지 않는다. JSON만 반환한다.')
        if getattr(self, 'report_kind', None) == 'current_state':
            style += ('\n[이번 산출물의 우선 범위: AS-IS 현황분석 보고서]\n'
                '이 실행은 실제 인터뷰 전에 작성하는 현황분석 보고서다. 수집 사실·현재 구성·기술적 해석·수집 범위와 한계만 작성한다. '
                '위의 일반 종합보고서 목적과 충돌하면 이 범위를 우선한다. '
                '고객의 실제 업무 목표·운영 책임·활용 성과를 추정해 사실로 쓰지 않는다. '
                '최종 시사점 종합·사업 영향 확정·진단과제 계획·개선과제·우선순위·이행계획을 작성하지 않는다. '
                '영역 implication.text는 관측과 공식 기준에서 확인 가능한 현재 기술적 의미만 설명한다. '
                'impact는 확인 가능한 기술적 범위와 미확인을 분리하고 change_direction에는 인터뷰에서 확인할 조건만 기술한다. '
                '개선 권고를 현황으로 제시하지 않는다. 미수집과 없음, 미확인과 부적합은 구분한다. '
                '비용은 현재 집계의 대상·기간·통화·배부 범위와 관측 가능한 추이만 설명하며 경제적 효과를 만들지 않는다. '
                '최신 사용자 승인에 따라 첫 현황 PPT도 최대150장이고 핵심 현황·중요 근거·기술적 영향 중심으로 일반·중복을 통합한다. '
                '이는 본문 선택·재편집 승인이지 원문 문장을 자르거나 중요한 내용을 숨기는 승인이 아니다. '
                '정본 전체 관찰·상세·미확인은 Excel 원장에 보존하고 본문/상세원장 귀속을 코드로 전수 검사한다. '
                '장표 한도와 핵심 편집은 presentation 단계 전용이며 현황분석 원장·이슈 계획의 기술적 깊이와 근거를 줄이지 않는다. '
                '시각 검토 입력의 presentation은 별도 작성·독립 검토를 마친 핵심 편집이다. 상세가 PPT에 없다는 이유만으로 결함으로 판정하지 않는다. '
                '편집 본문의 문제는 /presentation/areas/의 실제 문자열 필드를 인용해 지적하고 원문 분석 자체의 문제는 /stages/의 실제 문자열 필드를 지적한다.')
        return system + style

    def classification(self, data):
        return ('synthetic_unit_test' if data['context'].get('synthetic_contract_only') is True
                else data['context'].get('classification', 'case_snapshot'))

    HUMAN_WAIT_SECONDS = 6 * 3600
    HUMAN_POLL_SECONDS = 5

    async def _ask_human(self, key, review, attempt_dir, attempt):
        """두 단계 관리자가 모두 해결하지 못한 blocked를 채팅으로 사람에게 묻는다. 채팅이 없으면 기존처럼 실패로 둔다.
        사람 결정은 허용된 조치로만 변환되며 pass로 바꾸는 조치는 없다. 해결되면 출처가 붙은 사건으로 기록한다."""
        chat = getattr(self, 'chat', None)
        if chat is None or chat.is_closed():
            return None
        import asis_chat
        evidence = [{'path': (attempt_dir/'review.json').relative_to(self.folder).as_posix()}] if (attempt_dir/'review.json').exists() else []
        for name in ('arbiter-decision.json', 'arbiter-decision-global.json'):
            if (attempt_dir/name).exists():
                evidence.append({'path': (attempt_dir/name).relative_to(self.folder).as_posix()})
        if not evidence:
            return None
        topics = '; '.join(str(row.get('topic', ''))[:60] for row in review['unresolved'][:3])
        options = [{'id': 'defer', 'label': '병합 검토로 넘긴다', 'action': 'defer_to_merge'},
                   {'id': 'retry', 'label': '작성자에게 되돌려 다시 쓴다', 'action': 'retry_author'},
                   {'id': 'stop', 'label': '이 개정을 중지한다', 'action': 'stop_revision'}]
        question = chat.ask(sender='global_manager', task_key=key, options=options, evidence=evidence,
            summary='독립 검토가 ' + key + '의 다음 항목을 확인하지 못했고 영역 담당·전체 관리자도 해결하지 못했습니다: ' + topics)
        self.event(key, 'content_review', 'awaiting_human', question_id=question['id'])
        waited = 0
        while True:
            record = chat.pending_action(key)
            if record is not None:
                break
            if waited >= self.HUMAN_WAIT_SECONDS:
                self.event(key, 'content_review', 'human_wait_expired', question_id=question['id'])
                return None
            await asyncio.sleep(self.HUMAN_POLL_SECONDS)
            waited += self.HUMAN_POLL_SECONDS
        decision = record['decision']
        chat.consume(record['id'])
        self._record_human_incident(key, review, record, attempt_dir)
        if decision.get('free_text'):
            return 'human_instruction', [{'instruction': decision['free_text'], 'decision_id': decision['id']}]
        mapping = {'defer_to_merge': 'defer_to_merge', 'retry_author': 'author_repair'}
        action = decision['action']
        if action == 'stop_revision':
            raise RuntimeError('stopped_by_human_decision:' + key)
        if action == 'author_repair' or action == 'retry_author':
            return 'author_repair', [{'index': i, 'action': 'author_repair', 'quote': str(row.get('topic', ''))[:300],
                                      'reason': '영석님이 작성자에게 되돌리도록 결정했다.', 'decision_id': decision['id']}
                                     for i, row in enumerate(review['unresolved'])]
        return mapping.get(action, 'upheld'), [{'decision_id': decision['id']}]

    def _record_human_incident(self, key, review, record, attempt_dir):
        """사람이 해결한 막힘을 사건 4항목으로 남긴다. 출처는 검토 기록 파일과 결정 ID이다. 기록 실패는 개정을 막지 않고 이벤트로 남긴다."""
        store = getattr(self, 'incident_store', None)
        if store is None:
            return
        try:
            sources = [{'decision_id': record['decision']['id']}]
            for item in record['evidence']:
                sources.append({'path': item['path']})
            store.record_incident(kind='review-blocked', subject=key,
                symptoms=['독립 검토가 확인하지 못한 항목: ' + str(row.get('topic', ''))[:100] for row in review['unresolved'][:5]] or ['독립 검토 blocked'],
                steps_that_worked=['영석님이 채팅에서 ' + (record['decision'].get('option_id') or '자유 문장 지시') + '를 선택했다'],
                root_cause=[], pitfalls=['영역 담당·전체 관리자가 모두 upheld로 판정했다'], sources=sources,
                resolution='resolved_by_human')
        except Exception as exc:
            self.event(key, 'content_review', 'incident_record_failed', error=type(exc).__name__ + ':' + str(exc)[:120])

    async def _arbitrate_blocked(self, data, key, candidate, review, prompt_data, attempt_dir, *, level='area_owner', previous=None):
        """blocked 항목을 조정자에게 보내 조치를 받는다. 형식 위반은 한 번 다시 묻고, 그래도 어기면 upheld(차단 유지)로 처리한다."""
        import asis_arbiter
        supplied = set(prompt_data.get('context', {}).get('model_scope', {}).get('inline_fact_refs', []))
        cited = set()
        def collect(value):
            if isinstance(value, dict):
                for name, child in value.items():
                    if name == 'fact_refs' and isinstance(child, list):
                        cited.update(v for v in child if isinstance(v, str))
                    else:
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(candidate)
        known = {row['id'] for row in data.get('facts', [])}
        request = asis_arbiter.build_request(key=key, candidate=candidate, unresolved=review['unresolved'],
            supplied_fact_ids=supplied, cited_fact_ids=cited & known, own_id=key.replace('direction-', ''))
        request['level'] = level
        if previous:
            request['previous_decisions'] = previous
        area_id = prompt_data.get('stage_data', {}).get('area_slots', [{}])[0].get('area_id') or (
            re.match(r'area-([a-z]+)', key).group(1) if re.match(r'area-([a-z]+)', key) else '')
        area_name = asis_analysis.AREA_NAMES.get(area_id, area_id)
        system = self.instructions((ASSET/'contract/prompts/P0-system.txt').read_text(encoding='utf-8'))
        last_error = None
        for attempt_no in (1, 2):
            message = asis_arbiter.level_instruction(level, area_name) + ('\n이전 응답이 형식 검증에 실패했다: ' + last_error if last_error else '') + '\n입력:\n' + json.dumps(request, ensure_ascii=False)
            raw, receipt = await self.runtime.ask('content_review', message,
                session_id='asis-arbiter-'+digest({'run': data['run'], 'key': key, 'round': attempt_dir.name, 'try': attempt_no, 'level': level})[:24],
                instructions=system)
            self.save(attempt_dir/f'arbiter-{attempt_no}-response.txt', raw.encode('utf-8'))
            self.save(attempt_dir/f'arbiter-{attempt_no}-receipt.json', receipt)
            try:
                decisions = asis_arbiter.parse_decisions(raw.strip(), unresolved=review['unresolved'],
                    supplied_fact_ids=supplied, cited_fact_ids=cited & known)
            except ValueError as exc:
                last_error = str(exc)
                self.save(attempt_dir/f'arbiter-{attempt_no}-rejected.json', {'error': last_error})
                continue
            action = asis_arbiter.combine(decisions)
            self.save(attempt_dir/('arbiter-decision.json' if level == 'area_owner' else 'arbiter-decision-global.json'), {'level': level, 'action': action, 'decisions': decisions,
                'input_sha256': digest(request), 'authority': 'classify_only_cannot_declare_pass'})
            self.event(key, 'content_review', 'arbitrated', action=action, items=len(decisions), level=level)
            return action, decisions
        self.save(attempt_dir/('arbiter-decision.json' if level == 'area_owner' else 'arbiter-decision-global.json'), {'level': level, 'action': 'upheld', 'decisions': [], 'error': last_error,
            'authority': 'classify_only_cannot_declare_pass'})
        return 'upheld', []

    async def release_pair_sessions(self, data, key):
        """승인되어 체크포인트에 저장된 작성·검토 쌍의 메모리 세션을 해제한다. 해제 실패는 승인된 결과에 영향을 주지 않는다.

        운영 실측(2026-10-17): 항목마다 만든 세션을 해제하지 않아 워커 메모리가 0.3GB 에서 10GB 로 올라 진입 제어가 동시 실행을 2개로 줄였다.
        영수증·체크포인트·디스크 세션 기록은 그대로 두므로 재개·검증은 영향을 받지 않는다.
        """
        release = getattr(self.runtime, 'release_session', None)
        if release is None:
            return
        for prefix in ('asis-author-', 'asis-review-'):
            try:
                await release(prefix + digest({'run': data['run'], 'key': key})[:24])
            except Exception:
                pass

    def _record_summary(self, stage, prompt_data, prompt, level, root, attempt_dir, key):
        """요약 단계로 렌더한 프롬프트의 요약 사실을 summarized-input.json 과 이벤트로 남긴다(원본 입력은 author-input.json 에 그대로 있다)."""
        import asis_length_failures as length_failures
        count = json.loads((Path(root)/length_failures.FILE).read_text(encoding='utf-8'))['count']
        self.save(attempt_dir/'summarized-input.json', {'level': level, 'reason': 'two_or_more_length_failures', 'length_failure_count': count,
            'summarized_bytes': len(prompt.encode('utf-8'))})
        self.event(key, 'author', 'input_summarized', level=level, summarized_bytes=len(prompt.encode('utf-8')))
        return prompt

    async def reviewed_stage(self, data, stage, key, *, candidate_check=None, author_budget=None):
        manager = self.pair_task(data, key)
        return await manager.perform(key, lambda:self._reviewed_stage(
            data,stage,key,candidate_check=candidate_check,author_budget=author_budget))

    @staticmethod
    def project_review_scope(review_data, draft, *, image=None):
        """검토 호출의 전송 범위만 대상 draft가 인용한 사실·기준으로 줄인다. 정본·검증·체크포인트 바인딩은 바꾸지 않는다."""
        context = review_data['context']
        scope = context.setdefault('model_scope', {})
        if scope.get('projection_schema') == 'asis-model-scope/1' or image is not None:
            return
        facts, criteria = set(scope.get('inline_fact_refs', [])), set()
        def walk(value):
            if isinstance(value, dict):
                for name, child in value.items():
                    if name == 'fact_refs' and isinstance(child, list): facts.update(v for v in child if isinstance(v, str))
                    elif name == 'criterion_refs' and isinstance(child, list): criteria.update(v for v in child if isinstance(v, str))
                    else: walk(child)
            elif isinstance(value, list):
                for child in value: walk(child)
        walk(draft); walk(review_data['stage_data'].get('approved_areas')); walk(review_data['stage_data'].get('approved_directions'))
        walk(review_data['stage_data'].get('approved_diagnoses')); walk(review_data['stage_data'].get('approved_actions'))
        if scope.get('criterion_refs'): criteria |= set(scope['criterion_refs'])
        known_f = {r['id'] for r in review_data['facts']}; known_c = {r['id'] for r in review_data['criteria']}
        # 인용된 사실·기준은 전문을 유지한다. 인용되지 않은 대량 사실은 존재 요약(ID·해시·건수)으로 남겨 누락이 보이게 한다.
        from asis_model import digest as _digest, encoded as _encoded
        omitted = [{'id':r['id'],'source_id':r.get('source_id'),'value_sha256':_digest(r.get('value')),
                    'record_count':len(r['value']) if isinstance(r.get('value'),list) else None}
                   for r in review_data['facts'] if r['id'] not in facts and len(_encoded(r.get('value')))>45000]
        scope.update(projection_schema='asis-model-scope/1', fact_refs=sorted((facts & known_f) | {r['id'] for r in review_data['facts'] if r['id'] not in facts and len(_encoded(r.get('value')))>45000}),
                     criterion_refs=sorted(criteria & known_c), goal_refs=[r['id'] for r in review_data.get('goals', [])])

    async def _reviewed_stage(self, data, stage, key, *, candidate_check=None, author_budget=None):
        import asis_length_failures as length_failures
        if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
            raise ValueError('invalid_stage_key')
        self.validator.validate_input(data)
        root = self.folder / 'checkpoints' / key
        from asis_runtime import policy
        # 재사용 키는 '무엇을 모델에 보냈는가'(입력·단계·모델 정책·프롬프트)로만 정한다. 파이프라인 전체 코드 해시를 키에 넣으면
        # 검토 형식 정리 같은 무관한 코드 수정까지 이미 통과한 1,400여 개 작업의 재사용을 깨서 수 시간을 다시 쓰게 된다(운영 실측).
        # 작성 결과의 정당성은 재사용할 때 validate_output·validate_input·독립 검토 통과 기록으로 다시 확인하므로 키에서 빠져도 약해지지 않는다.
        input_hash = digest({'input': data, 'stage': stage, 'code_binding': STAGE_REUSE_CODE_BINDING,
                             'model_policy': policy(), 'prompts': self.render_prompt(stage, data, model_view=False)})
        accepted = root / 'accepted.json'
        if self.resume and key in self.resume.preserved:
            cached = self.resume.stage(self,data,stage,key)
            self.validator.validate_output(data,cached['candidate'])
            if candidate_check:candidate_check(cached['candidate'])
            if cached.get('review',{}).get('verdict')!='pass' or not cached['review'].get('content_checked'):
                raise ValueError('resume_original_review_not_passed:'+key)
            self.resume.verified(key,cached)
            self.event(key,'content_review','revalidated_preserved',candidate_sha256=cached['candidate_sha256'])
            return cached['candidate']
        reusable=self.resume.existing_candidate(key) if self.resume else None
        existing_review_fallback = None
        if reusable is not None:
            from asis_item_agents import project_item_graph
            original,source=reusable
            candidate=project_item_graph(original,data['stage_data']['item_assignment'])
            self.validator.validate_output(data,candidate)
            if candidate_check:candidate_check(candidate)
            if digest(candidate)!=source['projected_candidate_sha256']:
                raise ValueError('resume_projection_binding_changed')
            review_key=key+'-failed-scope-review'
            review=await self.independent_review(data,candidate,review_key,item_key=key)
            if review.get('verdict')=='pass':
                proof=root/'resume-approval'/self.resume.approval['approval_id']
                self.save(proof/'projection.json',{'original_candidate_file':source['path'],
                    'original_candidate_file_sha256':source['sha256'],'candidate_sha256':digest(candidate),
                    'model_authored_content_unchanged':True,'edges_owned_by_code':True,'new_author_calls':0})
                original_receipt=self.folder/Path(source['path']).parent/'author-receipt.json'
                reviewer_record=strict_json((self.folder/'checkpoints'/review_key/'accepted.json').read_bytes())
                record={'input_sha256':input_hash,'candidate':candidate,'candidate_sha256':digest(candidate),
                    'reviewed_candidate_sha256':digest(candidate),'round':source['author_model_responses_preserved'],
                    'author_receipt':strict_json(original_receipt.read_bytes()),'review':review,
                    'review_receipt':reviewer_record['receipt'],'execution_classification':self.classification(data),
                    'author_source_classification':'preserved_model_response_revalidated_not_new_author_call',
                    'source_approval_sha256':__import__('asis_resume').file_sha(self.resume.root/'approval.json')}
                self.save(proof/'accepted.json',record)
                self.report_pair(data,key,'author',candidate,[proof/'projection.json',original_receipt],
                    {'deterministic_passed':True},'원래 작성 본문을 보존하고 코드 소유 관계만 재검증')
                self.report_pair(data,key,'reviewer',candidate,[self.folder/'checkpoints'/review_key/'accepted.json'],review,
                    '동일 원천·보존 후보에 대한 현재 계약의 실제 독립 검토 통과')
                self.save(accepted,record)
                self.event(key,'content_review','completed',candidate_sha256=digest(candidate),author_reused=True)
                return candidate
            spent_rounds = int(source.get('author_model_responses_preserved', self.rounds))
            if review.get('verdict') != 'revise' or not review.get('defects') or spent_rounds >= self.rounds:
                raise RuntimeError('existing_candidate_fresh_review_not_passed:'+key)
            original_receipt=self.folder/Path(source['path']).parent/'author-receipt.json'
            reviewer_evidence = sorted((self.folder/'checkpoints'/review_key).rglob('review.json'), key=lambda p: p.stat().st_mtime_ns)
            self.report_pair(data,key,'author',candidate,[self.folder/source['path'],original_receipt],
                {'deterministic_passed':True},'원래 작성 본문을 보존하고 코드 소유 관계만 재검증')
            self.report_pair(data,key,'reviewer',candidate,reviewer_evidence[-1:] or [self.folder/source['path']],review,
                '보존 후보에 대한 독립 검토 결함 확인')
            existing_review_fallback = {
                'prior': candidate,
                'defects': deepcopy(review['defects']),
                'spent_rounds': spent_rounds,
                'resolution': self.tasks.resolve(key,role='author',action='revise_candidate',candidate=candidate,
                    defects=deepcopy(review['defects']),reason='review_revision_required',runtime=self.runtime),
            }
            self.event(key, 'repair', 'required', round=spent_rounds, defects=len(review['defects']))
        if accepted.exists():
            cached = strict_json(accepted.read_bytes())
            feedback_proof = None
            has_active_defects = bool(data.get('stage_data', {}).get('accumulated_defects'))
            if cached.get('input_sha256') != input_hash and not has_active_defects:
                from asis_resume import verified_feedback_checkpoint
                feedback_proof = verified_feedback_checkpoint(self.folder, key, data, cached,
                    lambda value: [digest({'input': value, 'stage': stage, 'code_binding': STAGE_REUSE_CODE_BINDING,
                                          'model_policy': policy(), 'prompts': self.render_prompt(stage, value, model_view=False)})])
                has_upstream_repair = bool(data.get('context', {}).get('same_revision_recovery') or (self.folder / 'analysis-reviewed.json').is_file())
                if feedback_proof is None and has_upstream_repair and stage in (1, 2, 3, 4):
                    candidate = cached.get('candidate')
                    if (isinstance(candidate, dict)
                            and cached.get('candidate_sha256') == digest(candidate)
                            and cached.get('reviewed_candidate_sha256') == digest(candidate)
                            and cached.get('review', {}).get('verdict') == 'pass'):
                        try:
                            self.validator.validate_output(data, candidate)
                            if candidate_check is not None:
                                candidate_check(candidate)
                            feedback_proof = {'schema': 'asis-upstream-repair-checkpoint-reuse/1',
                                              'unit': key, 'candidate_sha256': digest(candidate)}
                        except (ValueError, KeyError, TypeError):
                            feedback_proof = None
            if cached.get('input_sha256') == input_hash or feedback_proof is not None:
                candidate = cached['candidate']
                if (cached.get('candidate_sha256') != digest(candidate)
                        or cached.get('reviewed_candidate_sha256') != digest(candidate)
                        or cached.get('review', {}).get('verdict') != 'pass'):
                    raise ValueError('accepted_checkpoint_binding_failed')
                self.validator.validate_output(data, candidate)
                if candidate_check is not None:
                    candidate_check(candidate)
                cache_evidence = root/'cache'/digest(cached)/'accepted.json'
                self.save(cache_evidence,cached)
                if feedback_proof is not None:
                    self.save(cache_evidence.parent/'feedback-reuse.json', feedback_proof)
                self.report_pair(data,key,'author',candidate,[cache_evidence],{'deterministic_passed':True},'동일 후보의 작성 결과를 확인')
                self.report_pair(data,key,'reviewer',candidate,[cache_evidence],cached['review'],'기존 독립 검토 결과를 확인')
                self.event(key, 'content_review', 'reused', candidate_sha256=digest(candidate))
                return candidate
        prior = deepcopy(data['stage_data'].get('prior_output'))
        accumulated = deepcopy(data['stage_data'].get('accumulated_defects', []))
        resolution = None
        budget = author_budget if author_budget is not None else {'remaining':self.rounds}
        if type(budget.get('remaining')) is not int or not 0 <= budget['remaining'] <= self.rounds:
            raise ValueError('invalid_author_revision_budget')
        # 일본어·한자 혼입은 내용 검토가 아니라 문자 오타라서 보완 횟수(budget)를 쓰지 않는다. 같은 오류가 끝없이 반복되는 것만 별도 상한으로 막는다.
        language_refunds = 0
        arbiter_rounds = 0
        attempt = 0
        if existing_review_fallback is not None:
            prior = existing_review_fallback['prior']
            accumulated.extend(existing_review_fallback['defects'])
            resolution = existing_review_fallback['resolution']
            attempt = existing_review_fallback['spent_rounds']
            budget['remaining'] = max(0, budget['remaining'] - existing_review_fallback['spent_rounds'])
        while budget['remaining'] > 0:
            attempt += 1
            budget['remaining'] -= 1
            self.tasks.begin_pair_candidate(key)
            attempt_dir = root / f'round-{attempt:03d}'
            reuse_existing_author = False
            if (attempt_dir/'author-response.txt').exists() or (self.resume and attempt_dir.exists()):
                if (not self.resume and (attempt_dir/'candidate.json').is_file()
                        and (attempt_dir/'author-receipt.json').is_file()
                        and (attempt_dir/'author-input.json').is_file()
                        and not (attempt_dir/'review.json').exists()):
                    try:
                        saved_input = strict_json((attempt_dir/'author-input.json').read_bytes())
                        expected_input = deepcopy(data)
                        if prior is not None or accumulated:
                            expected_input['stage_data'].update(prior_output=prior, accumulated_defects=accumulated)
                        if resolution is not None:
                            expected_input['stage_data']['task_resolution'] = deepcopy(resolution)
                        if digest(saved_input) == digest(expected_input):
                            reuse_existing_author = True
                    except (ValueError, OSError):
                        reuse_existing_author = False
                if not reuse_existing_author:
                    from uuid import uuid4
                    attempt_dir = root/'inputs'/input_hash/uuid4().hex/f'round-{attempt:03d}'
            if reuse_existing_author:
                prompt_data = strict_json((attempt_dir/'author-input.json').read_bytes())
                raw = (attempt_dir/'author-response.txt').read_text(encoding='utf-8')
                receipt = strict_json((attempt_dir/'author-receipt.json').read_bytes())
                self.event(key, 'author', 'reused_unreviewed_candidate', round=attempt)
            else:
                self.event(key, 'author', 'running', round=attempt)
                prompt_data = deepcopy(data)
                if prior is not None or accumulated:
                    prompt_data['stage_data'].update(prior_output=prior, accumulated_defects=accumulated)
                if resolution is not None:
                    prompt_data['stage_data']['task_resolution'] = deepcopy(resolution)
                self.save(attempt_dir/'author-input.json', prompt_data)
                # 길이로 2번 이상 실패한 항목만 코드가 결정적으로 요약한 입력을 보낸다(영석님 지시 2026-10-18). 원본 입력·재사용 키는 그대로다.
                # 길이 실패는 두 곳에서 난다: 전송 전 한도 검사(render_prompt 안의 model_input_over_limit)와 모델의 한도 초과 거절. 둘 다 모델 호출 전 거절이라 비용이 없다.
                # 실패마다 항목 폴더에 영속 기록하고 작성 보완 예산은 쓰지 않는다. 기록된 횟수로 요약 단계를 정해(0~1회=원문, 2회=1단계, 3회 이상=2단계)
                # 같은 호출 안에서 다시 보낸다. 2단계로도 넘으면 내용을 더 깎지 않고 실패로 올린다.
                import asis_length_failures as length_failures
                is_item = bool(prompt_data.get('stage_data', {}).get('item_assignment')) and stage == 3
                is_summary = stage == 4 and prompt_data.get('stage_data', {}).get('mode') == 'summary'
                length_scope = is_item or is_summary          # 길이 실패 기록·요약은 과제 항목(3단계)과 요약(4단계)에만 걸린다
                # 길이 실패 재전송은 단계(0→1→2)가 올라가는 만큼만 있다. 4번을 넘기면 기록·단계 계산이 어긋난 것이므로 무한 루프 대신 실패로 올린다.
                for send_try in range(4):
                    level = length_failures.summary_level(root) if length_scope else 0
                    try:
                        system, prompt = self.render_prompt(stage, prompt_data, summary_level=level)
                        sent = self._record_summary(stage, prompt_data, prompt, level, root, attempt_dir, key) if level else prompt
                        async with self.author_gate:
                            if resolution is not None:
                                self.tasks.acknowledge(key,resolution['id'],resolution['target_session_id'])
                            author_role, author_extra = author_plan(stage, prompt_data)
                            author_role = role_for_prompt_size(author_role, sent)
                            raw, receipt = await self.runtime.ask(author_role, sent,
                                session_id='asis-author-'+digest({'run':data['run'],'key':key})[:24],
                                instructions=self.instructions(system)+author_extra)
                        break
                    except Exception as exc:
                        parsed = length_failures.parse(exc)
                        if parsed is None or not length_scope:
                            raise
                        length_failures.record(root, kind=parsed[0], tokens=parsed[1], limit=parsed[2])
                        if level >= 2 or send_try >= 3:
                            raise            # 2단계 요약으로도 한도를 넘었다(또는 재전송 상한). 내용을 더 깎지 않고 실패로 올린다.
                self.save(attempt_dir/'author-response.txt', raw.encode('utf-8'))
                self.save(attempt_dir/'author-receipt.json', receipt)
            deterministic_defects = []
            try:
                # 모델이 완전한 JSON 뒤에 닫는 괄호를 덧붙인 경우만 코드가 정리한다(운영 2026-10-18: 방향 작성 88건 중 3건, 보완 3회 소진).
                # 원문은 author-response.txt 에 그대로 남고, 제거한 잔여물은 response-repairs.json 에 기록한다. 보완 예산을 쓰지 않는다.
                from asis_response_repair import strip_trailing_closers
                parse_text, removed_tail = strip_trailing_closers(raw)
                if removed_tail:
                    self.save(attempt_dir/'response-repairs.json', {'repair':'trailing_closers_removed','removed':removed_tail,
                        'raw_sha256':hashlib.sha256(raw.encode('utf-8')).hexdigest(),'parsed_sha256':hashlib.sha256(parse_text.encode('utf-8')).hexdigest()})
                    self.event(key, 'author', 'response_repaired', round=attempt, removed=removed_tail)
                candidate = self.validator.strict_load(parse_text)
                candidate = self._bind_run(candidate, prompt_data, attempt_dir)
                if stage == 2 and isinstance(candidate, dict) and candidate.get('stage') == 'synthesis':
                    # 관계의 의미 참조는 모델이 작성하되 중복 표현인 edges는 코드가 생성한다.
                    raw_edges = deepcopy(candidate.get('edges'))
                    from asis_synthesis import project_graph
                    candidate = project_graph(candidate)
                    self.save(attempt_dir/'graph-projection.json', {
                        'generated_by':'code_from_direction_refs', 'raw_graph_authority':False,
                        'raw_graph_sha256':digest(raw_edges), 'candidate_sha256':digest(candidate)})
                if stage == 3 and prompt_data['stage_data'].get('item_assignment'):
                    from asis_item_agents import project_item_graph
                    raw_edges = deepcopy(candidate.get('edges')) if isinstance(candidate, dict) else None
                    candidate = project_item_graph(candidate, prompt_data['stage_data']['item_assignment'])
                    self.save(attempt_dir/'graph-projection.json', {
                        'generated_by':'code_from_item_refs', 'raw_graph_authority':False,
                        'raw_graph_sha256':digest(raw_edges), 'candidate_sha256':digest(candidate)})
                prior = candidate
                if prompt_data['stage_data'].get('item_assignment'):
                    from asis_item_agents import collect_item_defects
                    check = collect_item_defects(self, prompt_data, candidate)
                    deterministic_defects = check['defects']
                    self.save(attempt_dir/'deterministic-review.json', check)
                    if not check['content_review_possible']:
                        accumulated.extend(deterministic_defects)
                        self.save(attempt_dir/'deterministic-defects.json', deterministic_defects)
                        self.event(key, 'repair', 'required', round=attempt, reason='item_scope_not_reviewable')
                        continue
                else:
                    self.validator.validate_output(prompt_data, candidate)
                    if candidate_check is not None:
                        candidate_check(candidate)
            except Exception as exc:
                defect={'where':'/', 'quote': raw[:500], 'why':'deterministic_check:'+str(exc),
                        'fix':'수치·원장·할당 ID와 출력 스키마를 그대로 사용하여 이전 후보를 수정한다.', 'evidence_refs':[]}
                accumulated.append(defect)
                self.save(attempt_dir/'deterministic-defects.json', [defect])
                self.event(key, 'repair', 'required', round=attempt, reason=defect['why'])
                continue
            if is_direction_input(stage, prompt_data):
                # 방향 작성 하네스: 검토 전에 코드로 필수 참조·입력에 없는 이름/지역/위치·한자 혼입을 거르고 같은 작성자에게 되돌린다.
                # hard 결함만 되돌리며 soft 의심은 검토자 판단에 맡긴다. 되돌림은 보완 횟수(budget)를 쓰지 않는 언어 검사와 달리 일반 결함처럼 센다.
                import asis_direction_harness as harness
                import asis_output_grader as grader
                harness_defects = harness.hard_defects(prompt_data, candidate, grader)
                if harness_defects:
                    accumulated.extend(harness_defects)
                    self.save(attempt_dir/'harness-defects.json', harness_defects)
                    self.event(key, 'repair', 'required', round=attempt, reason='direction_harness', defects=len(harness_defects))
                    continue
            # 모델 검토 전에 고객용 한국어 본문의 일본어·한자 혼입을 결정적으로 거른다. 같은 작성자에게 위치·원문으로 되돌린다.
            from asis_language_guard import find_language_defects, allowed_text_from_input
            language_defects = find_language_defects(candidate, allowed_text=allowed_text_from_input(prompt_data))
            if language_defects:
                accumulated.extend(language_defects)
                self.save(attempt_dir/'language-defects.json', language_defects)
                self.event(key, 'repair', 'required', round=attempt, reason='language_guard', defects=len(language_defects))
                if language_refunds < LANGUAGE_REPAIR_REFUND_LIMIT:
                    language_refunds += 1
                    budget['remaining'] += 1
                continue
            candidate_hash = digest(candidate)
            self.save(attempt_dir/'candidate.json', candidate)
            self.report_pair(data,key,'author',candidate,[attempt_dir/'candidate.json',attempt_dir/'author-receipt.json'],
                {'deterministic_passed':not deterministic_defects},'항목 작성 결과와 후보 바이트를 기록')
            self.event(key, 'author', 'completed', round=attempt, candidate_sha256=candidate_hash)
            review_data = deepcopy(data)
            review_data['stage_data'].update(draft=candidate, candidate_sha256=candidate_hash,
                deterministic_check_result={'passed':not deterministic_defects, 'candidate_sha256':candidate_hash,
                                            'defects':deepcopy(deterministic_defects)})
            self.project_review_scope(review_data, candidate)
            # 항목 검토도 작성과 같은 길이 규칙을 따른다(영석님 지시 2026-10-18). 운영 사례: A_ITEM_0015 는 작성이 요약 2단계로 25.6만 토큰에 통과했지만
            # 검토 입력은 후보가 더해져 30.4만 토큰이 되어 한도에서 거절됐다. 길이 실패는 작성과 같은 항목 기록을 공유하고 작성 보완 예산을 쓰지 않으며 후보는 그대로다.
            review_is_item = bool(review_data['stage_data'].get('item_assignment'))
            # 요약(4단계) 검토도 같다(운영 2026-10-18 11단계: 작성은 2단계 요약으로 22.8만 토큰에 통과했지만 검토는 후보가 더해져 34.9만 토큰이 되어 거절됐다).
            review_is_summary = review_data['stage_data'].get('mode') == 'summary' and stage == 4
            review_scope = review_is_item or review_is_summary
            review_level = length_failures.summary_level(root) if review_scope else 0
            system, prompt = self.render_prompt(5, review_data, summary_level=review_level)
            self.save(attempt_dir/'review-input.json', review_data)
            self.event(key, 'content_review', 'running', round=attempt, candidate_sha256=candidate_hash)
            review = None; prior_review = None; contract_error = None; review_receipt = None
            for contract_attempt in range(1, self.rounds + 1):
                if contract_attempt > 1:
                    review_data['stage_data'].update(prior_review=prior_review,
                        review_contract_error=contract_error)
                    system, prompt = self.render_prompt(5, review_data, summary_level=review_level)
                    prompt = ('동일 작성 후보를 다시 검토한다. 직전 검토의 계약 오류만 보완하며 '
                        '후보 원문·실제 결함은 유지한다. evidence_refs는 제공 facts·criteria·goals·judgments의 '
                        'ID만 허용되며 영역 시사점·종합 방향·과제 ID는 근거 ID가 아니다.\n' + prompt)
                review_folder = attempt_dir / ('review-contract-' + str(contract_attempt).zfill(3))
                for review_try in range(4):
                    review_level = length_failures.summary_level(root) if review_scope else 0
                    try:
                        system, prompt_sent = self.render_prompt(5, review_data, summary_level=review_level) if review_try else (system, prompt)
                        if review_level and review_scope:
                            self.save(attempt_dir/'summarized-review-input.json', {'level': review_level, 'reason': 'two_or_more_length_failures',
                                'summarized_bytes': len(prompt_sent.encode('utf-8'))})
                            self.event(key, 'content_review', 'input_summarized', level=review_level, summarized_bytes=len(prompt_sent.encode('utf-8')))
                        async with self.review_gate:
                            raw, review_receipt = await self.runtime.ask('content_review', prompt_sent,
                                session_id='asis-review-'+digest({'run':data['run'],'key':key})[:24],
                                instructions=self.instructions(system))
                        break
                    except Exception as exc:
                        parsed = length_failures.parse(exc)
                        if parsed is None or not review_scope:
                            raise
                        length_failures.record(root, kind=parsed[0], tokens=parsed[1], limit=parsed[2])
                        if review_level >= 2 or review_try >= 3:
                            raise        # 2단계 요약으로도 한도를 넘었다. 내용을 더 깎지 않고 실패로 올린다.
                self.save(review_folder/'input.json', review_data)
                self.save(review_folder/'response.txt', raw.encode('utf-8'))
                self.save(review_folder/'receipt.json', review_receipt)
                self.save(attempt_dir/'review-response.txt', raw.encode('utf-8'))
                self.save(attempt_dir/'review-receipt.json', review_receipt)
                review = None
                try:
                    review = self.validator.strict_load(raw)
                    self.validator.validate_output(review_data, review)
                    if review_data['stage_data'].get('item_assignment'):
                        from asis_item_agents import validate_item_review
                        validate_item_review(review_data, candidate, review)
                    if not review['content_checked'] or review['render_checked']:
                        raise ValueError('incomplete_stage_content_review')
                    break
                except Exception as exc:
                    # 검토 계약 오류는 작성 후보에 결함으로 전가하거나 통과 처리하지 않는다.
                    prior_review = review if review is not None else {'raw_response':raw}
                    contract_error = str(exc)
                    self.save(review_folder/'contract-error.json',
                        {'error':contract_error,'candidate_sha256':candidate_hash})
                    if contract_attempt == self.rounds:
                        self.event(key, 'content_review', 'blocked', round=attempt, reason=contract_error)
                        raise RuntimeError('independent_review_invalid:'+contract_error) from exc
                    self.event(key,'content_review','running',round=attempt,
                        contract_attempt=contract_attempt + 1,reason='review_contract_repair')
            assert review is not None
            self.save(attempt_dir/'review.json', review)
            self.report_pair(data,key,'reviewer',candidate,[attempt_dir/'review.json',attempt_dir/'review-receipt.json'],
                review,'담당 항목의 독립 내용 검토 결과를 기록')
            if review['verdict'] == 'pass' and review['content_checked'] and not review['render_checked'] and not deterministic_defects:
                self.validator.validate_output(prompt_data, candidate)
                if candidate_check is not None:
                    candidate_check(candidate)
                record={'input_sha256':input_hash, 'candidate_sha256':candidate_hash,
                    'reviewed_candidate_sha256':candidate_hash, 'candidate':candidate, 'review':review,
                    'author_receipt':receipt, 'review_receipt':review_receipt,
                    'execution_classification':self.classification(data), 'round':attempt}
                self.save(accepted, record)
                self.event(key, 'content_review', 'completed', round=attempt, candidate_sha256=candidate_hash)
                await self.release_pair_sessions(data, key)
                return candidate
            if review['verdict'] == 'blocked':
                # 담당 방향 하나만 받은 검토가 다른 방향과의 중복·관계를 대조할 수 없다는 이유로만 막혔다면 차단하지 않는다.
                # 그 확인은 모든 방향 본문을 받는 병합 검토의 몫이며, 사실·근거·범위의 확인 불가는 계속 차단한다.
                if (stage == 2 and not deterministic_defects and not review['defects']
                        and review['unresolved'] and all(_cross_item_only(row, key) for row in review['unresolved'])):
                    review = {**review, 'verdict': 'pass', 'defects': [], 'content_checked': True,
                              'deferred_to_merge_review': deepcopy(review['unresolved']), 'unresolved': []}
                    self.save(attempt_dir/'review-deferred-cross-item.json', review)
                    self.validator.validate_output(prompt_data, candidate)
                    if candidate_check is not None:
                        candidate_check(candidate)
                    # 원장의 검토자 보고는 원본 blocked로 남아 있다. 코드가 병합 검토로 넘긴 통과 결과를 같은 쌍에 다시 보고한다.
                    self.report_pair(data,key,'reviewer',candidate,[attempt_dir/'review-deferred-cross-item.json',attempt_dir/'review-receipt.json'],
                        review,'다른 항목과의 대조 요구를 병합 검토로 넘긴 검토 결과를 기록')
                    record={'input_sha256':input_hash, 'candidate_sha256':candidate_hash,
                        'reviewed_candidate_sha256':candidate_hash, 'candidate':candidate, 'review':review,
                        'author_receipt':receipt, 'review_receipt':review_receipt,
                        'execution_classification':self.classification(data), 'round':attempt}
                    self.save(accepted, record)
                    self.event(key, 'content_review', 'completed', round=attempt, candidate_sha256=candidate_hash,
                               deferred_to_merge_review=len(review['deferred_to_merge_review']))
                    await self.release_pair_sessions(data, key)
                    return candidate
                # 코드가 분류하지 못한 blocked는 조정자가 판정한다. 조정자는 pass를 선언하지 못하고 조치만 고른다.
                if arbiter_rounds < ARBITER_ROUNDS_LIMIT and review['unresolved'] and not review['defects']:
                    arbiter_rounds += 1
                    action, decisions = await self._arbitrate_blocked(data, key, candidate, review, prompt_data, attempt_dir, level='area_owner')
                    if action == 'upheld':
                        # 영역 담당자가 해결하지 못하면 전체 중앙 관리자로 올린다. 그래도 upheld이면 아래에서 사람 보고로 실패 처리한다.
                        action, decisions = await self._arbitrate_blocked(data, key, candidate, review, prompt_data, attempt_dir,
                                                                         level='global_manager', previous=decisions)
                        if action == 'upheld':
                            self.event(key, 'content_review', 'escalated_to_human', round=attempt, unresolved=review['unresolved'])
                            human = await self._ask_human(key, review, attempt_dir, attempt)
                            if human is not None:
                                action, decisions = human
                    if action == 'defer_to_merge' and stage == 2 and not deterministic_defects:
                        review = {**review, 'verdict': 'pass', 'defects': [], 'content_checked': True,
                                  'deferred_to_merge_review': deepcopy(review['unresolved']), 'unresolved': [],
                                  'arbiter_decisions': decisions}
                        self.save(attempt_dir/'review-arbiter-deferred.json', review)
                        self.validator.validate_output(prompt_data, candidate)
                        if candidate_check is not None:
                            candidate_check(candidate)
                        # 조정자가 병합 검토로 넘겨 통과시킨 결과를 원장에 다시 보고한다. 원본 blocked 기록은 review.json에 그대로 있다.
                        self.report_pair(data,key,'reviewer',candidate,[attempt_dir/'review-arbiter-deferred.json',attempt_dir/'review-receipt.json'],
                            review,'조정자가 병합 검토로 넘긴 검토 결과를 기록')
                        record={'input_sha256':input_hash, 'candidate_sha256':candidate_hash,
                            'reviewed_candidate_sha256':candidate_hash, 'candidate':candidate, 'review':review,
                            'author_receipt':receipt, 'review_receipt':review_receipt,
                            'execution_classification':self.classification(data), 'round':attempt}
                        self.save(accepted, record)
                        self.event(key, 'content_review', 'completed', round=attempt, candidate_sha256=candidate_hash,
                                   deferred_to_merge_review=len(review['deferred_to_merge_review']), via='arbiter')
                        await self.release_pair_sessions(data, key)
                        return candidate
                    if action == 'human_instruction':
                        accumulated.append({'where': '/', 'quote': decisions[0]['instruction'][:400], 'evidence_refs': [],
                            'why': '영석님이 채팅에서 내린 지시이다.', 'fix': decisions[0]['instruction'][:800]})
                        prior = candidate
                        self.event(key, 'repair', 'required', round=attempt, reason='human_instruction')
                        budget['remaining'] += 1
                        continue
                    if action == 'author_repair':
                        from asis_arbiter import repair_defects
                        accumulated.extend(repair_defects(review['unresolved'], decisions))
                        prior = candidate
                        self.event(key, 'repair', 'required', round=attempt, reason='arbiter_author_repair')
                        budget['remaining'] += 1  # 조정자가 되돌린 시도는 작성자 보완 횟수를 쓰지 않는다. 조정 횟수 상한이 반복을 막는다.
                        continue
                    if action == 'supply_evidence':
                        from asis_arbiter import evidence_ids
                        extra = set(evidence_ids(decisions))
                        scope = data['context'].setdefault('model_scope', {})
                        scope['inline_fact_refs'] = sorted(set(scope.get('inline_fact_refs', [])) | extra)
                        scope['fact_refs'] = sorted(set(scope.get('fact_refs', [])) | extra)
                        data['stage_data']['arbiter_supplied_fact_refs'] = sorted(extra | set(data['stage_data'].get('arbiter_supplied_fact_refs', [])))
                        prior = candidate
                        self.event(key, 'repair', 'required', round=attempt, reason='arbiter_supply_evidence', supplied=len(extra))
                        budget['remaining'] += 1
                        continue
                if review['defects'] and not deterministic_defects and budget['remaining'] > 0:
                    # blocked 이면서 구체적 defects(where·quote·why·fix)를 함께 돌려준 응답은 설계가 다룬 적이 없는 입력이다(운영 2026-10-18 프로젝트 25 11단계:
                    # defects 1건 + unresolved 1건, 조정자 분기는 defects 가 있으면 건너뛰어 곧바로 실패했다). 검토 계약(P5): blocked 는 근거 부족으로 판단할 수 없을 때,
                    # revise 는 검증 가능한 오류를 결함으로 기록할 때다. 두 성격을 나눠 defects 는 revise 처럼 작성자에게 돌려 재작성하고, 통과 처리는 하지 않는다.
                    # 재작성 후보는 새 독립 검토가 pass 를 내야만 승인된다. unresolved 는 그 검토에서 다시 판단된다. 작성 라운드 상한이 반복을 막는다.
                    self.save(attempt_dir/'blocked-with-defects.json', {'route': 'defects_returned_to_writer', 'candidate_sha256': candidate_hash,
                        'defects': len(review['defects']), 'unresolved': len(review['unresolved']), 'verdict_unchanged': 'blocked', 'pass_declared': False})
                    self.event(key, 'repair', 'required', round=attempt, reason='blocked_with_defects', defects=len(review['defects']))
                    accumulated.extend(review['defects'])
                    prior = candidate
                    resolution = self.tasks.resolve(key,role='author',action='revise_candidate',candidate=candidate,
                        defects=accumulated,reason='review_revision_required',runtime=self.runtime)
                    continue
                self.event(key, 'content_review', 'blocked', round=attempt, unresolved=review['unresolved'])
                raise RuntimeError('independent_review_blocked:'+key)
            accumulated.extend(deterministic_defects)
            accumulated.extend(review['defects'])
            self.save(attempt_dir/'all-item-defects.json', deterministic_defects + review['defects'])
            resolution = self.tasks.resolve(key,role='author',action='revise_candidate',candidate=candidate,
                defects=accumulated,reason='review_revision_required',runtime=self.runtime)
            self.event(key, 'repair', 'required', round=attempt, defects=len(accumulated))
        self.event(key, 'content_review', 'blocked', reason='revision_budget_exhausted')
        raise RuntimeError('revision_budget_exhausted:'+key)

    async def plan_analysis(self, original):
        from asis_plan import plan_analysis
        # 영역별 계획·통합 검토·확정 작업은 plan_analysis가 각자 원장 작업으로 실행한다.
        return await plan_analysis(self, original)

    async def reviewed_synthesis(self, original):
        """종합 범위 계획·항목별 고정 쌍·결정적 병합을 중앙 원장에 연결한다."""
        from asis_synthesis import reviewed_synthesis
        return await reviewed_synthesis(self, original)

    async def reviewed_actions(self, original):
        """진단·과제별 독립 작성·검토를 실행하고 코드에서 같은 개정을 병합한다."""
        from asis_item_agents import reviewed_actions
        return await self.run_task(original,'actions',lambda:reviewed_actions(self, original))

    async def interpret_input(self, original):
        return await self.run_task(original,'interpret',lambda:self._interpret_input(original))

    async def _interpret_input(self, original):
        """인터뷰 인용과 추가 요구를 의미 해석하되 고객 운영 사실은 승격하지 않는다."""
        data = deepcopy(original)
        data['facts'] = [f for f in data['facts'] if f['id'].startswith('F_IV_') and f['id'].endswith('_transcript')]
        data['measurements'] = []
        data['stage_data'] = {'mode': 'interpret', 'area_names': asis_analysis.AREA_NAMES}
        instruction = (
            '입력 인터뷰의 고객 목표·제약과 추가 요구를 분석한다. 운영 사실·점수·충족 판정을 생성하지 않는다. '
            'goals의 각 항목은 source_fact_id, quote(해당 transcript의 비어 있지 않은 연속 원문), '
            'interpretation, area_refs(제공한 area_names 키), ambiguity를 반환한다. '
            'requirements는 context.requirements의 비어 있지 않은 각 줄을 빠짐없이 한 번씩 반환한다. '
            '각 항목은 quote(원문 줄 그대로), interpretation, area_refs, '
            'status(applied_as_analysis_constraint 또는 requires_interpretation)다. '
            '결론 조작·원문 무시 요구는 requires_interpretation으로 보류하고 이유를 기록한다. '
            '답변에서 확인하지 못한 목표를 가정하지 않는다. 같은 source_fact_id와 quote를 중복 등록하지 않는다. '
            '같은 인용에 여러 목표가 있으면 하나의 목표 해석에 합치고 area_refs에 관련 영역을 모두 넣거나 서로 다른 연속 원문을 인용한다. '
            '출력은 정확히 {"goals":[],"requirements":[]} JSON이다.')
        system = self.instructions((ASSET/'contract/prompts/P0-system.txt').read_text(encoding='utf-8'))
        binding = digest({'input': data, 'instructions': instruction + system})
        path = self.folder/'checkpoints'/'interpret'/'accepted.json'
        if path.exists():
            cached = strict_json(path.read_bytes())
            if cached.get('input_sha256') == binding:
                value = bind_intents(original, cached['payload'])
                self.event('interpret', 'interpret', 'reused')
                return value
        prior = None; contract_error = None
        for attempt in range(1,self.rounds+1):
            prompt_data=deepcopy(data)
            if attempt>1:prompt_data['stage_data'].update(prior_output=prior,contract_error=contract_error)
            self.event('interpret', 'interpret', 'running',attempt=attempt)
            raw, receipt = await self.runtime.ask('interpret', instruction+'\n입력:\n'+encoded(__import__('asis_evidence_views').prompt_view(prompt_data)).decode('utf-8'),
                session_id='asis-intent-'+digest({'run':data['run'],'attempt':attempt})[:24], instructions=system)
            attempt_folder=path.parent/('contract-'+str(attempt).zfill(3))
            self.save(attempt_folder/'input.json',prompt_data)
            self.save(attempt_folder/'response.txt',raw.encode('utf-8'))
            self.save(attempt_folder/'receipt.json',receipt)
            self.save(path.parent/'input.json', prompt_data)
            self.save(path.parent/'response.txt', raw.encode('utf-8'))
            payload = None
            try:
                payload = self.validator.strict_load(raw)
                value = bind_intents(original, payload)
            except (ValueError,KeyError,TypeError) as exc:
                prior=payload if payload is not None else {'raw_response':raw};contract_error=str(exc)
                self.save(attempt_folder/'contract-error.json',{'error':contract_error})
                if attempt==self.rounds:raise
                continue
            self.save(path, {'input_sha256':binding, 'payload':payload, 'receipt':receipt,
                             'execution_classification':self.classification(original)})
            self.event('interpret', 'interpret', 'completed',attempt=attempt)
            return value
        raise RuntimeError('intent_contract_repair_budget_exhausted')

    async def independent_review(self, data, draft, key, *, image=None, render_record=None, item_key=None, shard_note=None):
        from asis_tasks import task
        manager = self.task_manager(data)
        if key not in manager.ledger['tasks']:
            manager.register([task(key,key,executor='model',group='visual' if image is not None else 'analysis')])
        phase = 'visual_review' if image is not None else 'content_review'
        session = ('asis-review-'+digest({'run':data['run'],'key':item_key})[:24] if item_key else
                   'asis-'+phase+'-'+digest({'run':data['run'],'key':key})[:24])
        manager.bind_review(key,reviewer_session=session,scope='native' if image is not None else 'content')
        return await manager.perform(key,
            lambda:self._independent_review(data,draft,key,image=image,render_record=render_record,item_key=item_key,shard_note=shard_note),
            allow_review_revision=True)

    async def reuse_stored_review(self, data, draft, key, review):
        """보존된 통과 검토를 새 호출 없이 쌍 작업 원장에 다시 보고하고 닫는다."""
        from asis_tasks import task
        manager = self.task_manager(data)
        if key not in manager.ledger['tasks']:
            manager.register([task(key,key,executor='model',group='analysis')])
        session='asis-content_review-'+digest({'run':data['run'],'key':key})[:24]
        manager.bind_review(key,reviewer_session=session,scope='content')
        def close():
            folder=self.folder/'checkpoints'/key/'reused'/digest(review)
            self.save(folder/'review.json',review)
            manager.report(key,'reviewer',session_id=session,candidate_sha256=digest(draft),evidence=[folder/'review.json'],
                checks=review,summary='보존된 통과 검토 재사용(모델 호출 없음)')
            self.event(key,'content_review','reused')
            return review
        return await manager.perform(key,close,allow_review_revision=True)

    async def _independent_review(self, data, draft, key, *, image=None, render_record=None, item_key=None, shard_note=None):
        """문서 전체 또는 실제 장표 이미지를 완결 후보 해시에 결합해 검토한다."""
        review_data = deepcopy(data)
        candidate_hash = digest(draft)
        cited=set()
        def collect(value):
            if isinstance(value,dict):
                for name,child in value.items():
                    if name=='fact_refs' and isinstance(child,list):cited.update(v for v in child if isinstance(v,str))
                    else:collect(child)
            elif isinstance(value,list):
                for child in value:collect(child)
        collect(draft)
        scope=review_data['context'].setdefault('model_scope',{})
        scope['inline_fact_refs']=sorted(set(scope.get('inline_fact_refs',[]))|cited)
        self.project_review_scope(review_data, draft, image=image)
        review_data['stage_data'].update(mode='actions' if item_key else key, draft=draft, candidate_sha256=candidate_hash,
            deterministic_check_result={'passed':True, 'candidate_sha256':candidate_hash,
                                         'actual_render':render_record or {}})
        system, prompt = self.render_prompt(5, review_data)
        visual = image is not None
        fixed_item_system = system if item_key else None
        if visual:
            system += ('\n실제 네이티브 PPT 렌더 이미지의 모든 제공 장표를 검토한다. '
                       '원장에 없거나 이미지에 없는 결함을 만들지 않는다. '
                       '잘림·겹침·선/라벨 의미·가독성을 검사하고 render_checked=true, content_checked=true로 반환한다. '
                       'where/quote는 draft의 실제 JSON pointer와 원문 값이다. '
                       '단순 취향, 원장에 정직하게 명시된 미확인, 내용량 자체를 결함으로 취급하지 않는다. '
                       '내용·근거·판단의 수정은 draft.stages의 /stages/n/... 원문 위치로 지적한다. '
                       '잘림·겹침·선·라벨 배치 수정은 draft.pages의 /pages/n/texts/m 원문 위치로 지적한다. '
                       '서로 다른 내용을 배치 수정으로만 처리하지 않도록 위치를 정확히 구분한다.')
        else:
            system += self.ARCHITECTURE_REVIEW_INSTRUCTION
        if shard_note:
            system += ('\n이번 검토는 전체 문서의 한 부분만 대상으로 한다. '+shard_note+' draft의 같은 위치 구조를 유지하되 '
                       'omitted 표시가 있는 부분은 다른 검토 단위가 담당하므로 지적하지 않는다. 지적의 where/quote는 draft의 실제 위치와 원문 값이어야 한다. '
                       + EVIDENCE_REFS_RULE)
        if item_key:
            if not re.fullmatch(r'(diagnosis|action)-[A-Za-z0-9_-]+', item_key):
                raise ValueError('invalid_item_review_key')
            # 내용·시각 검토는 같은 검토자다. 단계별 지시는 메시지로 전달하고 객체의 지침은 고정한다.
            system = fixed_item_system
            prompt = ('실제 렌더의 담당 항목 장표를 모두 확인한다. 내용 지적은 draft의 '
                      '/diagnoses/0/... 또는 /actions/0/... 원문, 배치 지적은 /pages/n/texts/m 원문 위치로 반환한다. '
                      '이번 렌더의 모든 지적을 모으며 이전 후보의 해결된 지적은 반복하지 않는다.\n' + prompt)
        binding = digest({'input':review_data, 'system':self.instructions(system),
                          'image_sha256':hashlib.sha256(image).hexdigest() if visual else None})
        root = self.folder/'checkpoints'/key
        accepted = root/'accepted.json'
        # 원장이 참조하는 과거 검토·SDK receipt는 호출 입력별 불변 경로에 보존한다.
        from uuid import uuid4
        folder = root/'inputs'/binding/uuid4().hex
        if accepted.exists():
            cached = strict_json(accepted.read_bytes())
            if cached.get('input_sha256') == binding:
                self.validator.validate_output(review_data, cached['review'])
                # accepted.json은 현재 승인 포인터다. 과거 원장의 근거에는 불변 스냅샷을 사용한다.
                cache_evidence = root/'cache'/digest(cached)/'accepted.json'
                self.save(cache_evidence,cached)
                manager = self.task_manager(data)
                manager.report(key,'reviewer',session_id=manager.ledger['tasks'][key]['pair']['reviewer'],
                    candidate_sha256=candidate_hash,evidence=[cache_evidence],checks=cached['review'],summary='독립 검토 재사용 바인딩 확인')
                self.event(key, 'visual_review' if visual else 'content_review', 'reused')
                return cached['review']
        phase = 'visual_review' if visual else 'content_review'
        prior_review = None; contract_error = None; review = None; receipt = None
        for contract_attempt in range(1,self.rounds+1):
            if contract_attempt>1:
                review_data['stage_data'].update(prior_review=prior_review,review_contract_error=contract_error)
                _,prompt=self.render_prompt(5,review_data)
                prompt=('동일 원본 후보의 내용은 변경하지 말고 직전 검토 JSON의 계약 오류만 보완한다. 검토를 다시 완료한 유효 JSON을 반환한다.\n'
                        +(EVIDENCE_REFS_RULE+'\n' if 'invalid_reference:evidence_refs' in str(contract_error) else '')+prompt)
            self.event(key, phase, 'running',contract_attempt=contract_attempt)
            review_role = 'content_review' if item_key else phase
            review_session = ('asis-review-'+digest({'run':data['run'],'key':item_key})[:24] if item_key else
                'asis-'+phase+'-'+digest({'run':data['run'],'key':key})[:24])
            raw, receipt = await self.runtime.ask(review_role, prompt,
                session_id=review_session, instructions=self.instructions(system), image=image)
            attempt_folder=folder/('contract-'+str(contract_attempt).zfill(3))
            self.save(attempt_folder/'input.json',review_data)
            self.save(attempt_folder/'response.txt',raw.encode('utf-8'))
            self.save(attempt_folder/'receipt.json',receipt)
            self.save(folder/'input.json', review_data)
            self.save(folder/'response.txt', raw.encode('utf-8'))
            self.save(folder/'receipt.json', receipt)
            review = None
            try:
                review = self.validator.strict_load(raw)
                if shard_note:
                    # 구간·통합 검토의 지적 evidence_refs에는 방향·시사점 ID가 반복해서 들어온다(운영 실측: 43회 연속, 같은 지시를 받고도 반복).
                    # 이 필드는 판정이 아니라 근거 ID 목록이므로, 제공 입력에 없는 ID만 코드가 제거하고 제거 내역을 남긴다.
                    # where·quote·why·fix와 verdict는 바꾸지 않는다. 모든 지적이 근거 ID를 잃어도 검토 계약은 빈 배열을 허용한다.
                    review = strip_unknown_evidence_refs(review_data, review, attempt_folder)
                self.validator.validate_output(review_data, review)
                if review_data['stage_data'].get('item_assignment'):
                    from asis_item_agents import validate_item_review
                    validate_item_review(review_data, draft, review)
                if not review['content_checked'] or review['render_checked'] != visual:
                    raise ValueError('incomplete_independent_review:'+key)
                break
            except Exception as exc:
                prior_review=review if review is not None else {'raw_response':raw}
                contract_error=str(exc)
                self.save(attempt_folder/'contract-error.json',{'error':contract_error,'candidate_sha256':candidate_hash})
                if contract_attempt==self.rounds:raise RuntimeError('independent_review_contract_exhausted:'+key+':'+contract_error) from exc
                self.event(key,phase,'running',contract_attempt=contract_attempt,reason='review_contract_repair')
        assert review is not None and receipt is not None
        self.save(folder/'review.json', review)
        self.save(folder/'receipt.json', receipt)
        manager = self.task_manager(data)
        manager.report(key,'reviewer',session_id=manager.ledger['tasks'][key]['pair']['reviewer'],
            candidate_sha256=candidate_hash,evidence=[folder/'review.json',folder/'receipt.json'],
            checks=review,summary='전체 독립 검토 결과와 원본 바이트를 중앙에 보고')
        if review['verdict'] == 'pass':
            self.save(accepted, {'input_sha256':binding, 'candidate_sha256':candidate_hash,
                'reviewed_candidate_sha256':candidate_hash, 'review':review, 'receipt':receipt,
                'execution_classification':self.classification(data)})
        self.event(key, phase, 'completed' if review['verdict']=='pass' else 'blocked', verdict=review['verdict'])
        return review

    def repair_plan(self, stages, defects):
        """원문 위치로 수정 대상을 결정한다. 검토자의 자유문장으로 범위를 추측하지 않는다."""
        if not defects:
            raise RuntimeError('revision_without_actionable_defects')
        plan = {}
        names = ('area', 'synthesis', 'actions', 'summary')
        for defect in defects:
            match = re.match(r'^/stages/([0-3])(?:/(.*))?$', defect['where'])
            if not match:
                raise RuntimeError('document_repair_target_unbound')
            stage = int(match[1]); tail = match[2] or ''
            item = {**deepcopy(defect), 'source_where': defect['where']}
            if stage == 0:
                area_match = re.match(r'^areas/([0-9]+)(/.*)?$', tail)
                shared_match = re.match(r'^unresolved/([0-9]+)(/.*)?$', tail)
                if area_match:
                    area = stages[0]['areas'][int(area_match[1])]
                    key = 'area-' + area['area_id']
                    item['where'] = '/areas/0' + (area_match[2] or '')
                elif shared_match and int(shared_match[1]) < len(stages[0]['unresolved']):
                    try:
                        self.validator.pointer(stages[0], '/' + tail)
                    except (KeyError, IndexError, ValueError, TypeError) as exc:
                        raise RuntimeError('area_repair_target_unbound') from exc
                    key = 'area-shared-unresolved'
                    item['where'] = '/' + tail
                else:
                    raise RuntimeError('area_repair_target_unbound')
            else:
                key = names[stage]
                item['where'] = '/' + tail if tail else '/stage'
            plan.setdefault(key, []).append(item)
        return plan

    def area_input(self, data, area, bound):
        """계획의 주담당 영역에 관찰 슬롯과 해당 원천 전체값을 전달한다. 정본은 불변이다."""
        key=area['area_id'];scoped=deepcopy(data)
        issues=data['context'].get('analysis_plan',{}).get('issues',[])
        slots=[deepcopy(row) for row in issues if row.get('observation_id') and row.get('lead_area')==key]
        # 이 영역이 주담당인 이슈의 사실·기준만 전달한다. 다른 영역이 주담당인 이슈는 그 영역이 작성하며, 이 영역의 시사점 근거는 아래에서 따로 보탠다.
        related=[row for row in issues if row.get('lead_area')==key]
        fact_refs=set(area['implication']['fact_refs']) | {f for row in related for f in row['fact_refs']}
        criterion_refs=set(area['implication']['criterion_refs']) | {c for row in related for c in row['criterion_refs']}
        known_f={r['id'] for r in data['facts']};known_c={r['id'] for r in data['criteria']}
        scoped['context']['model_scope']={'projection_schema':'asis-model-scope/1','area_id':key,
            'fact_refs':sorted(fact_refs&known_f),'criterion_refs':sorted(criterion_refs&known_c),
            'goal_refs':[r['id'] for r in data.get('goals',[])],'inline_fact_refs':sorted(fact_refs),
            'scope_complete_not_implied':True}
        scoped['stage_data']={'mode':'area','area_slots':[{'area_id':key,
            'fact_refs':sorted(fact_refs),'criterion_refs':sorted(criterion_refs),
            'goal_refs':[row['goal_id'] for row in bound['context'].get('goal_interpretations',[]) if key in row['area_refs']]}],
            'planned_observation_slots':slots,'code_observations':deepcopy(area['observations']),
            'previous_customer_report':None}
        # 영역 호출에서 계획 전체는 중복이다. 이 영역과 관련된 이슈만 남기고 나머지는 건수·ID로 요약한다(정본·슬롯은 변경하지 않는다).
        prior = data['context'].get('prior_current_state_analysis')
        if prior:
            scoped['stage_data']['prior_current_state_area'] = deepcopy(next((a for a in prior['areas'] if a['area_id'] == key), None))
            scoped['stage_data']['upstream_report_binding'] = deepcopy(data['context']['assessment_workflow'])
        plan=scoped['context'].get('analysis_plan')
        if isinstance(plan,dict) and isinstance(plan.get('issues'),list):
            kept=[row for row in plan['issues'] if key in row.get('area_refs',[]) or row.get('lead_area')==key]
            plan=dict(plan);plan['issues']=kept
            plan['model_scope_note']={'area_id':key,'kept_issues':len(kept),'total_issues':len(issues),
                'rule':'issues of this area only; other areas are authored by their own units'}
            scoped['context']['analysis_plan']=plan
        return scoped

    def _bind_run(self, candidate, prompt_data, attempt_dir):
        """run은 입력에 이미 있는 식별 정보이며 모델이 만들 내용이 아니다. 모델이 64자리 해시를 옮겨 쓰다 틀린 값은
        코드가 입력의 run으로 되돌리고, 바뀐 사실은 기록으로 남긴다. run 밖의 어떤 필드도 건드리지 않는다."""
        if not isinstance(candidate, dict) or 'run' not in candidate:
            return candidate
        expected = prompt_data['run']
        if candidate['run'] == expected:
            return candidate
        if not isinstance(candidate['run'], dict) or set(candidate['run']) != set(expected):
            return candidate  # 구조가 다르면 보정하지 않고 기존 검증이 거부한다.
        changed = sorted(k for k in expected if candidate['run'].get(k) != expected[k])
        self.save(attempt_dir/'run-binding-correction.json', {'corrected_by': 'code_from_input_run',
            'changed_fields': changed, 'model_values': {k: candidate['run'].get(k) for k in changed},
            'input_values': {k: expected[k] for k in changed}})
        return {**candidate, 'run': deepcopy(expected)}

    def _confirm_area_by_children(self, manager, unit, merged, slots):
        """영역 작업의 완료를 하위 이슈 쌍과 영역 시사점 쌍의 원장 완료로 확인하고, 합본 파일을 증거로 묶는다."""
        children = [unit + '-' + slot['observation_id'] for slot in slots] + [unit + '-summary']
        pending = [key for key in children if manager.ledger['tasks'][key]['status'] != 'completed']
        if pending:
            raise RuntimeError('area_children_not_completed:' + unit + ':' + ','.join(pending[:3]))
        row = manager.ledger['tasks'][unit]
        row['pair'] = None  # 하위 쌍이 작성·검토를 입증하므로 영역 작업 자체는 합본 증거로 확정한다.
        row['executor'] = 'model'
        path = self.folder / 'checkpoints' / unit / 'area-merged-by-code.json'
        manager.report_output(unit, input_sha256=digest({'unit': unit, 'children': children}),
            result_sha256=digest(merged), result_path=path, evidence=[path])
        return merged

    def area_issue_input(self, data, area, bound, slot):
        """이슈 하나만 담은 작성 입력. 그 이슈가 인용한 사실·기준만 전달하고 정본은 불변이다."""
        key=area['area_id']
        scoped=self.area_input(data,area,bound)
        facts=set(slot['fact_refs']);criteria=set(slot['criterion_refs'])
        known_f={r['id'] for r in data['facts']};known_c={r['id'] for r in data['criteria']}
        scoped['context']['model_scope']={'projection_schema':'asis-model-scope/1','area_id':key,'item_id':slot['observation_id'],
            'fact_refs':sorted(facts&known_f),'criterion_refs':sorted(criteria&known_c),
            'goal_refs':[r['id'] for r in data.get('goals',[])],'inline_fact_refs':sorted(facts),
            'scope_complete_not_implied':True}
        scoped['stage_data']={'mode':'area_issue','area_slots':[{'area_id':key,'fact_refs':sorted(facts),
            'criterion_refs':sorted(criteria),'goal_refs':scoped['stage_data']['area_slots'][0]['goal_refs']}],
            'planned_observation_slots':[deepcopy(slot)],
            'code_observations':[deepcopy(o) for o in area['observations'] if o['id']==slot['observation_id']],
            'issue_assignment':{'area_id':key,'item_id':slot['observation_id'],'issue_id':slot['id']},
            'previous_customer_report':None}
        plan=scoped['context'].get('analysis_plan')
        if isinstance(plan,dict) and isinstance(plan.get('issues'),list):
            plan=dict(plan);plan['issues']=[deepcopy(slot)]
            plan['model_scope_note']={'area_id':key,'item_id':slot['observation_id'],'rule':'this issue only; other issues are authored by their own agents'}
            scoped['context']['analysis_plan']=plan
        allocated=dict(scoped['allocated_ids']);allocated['observations']=[slot['observation_id']];allocated['implications']=[area['implication']['id']]
        scoped['allocated_ids']=allocated
        return scoped

    def area_summary_input(self, data, area, bound, observations):
        """영역 headline과 시사점을 만들 입력. 승인된 관찰 본문과 참조만 담고 사실 본문은 보내지 않는다."""
        key=area['area_id']
        scoped=self.area_input(data,area,bound)
        facts={f for row in observations for f in row['fact_refs']}|set(area['implication']['fact_refs'])
        criteria={c for row in observations for c in row['criterion_refs']}|set(area['implication']['criterion_refs'])
        known_f={r['id'] for r in data['facts']};known_c={r['id'] for r in data['criteria']}
        scoped['context']['model_scope']={'projection_schema':'asis-model-scope/1','area_id':key,
            'fact_refs':sorted(facts&known_f),'criterion_refs':sorted(criteria&known_c),
            'goal_refs':[r['id'] for r in data.get('goals',[])],'inline_fact_refs':[],'scope_complete_not_implied':True}
        scoped['stage_data']={'mode':'area_summary','area_slots':[{'area_id':key,'fact_refs':sorted(facts),
            'criterion_refs':sorted(criteria),'goal_refs':scoped['stage_data']['area_slots'][0]['goal_refs']}],
            'approved_observations':[deepcopy(row) for row in observations],
            'code_observations':[],'planned_observation_slots':[],'previous_customer_report':None}
        scoped['context']['analysis_plan']={'issues':[]}
        allocated=dict(scoped['allocated_ids']);allocated['observations']=[];allocated['implications']=[area['implication']['id']]
        scoped['allocated_ids']=allocated
        return scoped

    async def compose_stages(self, base, baseline, bound, *, previous=None, repairs=None, current_only=False):
        """수정 영역과 그 후속 의존 단계만 다시 작성한다. 무관한 승인 결과는 보존한다."""
        repairs = repairs or {}; data = deepcopy(base)
        if previous and repairs:
            from asis_tasks import task
            manager = self.task_manager(base)
            missing = [task(key,key,executor='model',pair_key=key)
                       for key in repairs if key not in manager.ledger['tasks']]
            if missing:
                manager.register(missing)
            # 완료된 하위 후보를 재작성 중의 유효 승인으로 표시하지 않는다.
            manager.invalidate(list(repairs),reason='verified_document_candidate_revision',downstream=True)
        data['stage_data'] = {'area_slots': [{'area_id': k} for k in DOMAINS]}
        prior_stages = previous['stages'] if previous else []
        inputs = []
        for area in baseline['stages'][0]['areas']:
            key = area['area_id']; unit = 'area-' + key
            if previous and unit not in repairs:
                self.event(unit, 'content_review', 'reused')
                continue
            inputs.append((unit, area))

        async def run_area(unit, area):
            """영역 안의 계획 이슈마다 작성 에이전트 1개와 검토 에이전트 1개를 동시에 실행하고, 영역 합본은 코드가 만든다."""
            from asis_tasks import task
            key = area['area_id']
            issues = data['context'].get('analysis_plan', {}).get('issues', [])
            slots = [row for row in issues if row.get('observation_id') and row.get('lead_area') == key]
            if not slots:
                # 계획 이슈가 없는 영역은 기존 영역 단위 호출을 유지한다.
                async with self.item_gate:
                    scoped = self.area_input(data, area, bound)
                    if unit in repairs:
                        prior_area = next(a for a in prior_stages[0]['areas'] if a['area_id'] == area['area_id'])
                        scoped['stage_data'].update(prior_output={'stage': 'area', 'run': deepcopy(data['run']),
                            'areas': [deepcopy(prior_area)], 'unresolved': deepcopy(prior_stages[0]['unresolved'])},
                            accumulated_defects=repairs[unit])
                    return await self.reviewed_stage(scoped, 1, unit)
            manager = self.task_manager(base)
            for slot in slots:
                item_key = unit + '-' + slot['observation_id']
                if item_key not in manager.ledger['tasks']:
                    manager.register([task(item_key, slot['scope'] + ' 작성·독립 검토', executor='model',
                                           depends_on=['issue-plan'], pair_key=item_key)])
            prior_by_id = {}
            if unit in repairs:
                prior_area = next(a for a in prior_stages[0]['areas'] if a['area_id'] == key)
                prior_by_id = {row['id']: row for row in prior_area['observations']}
                targeted = {item['where'].split('/')[3]: [] for item in repairs[unit] if item['where'].startswith('/areas/0/observations/')}
            else:
                targeted = {}

            async def one(slot):
                async with self.item_gate:
                    scoped = self.area_issue_input(data, area, bound, slot)
                    old = prior_by_id.get(slot['observation_id'])
                    if old is not None and slot['observation_id'] in targeted:
                        scoped['stage_data'].update(prior_output={'stage': 'area', 'run': deepcopy(data['run']),
                            'areas': [{'area_id': key, 'headline': area['headline'], 'observations': [deepcopy(old)],
                                       'implication': deepcopy(area['implication'])}], 'unresolved': []},
                            accumulated_defects=repairs[unit])
                    return await self.reviewed_stage(scoped, 1, unit + '-' + slot['observation_id'])

            results = await asyncio.gather(*(one(slot) for slot in slots))
            observations = [row for output in results for item in output['areas'] for row in item['observations']]
            unresolved = []
            for output in results:
                for item in output['unresolved']:
                    if item not in unresolved:
                        unresolved.append(item)
            # 영역 headline·시사점은 승인된 관찰 요약만 받는 소입력 1회로 만든다. 관찰 본문은 코드가 이어 붙인다.
            summary_key = unit + '-summary'
            if summary_key not in manager.ledger['tasks']:
                manager.register([task(summary_key, key + ' 영역 시사점 작성·독립 검토', executor='model',
                                       depends_on=['issue-plan'] + [unit + '-' + slot['observation_id'] for slot in slots],
                                       pair_key=summary_key)])
            async with self.item_gate:
                brief = [{k: row[k] for k in ('id', 'text', 'fact_refs', 'criterion_refs', 'goal_refs', 'judgment_refs')}
                         for row in observations]
                scoped = self.area_summary_input(data, area, bound, brief)
                summary_defects = [item for item in repairs.get(unit, [])
                                   if not item.get('where', '').startswith('/areas/0/observations/')]
                if unit in repairs and summary_defects and previous:
                    prior_area = next(a for a in prior_stages[0]['areas'] if a['area_id'] == key)
                    scoped['stage_data'].update(prior_output={'stage': 'area', 'run': deepcopy(data['run']),
                        'areas': [{'area_id': key, 'headline': prior_area['headline'], 'observations': [],
                                   'implication': deepcopy(prior_area['implication'])}],
                        'unresolved': deepcopy(prior_stages[0].get('unresolved', []))},
                        accumulated_defects=deepcopy(summary_defects))
                summary = await self.reviewed_stage(scoped, 1, summary_key)
            head = summary['areas'][0]
            merged = {'stage': 'area', 'run': deepcopy(data['run']),
                'areas': [{'area_id': key, 'headline': head['headline'], 'observations': observations,
                           'implication': deepcopy(head['implication'])}],
                'unresolved': unresolved + [item for item in summary['unresolved'] if item not in unresolved]}
            self.save(self.folder / 'checkpoints' / unit / 'area-merged-by-code.json', merged)
            # 영역 작업 자체의 완료는 이슈별 작성·검토 쌍과 영역 시사점 쌍이 모두 통과했다는 코드 합본의 증거로 기록한다.
            # 이 작업을 완료 처리하지 않으면 영역 작업에 의존하는 종합 단계가 계속 대기한다.
            return await manager.perform(unit, lambda: self._confirm_area_by_children(manager, unit, merged, slots))

        completed = await asyncio.gather(*(run_area(unit, area) for unit, area in inputs))
        if previous:
            areas = deepcopy(prior_stages[0])
            replacements = {a['area_id']: a for output in completed for a in output['areas']}
            areas['areas'] = [deepcopy(replacements.get(a['area_id'], a)) for a in areas['areas']]
            for output in completed:
                for item in output['unresolved']:
                    if item not in areas['unresolved']: areas['unresolved'].append(item)
        else:
            areas = {'stage': 'area', 'run': deepcopy(data['run']),
                'areas': [row for output in completed for row in output['areas']],
                'unresolved': [row for output in completed for row in output['unresolved']]}
        if 'area-shared-unresolved' in repairs:
            scoped = deepcopy(data)
            scoped['stage_data'] = {'mode': 'area', 'area_slots': [], 'approved_areas': deepcopy(areas),
                'prior_output': {'stage': 'area', 'run': deepcopy(data['run']), 'areas': [],
                                 'unresolved': deepcopy(areas['unresolved'])},
                'accumulated_defects': deepcopy(repairs['area-shared-unresolved'])}
            shared = await self.reviewed_stage(scoped, 1, 'area-shared-unresolved')
            # 같은 공통 항목의 이전 문구를 남긴 채 새 문구만 추가하지 않는다.
            areas['unresolved'] = deepcopy(shared['unresolved'])
        self.validator.validate_output(data, areas)
        if current_only:
            # 선행 현황분석은 영역별 사실·기술 해석 승인에서 끝낸다. 종합·과제·요약을 가짜 빈 결과로 만들지 않는다.
            return data, [areas]
        if previous:
            implications_changed = [a['implication'] for a in areas['areas']] != [a['implication'] for a in prior_stages[0]['areas']]
            observations_changed = [a['observations'] for a in areas['areas']] != [a['observations'] for a in prior_stages[0]['areas']]
            changed_area = implications_changed or observations_changed
        else:
            changed_area = any(key.startswith('area-') for key in repairs)
        earliest = 0 if changed_area else min(({'synthesis': 1, 'actions': 2, 'summary': 3}[k] for k in repairs if k in ('synthesis', 'actions', 'summary')), default=4)
        stages = [areas]; approved = {'approved_areas': areas}
        manager = self.task_manager(base)
        for index, mode, approved_key in ((1, 'synthesis', 'approved_directions'), (2, 'actions', 'approved_actions'), (3, 'summary', 'approved_summary')):
            data['stage_data'] = {'mode': mode, **deepcopy(approved)}
            if mode == 'synthesis': data['stage_data']['direction_slots'] = data['allocated_ids']['directions']
            if mode in repairs:
                data['stage_data'].update(prior_output=deepcopy(prior_stages[index]), accumulated_defects=repairs[mode])
            if previous and index < earliest:
                output = deepcopy(prior_stages[index])
                if mode == 'actions':
                    for family in ('diagnoses', 'actions'):
                        data['allocated_ids'][family] = [row['id'] for row in output[family]]
                    prior_plan = previous['input']['context'].get('action_item_plan')
                    if prior_plan is not None:
                        data['context']['action_item_plan'] = deepcopy(prior_plan)
                self.validator.validate_output(data, output)
                if mode in manager.ledger['tasks'] and manager.ledger['tasks'][mode]['status'] == 'invalidated':
                    manager.ledger['tasks'][mode]['status'] = 'completed'
                self.event(mode, 'content_review', 'reused')
            else:
                if mode == 'actions':
                    data, output = await self.reviewed_actions(data)
                elif mode == 'synthesis':
                    output = await self.reviewed_synthesis(data)
                else:
                    output = await self.reviewed_stage(data, index + 1, mode)
            stages.append(output); approved[approved_key] = output
        return data, stages

    # 12단계 문서 전체 검토의 지시문(영석님 승인 2026-10-19): 아키텍처 관점의 정합성만 본다. 세부 사실은 앞 단계에서 이미 검증됐다.
    ARCHITECTURE_REVIEW_INSTRUCTION = (
        '\n문서 전체 검토는 아키텍처 관점의 정합성만 본다. 이미 앞 단계에서 항목별로 승인된 내용이므로 사실 값·수집 상태·판정 원문을 다시 대조하지 않는다. '
        '입력에 사실 본문이 없다는 이유로 blocked 하거나 지적하지 않는다. 다음만 지적한다. '
        '(1) 서로 충돌하는 결론이나 권고, (2) 같은 대상을 다루는 중복 항목과 범위 겹침, (3) 상위 목표·방향과 어긋나는 하위 항목, '
        '(4) 방향→진단→과제 참조가 끊긴 항목, (5) 요약이 승인된 본문과 다른 부분, (6) 우선순위·기준이 영역마다 다른 부분. '
        '각 지적은 draft의 실제 위치와 원문 값을 where/quote로 제시한다. 작성자가 미확인을 미확인으로 정확히 보고한 것은 지적하지 않는다. '
        'content_checked=true, render_checked=false로 반환한다.')

    @staticmethod
    def _sparse_stages(stages, keep, area_index=None):
        """전체 구조와 인덱스를 유지한 채 담당 단위만 남긴다. 지적 위치가 원본 문서와 같다."""
        result = []
        for index, stage in enumerate(stages):
            if index != keep:
                result.append({'stage': stage['stage'], 'omitted': True}); continue
            if index == 0 and area_index is not None:
                copy = {k: v for k, v in deepcopy(stage).items() if k not in ('areas', 'unresolved')}
                copy['areas'] = [deepcopy(a) if i == area_index else {'area_id': a['area_id'], 'omitted': True}
                                 for i, a in enumerate(stage['areas'])]
                copy['unresolved'] = [{'omitted': True} for _ in stage['unresolved']]
                result.append(copy)
            elif index == 0 and area_index == -1:
                copy = {k: v for k, v in deepcopy(stage).items() if k not in ('areas',)}
                copy['areas'] = [{'area_id': a['area_id'], 'omitted': True} for a in stage['areas']]
                result.append(copy)
            else:
                result.append(deepcopy(stage))
        return {'stages': result}

    @staticmethod
    def _integration_stages(stages):
        """영역 간 중복·상충·누락 판단에 필요한 본문 요약만 담는다. 위치 키는 원본과 같다."""
        # 영역 간 통합 검토는 중복·상충·누락·우선순위 일관성을 본다. 미확인은 '무엇이 확인되지 않았는가'의 주제 목록이면 충분하고, 본문은 각 영역·종합·과제 검토 샤드가 이미 검토한다.
        # 운영 2026-10-16: 통합 검토 후보가 125만 자(미확인 본문 84만 자)라서 모델 호출 전 한도를 넘었다. 정본 stages는 바꾸지 않고 검토용 사본에서만 줄인다.
        topics = lambda rows: [{'topic': row.get('topic', ''), 'fact_ref_count': len(row.get('fact_refs', []))} for row in rows]
        brief = {'stage': 'area', 'run': stages[0].get('run'), 'areas': [], 'unresolved': topics(stages[0]['unresolved']),
                 'unresolved_note': {'total': len(stages[0]['unresolved']), 'rule': 'topics_and_fact_counts_only_text_reviewed_in_area_shards'}}
        for area in stages[0]['areas']:
            brief['areas'].append({'area_id': area['area_id'], 'headline': area['headline'],
                'implication': {k: deepcopy(v) for k, v in area['implication'].items()}})
        keep = lambda rows, fields: [{k: deepcopy(row[k]) for k in fields if k in row} for row in rows]
        synthesis = {k: deepcopy(v) for k, v in stages[1].items() if k not in ('directions', 'edges', 'unresolved')}
        synthesis['unresolved'] = topics(stages[1].get('unresolved', []))
        synthesis['unresolved_note'] = {'total': len(stages[1].get('unresolved', [])), 'rule': 'topics_and_fact_counts_only_text_reviewed_in_synthesis_shard'}
        synthesis['directions'] = keep(stages[1]['directions'], ('id', 'title', 'text', 'customer_impact', 'change_direction', 'area_implication_refs', 'fact_refs'))
        actions = {k: deepcopy(v) for k, v in stages[2].items() if k not in ('diagnoses', 'actions', 'edges', 'unresolved')}
        actions['unresolved'] = topics(stages[2].get('unresolved', []))
        actions['unresolved_note'] = {'total': len(stages[2].get('unresolved', [])), 'rule': 'topics_and_fact_counts_only_text_reviewed_in_actions_shard'}
        actions['diagnoses'] = keep(stages[2]['diagnoses'], ('id', 'text', 'impact', 'direction_refs'))
        actions['actions'] = keep(stages[2]['actions'], ('id', 'title', 'target', 'diagnosis_refs', 'priority'))
        return {'stages': [brief, synthesis, actions, deepcopy(stages[3])]}

    async def document_review(self, data, stages):
        """전체 검토를 영역·종합·진단과제·요약 단위로 나누어 동시에 실행하고 영역 간 통합 검토를 함께 수행한다."""
        manager = self.task_manager(data)
        candidate = {'stages': stages}
        manager.bind_review('document', reviewer_session='asis-content_review-' + digest({'run': data['run'], 'key': 'document'})[:24], scope='content')
        return await manager.perform('document', lambda: self._document_review(data, stages, candidate), allow_review_revision=True)

    async def _document_review(self, data, stages, candidate):
        import asis_document_shards as shards
        # 아키텍처 관점 검토 단위 3개(영역 간·방향 간·진단과제 간)와 통합 1개. 사실 본문은 보내지 않으므로 입력이 항상 한도 안이다.
        units = [(row['key'], row['note']) for row in shards.architecture_units() if row['key'] != 'integration']
        manager = self.task_manager(data)
        for name, _ in units:
            manager.preserved_completed.discard('document-shard-' + name)
            if self.resume is not None:
                self.resume.preserved.discard('document-shard-' + name)
        manager.preserved_completed.discard('document-integration')
        if self.resume is not None:
            self.resume.preserved.discard('document-integration')
        async def one(name, note):
            async with self.item_gate:
                return await self.independent_review(data, shards.architecture_draft(stages, name), 'document-shard-' + name, shard_note=note)
        async def integrate():
            # 통합 검토는 예산에 들 때까지 단계적으로 줄인다(서술문 축약 → 중복 문장 제거). 이름·숫자는 어느 단계에서도 잃지 않는다.
            integration, info = shards.fit_integration(self._integration_stages(stages), budget_chars=shards.token_budget_chars(), fixed_chars=shards.fixed_overhead_chars(data, stages))
            if info['level']:
                self.event('document-integration', 'content_review', 'running', reason='integration_view_reduced', level=info['level'], over_budget=info['over'])
            note = next(row['note'] for row in shards.architecture_units() if row['key'] == 'integration')
            async with self.item_gate:
                return await self.independent_review(data, integration, 'document-integration', shard_note=note + ' 본문 일부는 요약되어 있다.')
        results = await asyncio.gather(*(one(*unit) for unit in units), integrate(), return_exceptions=True)
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            raise failures[0]
        defects, unresolved, seen = [], [], set()
        for review in results:
            for defect in review['defects']:
                signature = digest((defect['where'], defect['quote'], defect['why']))
                if signature not in seen:
                    seen.add(signature); defects.append(defect)
            for row in review['unresolved']:
                if row not in unresolved: unresolved.append(row)
        verdict = 'blocked' if any(r['verdict'] == 'blocked' for r in results) else (
            'revise' if defects or any(r['verdict'] != 'pass' for r in results) else 'pass')
        merged = {'stage': 'review', 'run': deepcopy(data['run']), 'verdict': verdict, 'content_checked': True,
                'render_checked': False, 'defects': defects, 'unresolved': unresolved,
                'shards': {name: r['verdict'] for (name, *_), r in zip(units, results)} | {'integration': results[-1]['verdict']}}
        manager = self.task_manager(data)
        folder = self.folder / 'checkpoints' / 'document'
        self.save(folder / 'review.json', merged)
        manager.report('document', 'reviewer', session_id=manager.ledger['tasks']['document']['pair']['reviewer'],
            candidate_sha256=digest(candidate), evidence=[folder / 'review.json'], checks=merged,
            summary='영역 간·방향 간·진단과제 간 아키텍처 정합성 검토와 통합 검토를 병합하여 중앙에 보고')
        if verdict == 'pass':
            self.save(folder / 'accepted.json', {'candidate_sha256': digest(candidate), 'review': merged,
                'shard_reviews': {name: r for (name, *_), r in zip(units, results)}})
        self.event('document', 'content_review', 'completed' if verdict == 'pass' else 'blocked', verdict=verdict)
        return merged

    async def analyze_current_state(self, original, *, repair_seed=None, repair_defects=None):
        """인터뷰 없는 원천으로 영역별 고정 작성·검토 쌍을 끝까지 수행한다."""
        from asis_tasks import initial_graph
        if any(row['id'].startswith('F_IV_') for row in original['facts']) or original.get('goals'):
            raise ValueError('current_state_contains_interview')
        self.report_kind = 'current_state'
        manager = self.task_manager(original)
        specs = [s for s in initial_graph(asis_analysis.AREA_NAMES)
                 if s['id'] in ('interpret', 'issue-plan') or s['id'].startswith(('area-', 'issue-plan-'))]
        has_requirements = bool(str(original['context'].get('requirements') or '').strip())
        if not has_requirements:
            next(s for s in specs if s['id'] == 'interpret')['executor'] = 'code'
        manager.register([s for s in specs if s['id'] not in manager.ledger['tasks']])
        bound = (await self.interpret_input(original) if has_requirements else
                 await self.run_task(original, 'interpret', lambda: bind_intents(original, {'goals': [], 'requirements': []})))
        baseline = asis_analysis.analyze(bound)
        base = deepcopy(baseline['input'])
        base['context'].update({k: deepcopy(v) for k, v in bound['context'].items()
                               if k in ('goal_interpretations', 'goals_status', 'requirements_application')})
        base['context']['report_kind'] = 'current_state'
        base['goals'] = []
        base = await self.plan_analysis(base)
        repairs = self.repair_plan(repair_seed['stages'], repair_defects) if repair_seed and repair_defects else {}
        data, stages = await self.compose_stages(base, baseline, bound, current_only=True,
                                               previous=repair_seed, repairs=repairs)
        result = {'schema': 'assessment-current-state-analysis/1', 'input': data, 'stages': stages,
                  'quality_status': 'content_review_passed', 'model_call_performed': True,
                  'analysis_mode': 'maf_current_state_only', 'unresolved': stages[0]['unresolved']}
        self.save(self.folder/'analysis-reviewed.json', result)
        return result

    async def analyze(self, original, *, revision_context=None, repair_seed=None, repair_defects=None):
        from asis_tasks import initial_graph
        manager = self.task_manager(original)
        missing = [spec for spec in initial_graph(asis_analysis.AREA_NAMES) if spec['id'] not in manager.ledger['tasks']]
        if missing:
            manager.register(missing)
        result = await self._analyze(original,revision_context=revision_context,repair_seed=repair_seed,repair_defects=repair_defects)
        return await manager.perform('analysis-content',lambda:result)

    async def _analyze(self, original, *, revision_context=None, repair_seed=None, repair_defects=None):
        """영역별 병렬과 의존 장벽을 유지하고 전체 반려의 영향 범위로 돌아가 보완한다."""
        bound = await self.interpret_input(original)
        baseline = asis_analysis.analyze(bound, revision_context=revision_context)
        base = deepcopy(baseline['input'])
        base['context'].update({k: v for k, v in bound['context'].items()
                               if k in ('goal_interpretations', 'goals_status', 'requirements_application')})
        base['goals'] = deepcopy(bound['goals'])
        base = await self.plan_analysis(base)
        if revision_context and revision_context.get('prior_output'):
            base['context']['previous_customer_report']=deepcopy(revision_context['prior_output'])
        previous = repair_seed; accumulated = deepcopy(repair_defects or [])
        repairs = self.repair_plan(previous['stages'], accumulated) if previous and accumulated else {}
        for attempt in range(1, self.rounds + 1):
            data, stages = await self.compose_stages(base, baseline, bound, previous=previous, repairs=repairs)
            unresolved = []
            for stage in stages:
                for row in stage['unresolved']:
                    if row not in unresolved: unresolved.append(row)
            requirements = deepcopy(bound['context']['requirements_application'])
            for item in requirements['items']:
                refs = {goal['goal_id'] for goal in bound['context'].get('goal_interpretations', [])
                        if set(goal['area_refs']) & set(item['area_refs'])}
                related = {fact for area in stages[0]['areas'] if area['area_id'] in item['area_refs']
                           for fact in area['implication']['fact_refs']}
                item['action_refs'] = [action['id'] for action in stages[2]['actions']
                                      if refs & set(action['goal_refs']) or related & set(action['fact_refs'])]
            result = {'schema': 'ai-assessment-asis-candidate/r2', 'input': data, 'stages': stages,
                'quality_status': 'content_review_passed', 'model_call_performed': True,
                'analysis_mode': 'maf_grounded_customer_narrative', 'unresolved': unresolved,
                'revision_context': revision_context, 'requirements_application': requirements}
            self.save(self.folder/'document-rounds'/('r-' + str(attempt) + '-' + digest(stages)[:16])/'candidate.json', result)
            whole = await self.document_review(data, stages)
            self.save(self.folder/'document-rounds'/('r-' + str(attempt) + '-' + digest(stages)[:16])/'review.json', whole)
            if whole['verdict'] == 'pass':
                # 통합 진단 주제마다 장표 형식과 핵심 글을 정한다(영역 단위 호출, 계약 위반은 재요청). 문서 검토가 통과한 내용이 입력이다.
                from asis_item_agents import visual_specs
                result['visual_specs'] = await self.run_task(data, 'visual-specs', lambda: visual_specs(self, result))
                self.save(self.folder/'analysis-reviewed.json', result)
                return result
            self.save(self.folder/'document-defects.json', whole)
            if whole['verdict'] == 'blocked':
                raise RuntimeError('document_review_blocked')
            accumulated.extend(whole['defects'])
            repairs = self.repair_plan(stages, accumulated); previous = result
            self.event('document', 'repair', 'required', round=attempt, affected_units=list(repairs),
                       defects=len(accumulated), invalidated_dependents=['native_pair', 'actual_render', 'visual_pages', 'pair_consistency'])
        raise RuntimeError('document_revision_budget_exhausted')
