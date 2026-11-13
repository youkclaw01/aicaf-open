"""진입 제어가 누적 메모리를 작업 1개의 비용으로 오판하지 않는 계약 시험.

2026-10-17 운영 실측(프로젝트 25 개정):
- 영역 작성 단계는 동시 21개를 허용했고 작업당 추정은 1.01GB 였다.
- 그 사이 워커 프로세스 트리 메모리가 0.3GB 에서 9.1GB 로 올랐다(작업이 끝나도 해제되지 않은 세션 객체).
- 모든 작업이 끝나 active=0 이 된 뒤 새 작업이 들어오는 순간 추정이 (9.14-0.31)/1 = 8.83GB 로 뛰었고 cost = max(이전, 측정) 때문에 내려오지 못했다.
- 서버에 18GB 가 비어 있었는데도 종합 계획 단계는 평균 동시 1.7, 방향 작성은 87개 중 2개만 돌았다(거절 1,200여 건 전부 commit_reserve).

계약:
1. 기준선(baseline)은 active=0 이고 대기 예약이 없는 순간마다 현재 RSS 로 다시 잡는다(누적분은 작업 비용이 아니다).
2. 작업당 비용은 실제 작업이 만든 증가분(동시 작업 수로 나눈 값)의 최근 관측으로 계산하고, 과거 최대값으로 영구 고정하지 않는다.
3. 한 번도 작업이 돌지 않은 상태의 초기 비용(시작 시 트리 RSS)은 보수적 시드로만 쓰고 실측이 생기면 대체한다.
4. 압박(메모리·커밋 부족)에서는 계속 새 진입을 보류한다(안전 장치는 약해지지 않는다).
"""
from pathlib import Path
import asyncio
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from asis_admission import AdaptiveAdmission

GB = 1 << 30


class Host:
    """통제된 호스트 표본: 워커 트리 RSS 와 가용 메모리를 시험이 조작한다."""

    def __init__(self, *, total=32 * GB, available=28 * GB, rss=int(.3 * GB), commit_limit=36 * GB, commit_available=32 * GB, cpu=5.0):
        self.total, self.available, self.rss = total, available, rss
        self.commit_limit, self.commit_available, self.cpu = commit_limit, commit_available, cpu

    def sample(self):
        return {'sample_valid': True, 'total_memory_bytes': self.total, 'available_memory_bytes': self.available,
                'cpu_percent': self.cpu, 'worker_tree_rss_bytes': self.rss,
                'commit_limit_bytes': self.commit_limit, 'commit_available_bytes': self.commit_available}


def gate(host, **kw):
    return AdaptiveAdmission(sampler=host.sample, interval=0.0, **kw)


def decide(g):
    return g._decision()


def test_accumulated_memory_is_not_charged_as_the_cost_of_one_new_task():
    """운영 점프 재현: 기준선 0.31GB, 작업이 모두 끝난 뒤 RSS 9.14GB, active 0 -> 1 일 때 비용이 8.8GB 로 뛰면 안 된다."""
    host = Host()
    g = gate(host)
    decide(g)  # 시작: baseline = 0.3GB
    # 동시 작업이 돌며 메모리가 올랐고 모두 끝났다(누적 9GB, 해제 안 됨)
    host.rss = int(9.14 * GB)
    g.active = 0
    allowed, reason, detail = decide(g)
    assert allowed, reason
    g.active = 1
    allowed, reason, detail = decide(g)
    assert detail['estimated_incremental_bytes'] < 2 * GB, '누적 메모리 8.8GB 가 작업 1개의 비용으로 계산됐다: %.2fGB' % (detail['estimated_incremental_bytes'] / GB)


