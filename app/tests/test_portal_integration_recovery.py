"""공통 포털의 이식 가능한 질문·가이드·보고서 진입 회귀 계약."""
from pathlib import Path
from copy import deepcopy
import ast
import asyncio
import urllib.parse

from aiohttp import web
import pytest

APP = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('track', ['new', 'existing'])
def test_current_state_derived_questions_preserve_the_source_and_original_bank(track):
    """운영 인수인계 폴더가 아니라 현재 공통 파생 질문 함수를 직접 검증한다."""
    import questions
    import assessment_workflow
    source = {'run': {'assessment_id': 97, 'source_bundle_sha256': 'a' * 64, 'track': track},
              'facts': [], 'context': {}}
    source_before = deepcopy(source)
    original = deepcopy(questions.questions_for(track))
    result = assessment_workflow.derive_questions(source, {'report_id': 'synthetic-state', 'sha256': 'b' * 64})
    assert source == source_before
    assert questions.questions_for(track) == original
    assert result['questions']
    assert {q['origin_id'] for q in result['questions']} <= {q['id'] for q in original}
    assert all(q['guidelines'] and q['derivation_reason'] and q['evaluation_use'] for q in result['questions'])
    assert all(q['id'].startswith('WF_') for q in result['questions'])


def test_guide_downloads_use_the_allowlist_and_actual_file_format(tmp_path):
    """과거 개인 경로·문서 버전·페이지 수를 고정하지 않고 실제 다운로드 함수를 검증한다."""
    source = (APP/'server.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    def literal(name):
        return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
    guides = literal('GUIDES')
    content_types = literal('GUIDE_CT')
    assert guides
    for key, filename in guides.items():
        assert Path(filename).name == filename
        assert Path(filename).suffix == '.' + key.rsplit('-', 1)[1]
        assert Path(filename).suffix in content_types
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'api_guide')
    audited = []
    namespace = {'GUIDES': guides, 'GUIDES_DIR': tmp_path, 'GUIDE_CT': content_types,
                 'web': web, 'urllib': urllib, 'audit': lambda *row: audited.append(row)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(APP/'server.py'), 'exec'), namespace)
    class Request(dict):
        def __init__(self, name):
            super().__init__(user={'upn': 'synthetic-unit-user'})
            self.match_info = {'name': name}
    for key, filename in guides.items():
        payload = ('synthetic-unit-guide:' + key).encode('utf-8')
        (tmp_path/filename).write_bytes(payload)
        response = asyncio.run(namespace['api_guide'](Request(key)))
        assert response.body == payload
        assert response.content_type == content_types[Path(filename).suffix]
        assert urllib.parse.quote(filename) in response.headers['Content-Disposition']
        assert audited[-1][-1] == filename
    before = list(audited)
    with pytest.raises(web.HTTPNotFound):
        asyncio.run(namespace['api_guide'](Request('../outside-secret')))
    assert audited == before


def test_shared_as_is_entry_retires_legacy_result_generation_without_deleting_files():
    source = (APP/'server.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    start = next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='api_infra_start')
    code = ast.get_source_segment(source,start)
    assert 'asis' in code
    assert 'pipeline_launcher.py' not in code
    assert '_archive_previous_run' not in code
    assert 'asis_view.register' in source
    ui=(APP/'static/index.html').read_text(encoding='utf-8')
    assert '/static/asis.js' in ui
    assert '/static/assessment-workflow.js' in ui
    assert 'window.AssessmentFlow.mount' in ui
    assert 'workflowBanner(aid)' in ui
    assert 'AsisUI.mount' not in ui
    assert "f.name==='final.pptx'" not in ui
