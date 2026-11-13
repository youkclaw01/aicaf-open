"""코드가 바뀐 뒤 과거 준비 확인만으로 생성이 활성화되지 않는 계약 시험."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_artifacts
import asis_runtime
import asis_view
from test_asis_worker import controller


def test_ready_marker_is_bound_to_exact_current_implementation(tmp_path,monkeypatch):
    control=controller(tmp_path)
    monkeypatch.setattr(asis_runtime,'implementation_fingerprint',lambda:'verified-current-code',raising=False)
    ready={'verified':True,'model_policy':asis_runtime.policy(),'template_sha256':asis_artifacts.TEMPLATE_HASH,
        'implementation_sha256':'old-code'}
    asis_view.atomic(tmp_path/'asis-runtime-ready.json',ready)
    assert not control.runtime_ready(),'a stale implementation verification cannot enable production generation'
    ready['implementation_sha256']='verified-current-code'
    asis_view.atomic(tmp_path/'asis-runtime-ready.json',ready)
    assert control.runtime_ready()
