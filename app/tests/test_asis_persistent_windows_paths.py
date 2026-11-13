"""Windows의 장기 항목 보완 원장이 MAX_PATH를 넘겨도 손실 없이 저장되는지 검증한다."""
from pathlib import Path
import os
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_pipeline import Pipeline


@pytest.mark.skipif(os.name != 'nt', reason='native Windows path boundary')
def test_windows_item_revision_save_supports_long_path(tmp_path):
    """설정 변경 없이 실제 Windows 파일 I/O로 임시 파일과 원장 저장을 확인한다."""
    folder = tmp_path/'item-history'/('a'*80)/('b'*80)/('c'*40)
    target = folder/'input.json'
    pipe = Pipeline(None,tmp_path/'run')
    payload = b'{"classification":"synthetic_windows_path_test"}'
    pipe.save(target,payload)
    resolved = str(target.resolve())
    extended = Path('\\\\?\\'+resolved)
    assert extended.read_bytes() == payload
