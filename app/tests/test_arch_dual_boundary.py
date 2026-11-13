"""합성 입력으로 개정·캐시·실행기 실패·동시 게시의 네 경계를 검증한다."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import struct
import sys
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

APP = Path(__file__).resolve().parents[1]
HELPER = APP / 'arch_dual_artifacts.py'
SOFFICE = '/opt/homebrew/bin/soffice'
sys.dont_write_bytecode = True


def _load_helper():
    """미구현을 명시적 단언으로 표시하고 시험 대상만 읽어 들인다."""
    assert HELPER.is_file(), 'arch_dual_artifacts is not implemented'
    spec = importlib.util.spec_from_file_location('arch_dual_boundary_helper', HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(data):
    """실제 입력·저장 바이트의 계보 해시를 계산한다."""
    return hashlib.sha256(data).hexdigest()


def _png(marker):
    """모델 호출 없이 구별되는 유효 PNG 컨테이너를 메모리에서 만든다."""
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 1, 8, 2, 0, 0, 0))
            + chunk(b'tEXt', b'Fixture\x00' + marker)
            + chunk(b'IDAT', zlib.compress(b'\x00\x14\x87\x7d\xe1\x5a\x41'))
            + chunk(b'IEND', b''))


@pytest.fixture(scope='session')
def payloads():
    """판단·모델·고객 자료 없이 편집 가능한 한 장 합성 PPT를 준비한다."""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width = Inches(13.333333)
    presentation.slide_height = Inches(7.5)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(11), Inches(1))
    box.text = 'SYNTHETIC_BOUNDARY_NATIVE_PPT'
    stream = io.BytesIO()
    presentation.save(stream)
    return _png(b'synthetic-boundary-a'), _png(b'synthetic-boundary-b'), stream.getvalue()


def _save_pair(helper, root, image, pptx, revision):
    """호출자가 명시한 허용 루트로 원본과 같은 개정 PPT를 저장한다."""
    original = helper.save_original(root, image, revision_id=revision,
                                    source_model='synthetic_fixture_no_model_call', allowed_root=root)
    native = helper.save_native_pptx(original, pptx, pptx_revision_id=revision,
                                     source_image_sha256=original['sha256'], allowed_root=root)
    return original, native


def _publish(helper, root, image, pptx, revision, **options):
    """현행 공개 API와 개정·해시 URL 계약으로 합성 입력을 게시한다."""
    return helper.publish_dual_artifacts(
        root, image, pptx, revision_id=revision, pptx_revision_id=revision,
        source_image_sha256=_sha(image), source_model='synthetic_fixture_no_model_call',
        assessment_id=901, state='AS-IS', diagram='D2', allowed_root=root, **options)


def _assert_disabled(metadata, code):
    """실패 표시와 두 원본 다운로드를 실제 메타데이터에서 확인한다."""
    assert metadata['schema'] == 'aicaf.arch.dual-artifacts.v1'
    assert metadata['status'] == metadata['render_status'] == 'failed'
    assert metadata['render_error'] == {'code': code}
    assert metadata['views'][0]['enabled'] is True
    assert metadata['views'][1]['enabled'] is False
    assert metadata['views'][1]['url'] is None
    assert 'ppt_preview' not in metadata['artifacts']
    assert all(item['enabled'] for item in metadata['downloads'])
    for item in metadata['downloads']:
        query = parse_qs(urlparse(item['url']).query)
        artifact = metadata['artifacts'][item['artifact_id']]
        assert query == {'rev': [metadata['revision_id']], 'sha256': [artifact['sha256']], 'dl': ['1']}


def test_actual_unequal_revision_and_source_pairs_are_refused(tmp_path, payloads):
    """실제 저장된 다른 개정·다른 원본을 따뜻한 캐시에 결합하지 않는다."""
    helper = _load_helper()
    image, changed_image, pptx = payloads
    original, native = _save_pair(helper, tmp_path, image, pptx, 'rev-boundary-a')
    same_bytes_other_revision = helper.save_original(
        tmp_path, image, revision_id='rev-boundary-b',
        source_model='synthetic_fixture_no_model_call', allowed_root=tmp_path)
    other_source = helper.save_original(
        tmp_path, changed_image, revision_id='rev-boundary-c',
        source_model='synthetic_fixture_no_model_call', allowed_root=tmp_path)
    assert original['revision_id'] != same_bytes_other_revision['revision_id']
    assert original['sha256'] != other_source['sha256']
    for unequal, revision, digest, code in [
        (same_bytes_other_revision, original['revision_id'], original['sha256'], 'pptx_revision_mismatch'),
        (original, original['revision_id'], other_source['sha256'], 'source_image_sha_mismatch'),
    ]:
        with pytest.raises(ValueError, match=code):
            helper.save_native_pptx(unequal, pptx, pptx_revision_id=revision,
                                    source_image_sha256=digest, allowed_root=tmp_path)
    for unequal in (same_bytes_other_revision, other_source):
        preview = helper.render_preview(original, native, soffice=SOFFICE, allowed_root=tmp_path)
        assert preview['renderer']['method'] == 'pptx_to_pdf_to_png'
        with pytest.raises(helper.RenderError) as caught:
            helper.render_preview(unequal, native, soffice=SOFFICE, allowed_root=tmp_path)
        assert caught.value.code == 'preview_binding_mismatch'
        with pytest.raises(ValueError, match='native_binding_mismatch'):
            helper.create_metadata(unequal, native, None, assessment_id=901,
                                    state='AS-IS', diagram='D2', allowed_root=tmp_path)
        assert not (tmp_path / original['revision_id'] / 'ppt-preview.png').exists()
    assert Path(original['path']).read_bytes() == image
    assert Path(native['path']).read_bytes() == pptx


def test_tampered_ppt_invalidates_actual_render_cache(tmp_path, payloads):
    """실물 렌더 후 PPT 바이트 변조는 기존 PNG·PDF·영수증을 무효화한다."""
    helper = _load_helper()
    import fitz

    image, _, pptx = payloads
    original, native = _save_pair(helper, tmp_path, image, pptx, 'rev-cache-tamper')
    preview = helper.render_preview(original, native, soffice=SOFFICE, allowed_root=tmp_path)
    assert preview['renderer']['name'] == 'LibreOffice'
    assert preview['source_pptx_sha256'] == preview['renderer']['source_snapshot_sha256'] == _sha(pptx)
    assert _sha(Path(preview['pdf_path']).read_bytes()) == preview['source_pdf_sha256']
    with fitz.open(preview['pdf_path']) as document:
        assert len(document) == 1
        assert 'SYNTHETIC_BOUNDARY_NATIVE_PPT' in document[0].get_text()
    cached = helper.render_preview(original, native, soffice=SOFFICE, allowed_root=tmp_path)
    assert cached == preview
    tampered = pptx + b'synthetic-ppt-tamper'
    Path(native['path']).write_bytes(tampered)
    assert _sha(tampered) != native['sha256']
    with pytest.raises(helper.RenderError) as caught:
        helper.render_preview(original, native, soffice=SOFFICE, allowed_root=tmp_path)
    assert caught.value.code == 'source_pptx_sha_mismatch'
    for name in ('ppt-preview.png', 'ppt-render.pdf', 'ppt-preview.binding.json'):
        assert not (Path(native['directory']) / name).exists()
    assert Path(original['path']).read_bytes() == image
    assert Path(native['path']).read_bytes() == tampered


def test_unavailable_and_timeout_disable_preview_preserving_inputs(tmp_path, payloads):
    """실제 누락 실행기·실제 자식 시간초과가 원본·PPT를 보존하고 이전 미리보기를 제거한다."""
    helper = _load_helper()
    image, _, pptx = payloads
    slow = tmp_path / 'slow-renderer'
    slow.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(5)\n', encoding='utf-8')
    slow.chmod(0o700)
    for revision, executable, timeout, code in [
        ('rev-unavailable', str(tmp_path / 'missing-renderer'), 90, 'renderer_unavailable'),
        ('rev-timeout', str(slow), 0.05, 'renderer_timeout'),
    ]:
        ready = _publish(helper, tmp_path, image, pptx, revision, soffice=SOFFICE)
        assert ready['status'] == 'done'
        directory = tmp_path / revision
        assert (directory / 'ppt-preview.png').is_file()
        before = {name: (directory / name).read_bytes() for name in ('image.png', 'diagram.pptx')}
        failed = _publish(helper, tmp_path, image, pptx, revision,
                          soffice=executable, timeout=timeout, force=True)
        _assert_disabled(failed, code)
        assert json.loads((directory / 'artifact-meta.json').read_text(encoding='utf-8')) == failed
        assert {name: (directory / name).read_bytes() for name in before} == before
        for name in ('ppt-preview.png', 'ppt-render.pdf', 'ppt-preview.binding.json'):
            assert not (directory / name).exists()


def test_simultaneous_immutable_publish_refuses_different_original(tmp_path, payloads, monkeypatch):
    """원자 링크의 실제 경쟁에서도 같은 개정의 다른 원본이 기존 바이트를 덮어쓰지 못한다."""
    helper = _load_helper()
    image_a, image_b, pptx = payloads
    barrier = threading.Barrier(2)
    real_link = helper.os.link

    def racing_link(source, destination, *args, **kwargs):
        """두 실제 임시 파일을 최종 원본 공개 직전에서 함께 진행시킨다."""
        if Path(destination).name == 'image.png':
            barrier.wait(timeout=10)
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(helper.os, 'link', racing_link)
    revision = 'rev-simultaneous'
    missing = str(tmp_path / 'missing-renderer')

    def publish(image):
        """모형 성공 응답 없이 실제 게시와 명시적 렌더 실패를 실행한다."""
        try:
            return image, _publish(helper, tmp_path, image, pptx, revision, soffice=missing), None
        except ValueError as error:
            return image, None, str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, (image_a, image_b)))
    winners = [row for row in results if row[2] is None]
    losers = [row for row in results if row[2] is not None]
    assert len(winners) == len(losers) == 1
    winner, metadata, _ = winners[0]
    assert losers[0][2] == 'revision_conflict:image.png'
    directory = tmp_path / revision
    assert (directory / 'image.png').read_bytes() == winner
    assert (directory / 'diagram.pptx').read_bytes() == pptx
    assert metadata['original_image_sha256'] == _sha(winner)
    _assert_disabled(metadata, 'renderer_unavailable')
    assert json.loads((directory / 'artifact-meta.json').read_text(encoding='utf-8')) == metadata
    with pytest.raises(ValueError, match='revision_conflict:image.png'):
        _publish(helper, tmp_path, losers[0][0], pptx, revision, soffice=missing)
    assert (directory / 'image.png').read_bytes() == winner
    assert not list(directory.glob('.*.tmp'))


class _LegacySuiteBinding:
    """기존 시험 원문을 바꾸지 않고 복사본·명시적 허용 루트로 전체를 재실행한다."""

    def __init__(self, legacy):
        self.legacy = legacy.resolve()
        self.renders = []

    @pytest.fixture(autouse=True)
    def _explicit_test_scope(self, request, monkeypatch, tmp_path):
        """실제 실행기를 관찰하고 기존 시험의 생략된 허용 루트만 시험별로 전달한다."""
        import functools
        import subprocess

        real_run = subprocess.run

        def observed_run(command, *args, **kwargs):
            """실제 변환과 합성 실패 실행기를 구분하며 성공 응답을 만들지 않는다."""
            row = {'test': request.node.nodeid, 'executable': Path(command[0]).name,
                   'kind': 'actual_LibreOffice' if command[0] == SOFFICE else 'synthetic_failure',
                   'phase': 'conversion' if '--convert-to' in command else 'version'}
            self.renders.append(row)
            try:
                result = real_run(command, *args, **kwargs)
            except (subprocess.TimeoutExpired, FileNotFoundError) as error:
                row['error_type'] = type(error).__name__
                raise
            row['exit_code'] = result.returncode
            if row['phase'] == 'conversion':
                row['source_pptx_sha256'] = _sha(Path(command[-1]).read_bytes())
                pdf = Path(command[command.index('--outdir') + 1]) / 'diagram.pdf'
                if pdf.is_file():
                    row['pdf_sha256'] = _sha(pdf.read_bytes())
            return result

        monkeypatch.setattr(subprocess, 'run', observed_run)
        if Path(request.module.__file__).resolve() != self.legacy:
            return
        monkeypatch.setattr(request.module, 'HELPER', HELPER)
        original_loader = request.module.load_helper

        def scoped_loader():
            """공개 함수는 원문을 재사용하고 시험 호출에 허용 루트를 명시한다."""
            module = original_loader()
            for name in ('save_original', 'save_native_pptx', 'render_preview',
                         'create_metadata', 'publish_dual_artifacts'):
                original_function = getattr(module, name)

                def bind(function):
                    """각 함수의 서명·문서를 유지하면서 허용 루트를 전달한다."""
                    @functools.wraps(function)
                    def invoke(*args, **kwargs):
                        kwargs.setdefault('allowed_root', tmp_path)
                        return function(*args, **kwargs)
                    return invoke

                setattr(module, name, bind(original_function))
            return module

        monkeypatch.setattr(request.module, 'load_helper', scoped_loader)


def _main():
    """설치·원문 수정 없이 기존 전체 시험과 신규 네 시험의 실제 결과를 출력한다."""
    import argparse
    import os
    import tempfile

    parser = argparse.ArgumentParser()
    parser.add_argument('--legacy-tests', type=Path, required=True)
    args = parser.parse_args()
    legacy = args.legacy_tests.resolve()
    source = legacy.with_name('dual_artifacts.py')
    before = {str(path): _sha(path.read_bytes()) for path in (source, legacy)}
    plugin = _LegacySuiteBinding(legacy)

    def no_network(event, values):
        """시험 중 네트워크·모델 전송이 시작되면 즉시 거부한다."""
        if event in ('socket.connect', 'socket.getaddrinfo'):
            raise AssertionError('network_not_allowed')

    sys.addaudithook(no_network)
    with tempfile.TemporaryDirectory(prefix='a4-dual-boundary-') as directory:
        os.environ.update(PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                          TMPDIR=directory, TEMP=directory, TMP=directory)
        tempfile.tempdir = directory
        code = pytest.main([str(legacy), str(Path(__file__).resolve()), '-q', '--tb=short',
                            '-p', 'no:cacheprovider', '--basetemp', str(Path(directory) / 'pytest')],
                           plugins=[plugin])
        after = {str(path): _sha(path.read_bytes()) for path in (source, legacy)}
        assert after == before, 'staged_sources_changed'
        evidence = {'exit_code': int(code), 'staged_sources_unchanged': after == before,
                    'sha256': {**after, str(HELPER): _sha(HELPER.read_bytes()),
                               str(Path(__file__).resolve()): _sha(Path(__file__).read_bytes())},
                    'actual_LibreOffice_conversion_tests': sorted({row['test'] for row in plugin.renders
                                                                  if row['kind'] == 'actual_LibreOffice'
                                                                  and row['phase'] == 'conversion'}),
                    'synthetic_failure_renderer_tests': sorted({row['test'] for row in plugin.renders
                                                               if row['kind'] == 'synthetic_failure'}),
                    'render_execution_evidence': plugin.renders}
        print(json.dumps(evidence, sort_keys=True, indent=2))
        return int(code)


if __name__ == '__main__':
    raise SystemExit(_main())
