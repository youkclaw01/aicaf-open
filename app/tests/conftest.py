"""외부 모델을 대체한 Runtime 단위 시험의 호스트 자원도 명시적으로 통제한다."""
from types import SimpleNamespace
import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_unit_resources(request, monkeypatch):
    """호스트·공급자 실측 시험은 제외하고 가짜 Client를 쓰는 기존 단위 시험만 격리한다."""
    files = {
        'test_asis_runtime.py', 'test_asis_timeout.py',
        'test_asis_persistent_item_sessions.py', 'test_asis_persistent_installed_sdk.py',
        'test_asis_transport_message.py', 'test_asis_continuation_budget.py',
        'test_asis_terminal_recovery.py',
    }
    if request.node.path.name not in files:
        return
    import asis_admission
    sample = {'total_memory_bytes': 1000000000, 'available_memory_bytes': 900000000,
              'cpu_percent': 5, 'logical_cpu_count': 2, 'worker_tree_rss_bytes': 1000000,
              'sample_valid': True}
    monkeypatch.setattr(asis_admission, 'HostSampler', lambda: SimpleNamespace(sample=lambda: dict(sample)))
