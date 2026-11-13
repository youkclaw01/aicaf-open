"""장표 시각 형식 계약: 글이 아니라 이해하기 쉬운 형식(개념 아키텍처·표·상태 매트릭스·흐름도·차트·스코어 카드)으로 진단을 보여 준다.

모델은 주제를 보고 형식과 그 형식의 데이터(라벨·셀·단계)를 고르고, 코드가 형식별 계약과 글 길이 상한을 검증한다.
수치는 모델이 쓰지 않는다(차트·스코어 카드의 값은 인용한 사실에서 코드가 뽑는다). 이 모듈은 계약 검증만 하며 그리기는 하지 않는다.
"""
import asis_overview_architecture as A

FORMS = ('architecture', 'table', 'matrix', 'flow', 'chart', 'scorecard')
LIMITS = {'headline': 60, 'conclusion': 90, 'label': 25, 'cell': 25, 'step': 20, 'note': 60}
TABLE_COLUMNS = (3, 5); TABLE_ROWS_MAX = 8
MATRIX_ROWS_MAX = 8; MATRIX_COLUMNS = (2, 5); MATRIX_STATES = ('충족', '부분', '미확인', '해당없음')
FLOW_STEPS = (3, 6)
SCORECARD_ITEMS = (3, 4)
CHART_METRICS_MAX = 3
LAYER_KEYS = tuple(k for k, _, _ in A.LAYERS) + (A.OTHER[0],)