def test_admission_capacity_after_a_stage_boundary_is_not_collapsed_to_one_or_two():
    """운영 순서 그대로 재현: 영역 단계 동시 21개 -> 메모리 9GB 누적 -> 모두 종료 -> 방향 단계 진입.
    옛 구현은 방향 단계에서 1~2개만 허용했다(서버 가용 18GB, 거절 사유 전부 commit_reserve)."""
    host = Host(available=28 * GB, commit_available=32 * GB, rss=int(.31 * GB))
    g = gate(host)
    decide(g)
    admitted_area = 0
    for _ in range(21):
        allowed, _, detail = decide(g)
        if allowed:
            g.active += 1
            g.reservations[g.active] = detail['estimated_incremental_bytes']
            admitted_area += 1
    assert admitted_area == 21
    # 작업이 도는 동안 메모리가 9GB 까지 쌓였고(해제되지 않음) 서버 가용은 19GB 로 줄었다
    host.rss, host.available, host.commit_available = int(9.14 * GB), 19 * GB, int(17.9 * GB)
    decide(g)
    g.active = 0
    g.reservations.clear()
    admitted = 0
    reason = None
    for _ in range(40):
        allowed, reason, detail = decide(g)
        if not allowed:
            break
        g.active += 1
        g.reservations[g.active] = detail['estimated_incremental_bytes']
        admitted += 1
    assert admitted >= 8, '서버 가용 19GB 인데 방향 단계에서 %d개만 허용됐다(사유 %s)' % (admitted, reason)


def test_cost_decreases_when_recent_work_is_cheaper_than_the_earlier_peak():
    host = Host()
    g = gate(host)
    decide(g)
    # 처음엔 작업이 4GB 를 쓴 것으로 보였다가 이후 작업은 1GB 만 쓴다
    g.active = 1
    host.rss = int(4.3 * GB)
    decide(g)
    high = g.cost_bytes
    g.active = 0
    decide(g)  # 작업 종료 -> 기준선 재설정
    g.active = 1
    host.rss = host.rss + 1 * GB
    decide(g)
    assert g.cost_bytes < high, '비용이 과거 최대값에 고정됐다: %.2f -> %.2f GB' % (high / GB, g.cost_bytes / GB)


def test_baseline_is_reset_only_when_idle():
    host = Host()
    g = gate(host)
    decide(g)
    start = g.baseline_rss
    g.active = 3
    host.rss = int(3.3 * GB)
    decide(g)
    assert g.baseline_rss == start, '작업이 도는 중에는 기준선을 바꾸면 안 된다(진행 중 작업의 증가분이 사라진다)'
    g.active = 0
    host.rss = int(3.5 * GB)
    decide(g)
    assert g.baseline_rss == int(3.5 * GB), '작업이 모두 끝나 대기 예약도 없으면 현재 값으로 기준선을 다시 잡는다'


def test_baseline_is_not_reset_while_reservations_are_pending():
    host = Host()
    g = gate(host)
    decide(g)
    start = g.baseline_rss
    g.active = 0
    g.reservations[1] = 2 * GB   # 아직 메모리에 나타나지 않은 예약
    host.rss = int(1.3 * GB)
    decide(g)
    assert g.baseline_rss == start


def test_real_memory_pressure_still_blocks_new_admission():
    """안전 장치 유지: 가용 메모리가 정말 부족하면 비용 모델과 무관하게 보류한다."""
    host = Host(available=int(5 * GB), commit_available=int(4 * GB), rss=int(.5 * GB))
    g = gate(host)
    decide(g)
    g.active = 2
    host.rss = int(3.5 * GB)
    allowed, reason, _ = decide(g)
    assert allowed is False and reason in ('memory_reserve', 'commit_reserve')


def test_initial_seed_is_replaced_by_the_first_measurement():
    host = Host(rss=int(2.0 * GB))   # 시작 때 이미 2GB(시드)
    g = gate(host)
    decide(g)
    assert g.cost_bytes == int(2.0 * GB)
    g.active = 1
    host.rss = int(2.4 * GB)
    decide(g)
    assert g.cost_bytes <= int(2.0 * GB), '실측(0.4GB)이 보수적 시드(2.0GB)보다 작으면 시드를 대체해야 한다'
