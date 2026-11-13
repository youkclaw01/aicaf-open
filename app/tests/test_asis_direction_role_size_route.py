"""방향 작성 입력이 luna 문맥(128K)을 넘길 크기면 sol 작성 역할로 보낸다(운영 D_P0030 실패 재발 방지)."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_pipeline


def test_small_prompt_keeps_luna():
    assert asis_pipeline.role_for_prompt_size('direction_author', 'x' * 1000) == 'direction_author'


def test_large_prompt_routes_to_author():
    assert asis_pipeline.role_for_prompt_size('direction_author', 'x' * 300_000) == 'author'


def test_other_roles_unchanged():
    assert asis_pipeline.role_for_prompt_size('author', 'x' * 900_000) == 'author'