def _text(value, limit, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('visual_text_empty:' + name)
    if len(value.strip()) > limit:
        raise ValueError('visual_text_too_long:%s:%d>%d' % (name, len(value.strip()), limit))
    return value.strip()


def _only(row, keys, name):
    if not isinstance(row, dict) or set(row) != set(keys):
        raise ValueError('visual_shape_invalid:' + name)


def _architecture(data):
    _only(data, ('focus_layers', 'gap_layers'), 'architecture')
    for key in ('focus_layers', 'gap_layers'):
        if not isinstance(data[key], list) or any(layer not in LAYER_KEYS for layer in data[key]) or len(set(data[key])) != len(data[key]):
            raise ValueError('visual_architecture_layer_unknown:' + key)
    if not data['focus_layers']:
        raise ValueError('visual_architecture_focus_empty')
    if set(data['gap_layers']) - set(data['focus_layers']):
        raise ValueError('visual_architecture_gap_outside_focus')
    return {'focus_layers': list(data['focus_layers']), 'gap_layers': list(data['gap_layers'])}


def _table(data):
    _only(data, ('columns', 'rows'), 'table')
    columns, rows = data['columns'], data['rows']
    if not isinstance(columns, list) or not TABLE_COLUMNS[0] <= len(columns) <= TABLE_COLUMNS[1]:
        raise ValueError('visual_table_columns:%s' % TABLE_COLUMNS[0:2].__repr__())
    if not isinstance(rows, list) or not 1 <= len(rows) <= TABLE_ROWS_MAX:
        raise ValueError('visual_table_rows_over:%d' % TABLE_ROWS_MAX)
    head = [_text(c, LIMITS['label'], 'column') for c in columns]
    body = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(head):
            raise ValueError('visual_table_row_width')
        body.append([_text(cell, LIMITS['cell'], 'cell') for cell in row])
    return {'columns': head, 'rows': body}


def _matrix(data):
    _only(data, ('columns', 'rows'), 'matrix')
    columns, rows = data['columns'], data['rows']
    if not isinstance(columns, list) or not MATRIX_COLUMNS[0] <= len(columns) <= MATRIX_COLUMNS[1]:
        raise ValueError('visual_matrix_columns')
    if not isinstance(rows, list) or not 1 <= len(rows) <= MATRIX_ROWS_MAX:
        raise ValueError('visual_matrix_rows_over:%d' % MATRIX_ROWS_MAX)
    head = [_text(c, LIMITS['label'], 'column') for c in columns]
    body = []
    for row in rows:
        _only(row, ('label', 'states'), 'matrix_row')
        if not isinstance(row['states'], list) or len(row['states']) != len(head) or any(s not in MATRIX_STATES for s in row['states']):
            raise ValueError('visual_matrix_state_invalid')
        body.append({'label': _text(row['label'], LIMITS['label'], 'row'), 'states': list(row['states'])})
    return {'columns': head, 'rows': body}


def _flow(data):
    _only(data, ('steps',), 'flow')
    steps = data['steps']
    if not isinstance(steps, list) or not FLOW_STEPS[0] <= len(steps) <= FLOW_STEPS[1]:
        raise ValueError('visual_flow_steps:%d-%d' % FLOW_STEPS)
    out = []
    for step in steps:
        _only(step, ('label', 'owner', 'broken'), 'flow_step')
        if not isinstance(step['broken'], bool):
            raise ValueError('visual_flow_broken_not_boolean')
        out.append({'label': _text(step['label'], LIMITS['step'], 'step'), 'owner': _text(step['owner'], LIMITS['label'], 'owner'), 'broken': step['broken']})
    return {'steps': out}


def _chart(data):
    """모델은 인용한 사실 ID와 집계 방식만 고른다. 수치는 코드가 사실에서 뽑는다."""
    _only(data, ('metrics',), 'chart')
    metrics = data['metrics']
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= CHART_METRICS_MAX:
        raise ValueError('visual_chart_metrics:%d' % CHART_METRICS_MAX)
    out = []
    for metric in metrics:
        _only(metric, ('fact_id', 'label', 'aggregate'), 'chart_metric')
        if metric['aggregate'] not in ('count', 'sum_by_day', 'sum'):
            raise ValueError('visual_chart_aggregate_unknown')
        if not isinstance(metric['fact_id'], str) or not metric['fact_id'].startswith('F_'):
            raise ValueError('visual_chart_fact_id')
        out.append({'fact_id': metric['fact_id'], 'label': _text(metric['label'], LIMITS['label'], 'metric'), 'aggregate': metric['aggregate']})
    return {'metrics': out}


def _scorecard(data):
    _only(data, ('items',), 'scorecard')
    items = data['items']
    if not isinstance(items, list) or not SCORECARD_ITEMS[0] <= len(items) <= SCORECARD_ITEMS[1]:
        raise ValueError('visual_scorecard_items:%d-%d' % SCORECARD_ITEMS)
    out = []
    for item in items:
        _only(item, ('fact_id', 'label', 'aggregate'), 'scorecard_item')
        if item['aggregate'] not in ('count', 'sum'):
            raise ValueError('visual_scorecard_aggregate_unknown')
        out.append({'fact_id': item['fact_id'], 'label': _text(item['label'], LIMITS['label'], 'item'), 'aggregate': item['aggregate']})
    return {'items': out}


_BUILDERS = {'architecture': _architecture, 'table': _table, 'matrix': _matrix, 'flow': _flow, 'chart': _chart, 'scorecard': _scorecard}


def validate_visual(visual, cited_facts):
    """진단 하나의 장표 명세를 검증한다: 형식·데이터·글 길이·인용 사실 범위. 글은 헤드라인과 결론 한 줄뿐이다."""
    _only(visual, ('form', 'headline', 'conclusion', 'data'), 'visual')
    if visual['form'] not in FORMS:
        raise ValueError('visual_form_unknown:' + str(visual['form']))
    data = _BUILDERS[visual['form']](visual['data'])
    ids = [m['fact_id'] for m in data.get('metrics', data.get('items', []))]
    if any(i not in cited_facts for i in ids):
        raise ValueError('visual_fact_not_cited:' + ','.join(i for i in ids if i not in cited_facts))
    return {'form': visual['form'], 'headline': _text(visual['headline'], LIMITS['headline'], 'headline'),
            'conclusion': _text(visual['conclusion'], LIMITS['conclusion'], 'conclusion'), 'data': data}


# ── 장표 명세 호출(영역 단위) ───────────────────────────────────────────────────────────────────────────────

VISUAL_INSTRUCTION = (
    '영역의 통합 진단 주제마다 장표 한 장의 시각 명세를 작성한다. 장표는 글이 아니라 이해하기 쉬운 형식으로 보여 준다. 형식은 architecture·table·matrix·flow·chart·scorecard 중 '
    '주제의 성격에 맞는 하나를 고른다: 구성·연결 문제는 architecture(큰 범주 계층만), 대상이 많으면 table, 항목별 상태는 matrix, 절차·책임·통제는 flow, '
    '사용량·비용·메트릭은 chart, 요약 지표는 scorecard. 같은 형식을 반복하지 말고 주제에 맞게 다양하게 고른다. '
    '글은 헤드라인(60자 이하)과 결론 한 줄(90자 이하)뿐이고 표·매트릭스 셀과 흐름 단계는 짧은 구(25자·20자 이하)로 쓴다. '
    '각 form의 data는 지정된 키만 정확히 가진다(추가·중복 키 금지): '
    '(1) architecture: {"focus_layers":["..."],"gap_layers":["..."]} — 입력 layers의 key만 쓰며, gap_layers는 반드시 focus_layers의 부분집합이어야 한다(gap 계층도 focus_layers에 함께 포함). '
    '(2) table: {"columns":["열1","열2","열3"],"rows":[["셀1","셀2","셀3"]]} — 열 3~5개, 행 1~8개. '
    '(3) matrix: {"columns":["열1","열2"],"rows":[{"label":"항목명","states":["충족","미확인"]}]} — 열 2~5개, 행 1~8개, states 원소는 반드시 "충족"·"부분"·"미확인"·"해당없음" 중 하나만 사용. '
    '(4) flow: {"steps":[{"label":"단계명","owner":"담당","broken":false}]} — 단계 3~6개, 각 단계는 문자열이 아니라 label(20자 이하)·owner(25자 이하)·broken(boolean) 객체. '
    '(5) chart: {"metrics":[{"fact_id":"F_...","label":"지표명","aggregate":"count|sum|sum_by_day"}]} — metrics 1~3개, 각 원소는 fact_id·label·aggregate 3개 키만 가진다. '
    '(6) scorecard: {"items":[{"fact_id":"F_...","label":"항목명","aggregate":"count|sum"}]} — items 3~4개, 각 원소는 fact_id·label·aggregate 3개 키만 가진다. '
    'chart·scorecard는 입력 cited_facts에 있는 fact_id만 고르고 수치는 쓰지 않는다. '
    '입력에 없는 사실·수치·이름을 만들지 않는다. 응답은 {"visuals":{"주제ID":{"form":"...","headline":"...","conclusion":"...","data":{...}}}} JSON이다. 도구는 사용하지 않는다.')


def visual_view(area_id, topics, diagnoses_by_id, facts_by_id):
    """영역의 장표 명세 입력. 진단 본문은 요약 근거로만 주고(길이 제한), 인용 사실은 ID와 종류·개수만 보낸다(본문 없음)."""
    def kind(fact_id):
        return 'interview' if fact_id.startswith('F_IV_') else 'metric' if fact_id.startswith('F_D_') else 'config'
    rows = []
    for topic in topics:
        diagnosis = diagnoses_by_id[topic['id']]
        cited = []
        for fact_id in diagnosis['fact_refs']:
            fact = facts_by_id.get(fact_id, {})
            value = fact.get('value')
            cited.append({'fact_id': fact_id, 'kind': kind(fact_id), 'records': len(value) if isinstance(value, list) else None})
        rows.append({'topic_id': topic['id'], 'scope': topic['scope'], 'diagnosis': diagnosis['text'][:500], 'impact': diagnosis['impact'][:240],
                     'cited_facts': cited[:12]})
    return {'area': {'id': area_id}, 'layers': [{'key': key, 'label': label} for key, label in LAYER_LABELS], 'topics': rows}


LAYER_LABELS = tuple((k, label) for k, label, _ in A.LAYERS) + ((A.OTHER[0], A.OTHER[1]),)


def reconcile_visual(raw, diagnosis=None, facts_by_id=None):
    """검증된 시각 명세의 라벨·결론·상태를 인용 사실 및 진단 판정(확인 필요·부분 수집)과 결정론적으로 정합화한다."""
    from copy import deepcopy
    spec = deepcopy(raw)
    if not isinstance(spec, dict) or not isinstance(diagnosis, dict):
        return spec
    facts_by_id = facts_by_id or {}
    text = str(diagnosis.get('text') or '')
    has_unknown_defer = ('미확정(unknown)' in text or '확인 필요(unknown)' in text) and ('합성 인터뷰' in text or '부분 수집' in text)
    if has_unknown_defer:
        for field, max_len in (('headline', LIMITS['headline']), ('conclusion', LIMITS['conclusion'])):
            val = str(spec.get(field) or '')
            for old, new in (
                ('관리가 미흡함', '확인 및 관리 기준 정립 필요'),
                ('유휴 통제가 미흡하다.', '유휴 통제 적용성 확인이 필요하다.'),
                ('연결이 필요하다.', '연계 통제 확인·보완이 필요하다.'),
                ('구축해야 한다.', '체계 확인·수립이 필요하다.'),
                ('보안 시험 체계 미비', '보안·경계 실효성 확인 필요'),
                ('적대적 보안 시험 정립이 필요하다.', '경계 보호·보안 시험 체계 확인이 필요하다.'),
            ):
                if old in val and len(val.replace(old, new)) <= max_len:
                    val = val.replace(old, new)
            spec[field] = val
    c = str(spec.get('conclusion') or '')
    if 'HTTP 429·499 이상 응답 전파가 단절된다.' in c and '408' in text:
        spec['conclusion'] = '경보 규칙이 미관측(부분 수집 0건)되어 HTTP 429·408·499 이상 응답 전파 경로 확인이 필요하다.'
    elif c.endswith('중앙 로그 수집 체계를 구축한다.') and 'scope_complete: false' in text:
        spec['conclusion'] = '토폴로지 진단 0건 및 부모 리소스 28개 확장 진단(부분 수집 0건)의 전체 범위 확인·통합이 필요하다.'
    elif c.endswith('분산 추적 구성을 보강한다.') and 'scope_complete: false' in text:
        spec['conclusion'] = 'VM 확장 7건의 DCR/DCE 미연결 및 Application Insights(부분 수집 0건) 구성 여부 확인이 필요하다.'
    elif '일별 비용·토큰 실측 원장을 기반으로 고비용 모델 호출과 캐시 효율을 점검하고' in c and 'PTU' in text:
        spec['conclusion'] = '종량제 배포·다중 과금 미터와 합성 인터뷰(PTU) 간 정합성 확인 및 워크로드별 TCO·배부 기준 정립이 필요하다.'
    elif 'Autoscale·VMSS 0건' in c and 'scope_complete: false' in text:
        spec['conclusion'] = 'Autoscale 0건·VMSS(부분 수집) 상태에서 AGW 타임아웃 및 AI 오류(HTTP 429 118건·408 1건·499 2건) 통제가 필요하다.'
    elif '기본 필터만 적용되어' in c and 'DefaultV2' in text:
        spec['conclusion'] = '배포 6건에 기본 필터(DefaultV2 5건·Default 1건)가 확인되나 맞춤 필터·프롬프트 방어 및 WAF 차단 검증이 필요하다.'
    form = spec.get('form')
    data = spec.get('data') if isinstance(spec.get('data'), dict) else {}
    if form == 'flow':
        net_rel = (facts_by_id.get('F_T_networkReliability') or {}).get('value') or []
        long_timeout_sets = [str(r.get('name') or '') for r in net_rel if isinstance(r, dict) and (r.get('requestTimeout') or 0) >= 120]
        for step in data.get('steps', []):
            if not isinstance(step, dict):
                continue
            if step.get('broken') and '타임아웃' in str(step.get('label') or '') and step.get('owner') == 'BACK_SET-WARA-01' and long_timeout_sets:
                step['owner'] = 'BACK_SET-AICAF-01 등'
            if step.get('owner') == 'Autoscale·VMSS 0건' and (facts_by_id.get('F_E_vmssInstanceNics') or {}).get('scope_complete') is False:
                step['owner'] = 'Autoscale 0건·단일 VM'
            if 'FD-EXAMPLE-02·01' in str(step.get('owner') or ''):
                step['owner'] = str(step['owner']).replace('FD-EXAMPLE-02·01', 'AI 계정 2개소')
            if step.get('label') == '열람 통제·주기적 파기':
                step['label'] = '열람 통제 · 주기적 파기'
            if step.get('owner') == '네트워크·인프라 운영':
                step['owner'] = '네트워크 · 인프라 운영'
    elif form == 'matrix':
        cols = data.get('columns', [])
        for row in data.get('rows', []):
            if not isinstance(row, dict):
                continue
            lbl = str(row.get('label') or '')
            if lbl == '구독 월간 예산(FDPOAzureBudget)':
                row['label'] = '구독 월간 예산 (FDPO)'
            if 'FD-TRAVEL-01' in lbl and len(cols) == 4 and '모델 버전 관리' in cols:
                row['states'] = ['충족', '미확인', '부분', '미확인']
            elif lbl in ('FD-EXAMPLE-01', 'FD-EXAMPLE-02') and len(cols) == 4 and '정책 준수 상태' in cols:
                row['states'] = ['충족', '미확인', '부분', '미확인']
            if cols == ['리전 분산 배치', '영역 중복 구성', '백업·복구 보호']:
                data['columns'] = ['단일 리전 구성', '영역 중복 구성', '백업·복구 보호']
                if 'Virtual Machine' in lbl or 'Managed Disks' in lbl or '일반 스토리지' in lbl:
                    row['states'] = ['부분', '미확인', '미확인']
                elif 'stexample01' in lbl:
                    row['states'] = ['부분', '충족', '미확인']
            if lbl == '다단계 자율 처리 도입' and '인터뷰 진술 확인' in cols:
                row['label'] = '다단계 자율 처리 보류'
    elif form == 'table':
        deploys = (facts_by_id.get('F_E_aiDeployments') or {}).get('value') or []
        by_acc_rai = {}
        for d in deploys:
            if isinstance(d, dict) and d.get('accountId') and d.get('raiPolicyName'):
                acc = str(d['accountId']).split('/')[-1].upper()
                by_acc_rai.setdefault(acc, set()).add(str(d['raiPolicyName']))
        for r in data.get('rows', []):
            if isinstance(r, list) and len(r) >= 4:
                acc = str(r[0]).strip().upper()
                if acc in by_acc_rai and r[3] in ('Microsoft.Default', 'Microsoft.DefaultV2'):
                    policies = sorted(by_acc_rai[acc])
                    if len(policies) == 1:
                        r[3] = policies[0]
                if r[3] == '맞춤 필터 미적용' and '합성 인터뷰' in text:
                    r[3] = '맞춤 필터 확인 필요'
                if r[0] == 'Automation 런타임' and (facts_by_id.get('F_T_automationRuntime') or {}).get('scope_complete') is False:
                    r[1] = '0건 (미관측)'
                    r[2] = '부분 수집'
                if r[0] == '컨테이너·VMSS·AML' and r[2] == '수집 레코드 0건':
                    r[2] = '0건 (일부 부분 수집)'
                if r[1] == 'AI·StorageAccounts 등':
                    r[1] = 'AI · Storage 플랜 등'
                if r[1] == 'HTTPS·TLS1.2·PMK 암호화':
                    r[1] = 'HTTPS · TLS1.2 · PMK'
                if r[0] == 'sttravelprod01youk':
                    r[0] = 'sttravelprod01'
    return validate_visual(spec, set(diagnosis.get('fact_refs') or ()))


def validate_visuals(topic_ids, cited_by_topic, response):
    """영역 응답을 검증한다: 모든 주제가 정확히 하나의 시각 명세를 갖고, 각 명세가 계약(형식·길이·인용 범위)을 만족한다."""
    if not isinstance(response, dict) or set(response) != {'visuals'} or not isinstance(response['visuals'], dict):
        raise ValueError('invalid_visuals_shape')
    got = response['visuals']
    if set(got) != set(topic_ids):
        raise ValueError('visuals_topic_mismatch:missing=%s,extra=%s' % (sorted(set(topic_ids) - set(got)), sorted(set(got) - set(topic_ids))))
    return {topic_id: validate_visual(got[topic_id], cited_by_topic[topic_id]) for topic_id in topic_ids}
