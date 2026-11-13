"""단계별 병렬도 감시(순수 계산, 모델 호출 없음).

영수증 목록과 단계별 독립 단위 수를 받아 단계마다 실제 동시 수·이상 소요·손실 시간을 계산하고, 독립 작업이 충분한데도
동시 수가 낮은 단계에 경고를 낸다. 경고의 원인은 영수증의 대기 기록(last_hold_reason)에서만 정한다(지어내지 않는다).
"""
from datetime import datetime

LOW_RATIO = .25        # (이전 기준, 호환용) 평균 동시 수가 독립 단위 수의 25% 미만
LOW_RATIO_OF_PEAK = .5 # 평균 동시 수가 달성 가능한 동시 수의 50% 미만이면 의심한다
MONITOR_FLOOR = 8      # 경고 판정용 하한(진입 제어의 강제 하한이 아니다): 이 서버가 낸 최대 동시가 이보다 작아도 독립 단위가 충분하면 이 정도는 기대한다
MIN_UNITS = 4          # 독립 단위가 이보다 적으면 직렬이 자연스럽다
MIN_LOSS_SECONDS = 120 # 손실이 이보다 작으면 경고하지 않는다
RESOURCE_REASONS = ('memory_reserve', 'commit_reserve', 'cpu_pressure', 'resource_sample_unavailable')


def _ts(value):
    return datetime.fromisoformat(value).timestamp()


def analyze(calls, stage_info):
    """calls: [{'stage','request_started_at','completed_at', 선택: queue_wait_seconds, last_hold_reason}].
    stage_info: {단계: {'independent_units': 정수}}."""
    by = {}
    for row in calls:
        by.setdefault(row['stage'], []).append(row)
    stages, warnings = {}, []
    for stage, rows in sorted(by.items()):
        spans = [(_ts(r['request_started_at']), _ts(r['completed_at'])) for r in rows]
        start, end = min(s for s, _ in spans), max(e for _, e in spans)
        span = end - start
        events = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
        current = peak = 0
        for _, delta in events:
            current += delta
            peak = max(peak, current)
        lengths = sorted(e - s for s, e in spans)
        busy = sum(lengths)
        units = int((stage_info.get(stage) or {}).get('independent_units') or 0)
        wait_recorded = all('queue_wait_seconds' in r and r['queue_wait_seconds'] is not None for r in rows)
        # 이상 소요: 독립 단위를 모두 동시에 돌릴 수 있을 때 가장 긴 호출 길이. 직렬 의존이 있는 단계는 독립 단위 수가 작게 들어온다.
        ideal = lengths[-1] if units >= len(rows) or units >= MIN_UNITS else span
        row = {'calls': len(rows), 'span_seconds': round(span), 'busy_seconds': round(busy), 'avg_concurrency': round(busy / span, 2) if span else 0.0,
               'peak': peak, 'median_call_seconds': round(lengths[len(lengths) // 2]), 'longest_call_seconds': round(lengths[-1]),
               'independent_units': units, 'ideal_seconds': round(ideal), 'loss_seconds': max(0, round(span - ideal)), 'wait_recorded': wait_recorded}
        holds = {}
        for r in rows:
            reason = r.get('last_hold_reason')
            if reason:
                holds[reason] = holds.get(reason, 0) + 1
        row['hold_reasons'] = holds
        stages[stage] = row
    # 달성 가능한 동시 수: 독립 단위 수와 '이 개정에서 서버가 실제로 낸 최대 동시 수(없으면 감시 하한)' 중 작은 값.
    # 단계 자신의 최대값을 쓰면 병목이 최대 동시 수 자체를 낮춘 직렬 단계(운영 6단계: 최대 3, 평균 1.7)를 정상으로 판정한다.
    demonstrated = max([r['peak'] for r in stages.values()] + [MONITOR_FLOOR])
    for stage, row in stages.items():
        units = row['independent_units']
        reachable = min(units, demonstrated) if units else 0
        if units >= MIN_UNITS and reachable and row['avg_concurrency'] < reachable * LOW_RATIO_OF_PEAK and row['loss_seconds'] >= MIN_LOSS_SECONDS:
            resource = {k: v for k, v in row['hold_reasons'].items() if k in RESOURCE_REASONS}
            if resource:
                reason = max(resource, key=lambda k: resource[k])
                cause = 'resource_admission'
            else:
                reason, cause = None, 'not_resource_admission'
            warnings.append({'stage': stage, 'cause': cause, 'reason': reason, 'independent_units': units, 'avg_concurrency': row['avg_concurrency'],
                             'peak': row['peak'], 'loss_seconds': row['loss_seconds'], 'wait_recorded': row['wait_recorded']})
    return {'stages': stages, 'warnings': warnings}


def stage_of_unit(unit):
    """영수증이 놓인 체크포인트 폴더 이름으로 18단계 번호를 정한다. 알 수 없으면 None(임의 배정하지 않는다)."""
    import asis_stages
    base = unit.rsplit('-review', 1)[0] if unit.endswith('-review') else unit
    return asis_stages.stage_of(base) or asis_stages.stage_of(unit)


def report_for_revision(folder):
    """개정 폴더의 영수증·작업 원장으로 단계별 병렬도 보고서(parallelism-report.json)를 만들어 돌려준다. 모델 호출 없음."""
    import json
    from pathlib import Path
    import asis_stages
    folder = Path(folder)
    calls, skipped, unclassified, seen = [], 0, 0, set()
    earlier = {}
    # 이번 실행의 시작 시각 = 작업 원장 생성 시각(워커가 시작할 때 원장을 새로 만든다). 재개한 개정에는 이전 실행의 영수증이 섞여 있고
    # 이를 같은 구간으로 계산하면 단계 소요가 수 시간으로 늘어 거짓 경고가 난다(운영 2026-10-17: 평균 동시 0.07, 손실 8,046초).
    run_start = None
    ledger_file = folder / 'task-ledger.json'
    if ledger_file.is_file():
        try:
            created = json.loads(ledger_file.read_text(encoding='utf-8')).get('created_at')
            run_start = datetime.fromisoformat(created).timestamp() if created else None
        except (OSError, ValueError, TypeError):
            run_start = None
    checkpoints = folder / 'checkpoints'
    if checkpoints.is_dir():
        for path in checkpoints.rglob('*receipt*.json'):
            try:
                if path.stat().st_size > 4_000_000:
                    skipped += 1
                    continue
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                skipped += 1
                continue
            for row in (data if isinstance(data, list) else [data]):
                if not isinstance(row, dict) or not row.get('request_started_at') or not row.get('completed_at'):
                    continue
                rid = row.get('response_id')
                if rid in seen:
                    continue
                seen.add(rid)
                unit = path.relative_to(checkpoints).parts[0]
                stage = stage_of_unit(unit)
                if stage is None:
                    unclassified += 1
                    continue
                if run_start is not None and _ts(row['request_started_at']) < run_start:
                    earlier[stage] = earlier.get(stage, 0) + 1
                    continue
                calls.append({'stage': stage, 'request_started_at': row['request_started_at'], 'completed_at': row['completed_at'],
                              'queue_wait_seconds': row.get('queue_wait_seconds'), 'last_hold_reason': row.get('admission_last_hold_reason')})
    info = {}
    ledger = folder / 'task-ledger.json'
    if ledger.is_file():
        try:
            tasks = json.loads(ledger.read_text(encoding='utf-8')).get('tasks', {})
        except (OSError, ValueError):
            tasks = {}
        counts = {}
        for key in tasks:
            if '.' in key or key.endswith('-review'):
                continue
            stage = asis_stages.stage_of(key)
            if stage is not None:
                counts[stage] = counts.get(stage, 0) + 1
        info = {stage: {'independent_units': n} for stage, n in counts.items()}
    result = analyze(calls, info) if calls else {'stages': {}, 'warnings': []}
    result['stages'] = {str(k): v for k, v in result['stages'].items()}
    for stage, count in earlier.items():
        result['stages'].setdefault(str(stage), {'calls': 0})['earlier_run_calls'] = count
    result['unclassified_calls'] = unclassified
    result['run_started_at'] = datetime.fromtimestamp(run_start, __import__('datetime').timezone.utc).isoformat() if run_start is not None else None
    result['skipped_receipts'] = skipped
    try:
        (folder / 'parallelism-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        pass
    return result
