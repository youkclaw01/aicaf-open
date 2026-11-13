"""원본 이미지와 네이티브 PPT 산출물을 같은 개정에 결합하는 격리 helper."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

SANDBOX_ROOT = Path(__file__).resolve().parents[1]
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
PDF_EXPORT = 'pdf:impress_pdf_Export:{"UseTaggedPDF":{"type":"boolean","value":"false"}}'
PPTX_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
NS = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}


def _sha(data: bytes) -> str:
    """전송·저장 대상 바이트의 SHA-256을 계산한다."""
    return hashlib.sha256(data).hexdigest()


def _directory(artifact_root: Path, revision_id: str, allowed_root: Path | None) -> Path:
    """개정·출력 경로가 명시적으로 허용된 루트 안에 있는지 확인한다."""
    if not isinstance(revision_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', revision_id):
        raise ValueError('invalid_revision_id')
    authorized = Path(allowed_root or SANDBOX_ROOT).resolve()
    target = Path(artifact_root).resolve() / revision_id
    if not target.resolve().is_relative_to(authorized):
        raise ValueError('output_outside_allowed_root')
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_immutable(path: Path, data: bytes) -> None:
    """이미 존재하는 개정의 바이트를 덮어쓰지 않고 완성본만 공개한다."""
    if path.is_symlink():
        raise ValueError('symlink_artifact_rejected')
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('revision_conflict:' + path.name)
        return
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError('revision_conflict:' + path.name)
    finally:
        temporary.unlink(missing_ok=True)


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """PNG 컨테이너의 서명·IHDR·종료 청크와 양수 크기를 확인한다."""
    if not isinstance(data, bytes) or len(data) < 45 or data[:8] != PNG_MAGIC or data[12:16] != b'IHDR' or b'IEND' not in data:
        raise ValueError('invalid_original_png')
    size = struct.unpack('>II', data[16:24])
    if not all(size):
        raise ValueError('invalid_original_png')
    return size


def save_original(artifact_root: Path, image_bytes: bytes, *, revision_id: str, source_model: str,
                  allowed_root: Path | None = None) -> dict:
    """이미지 응답 바이트를 재인코딩 없이 불변 개정에 저장한다."""
    directory = _directory(artifact_root, revision_id, allowed_root)
    path = directory / 'image.png'
    if path.exists() and path.read_bytes() != image_bytes:
        raise ValueError('revision_conflict:image.png')
    width, height = _png_dimensions(image_bytes)
    _write_immutable(path, image_bytes)
    return {'id': 'original_image', 'name': path.name, 'path': str(path), 'directory': str(directory),
            'sha256': _sha(path.read_bytes()), 'bytes': len(image_bytes), 'width': width, 'height': height,
            'content_type': 'image/png', 'revision_id': revision_id, 'source_model': source_model}


def _native_content(data: bytes) -> dict:
    """OOXML 주형식·ZIP 무결성·편집 가능 개체를 읽어 이미지 전용 PPT를 거부한다."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            names = set(package.namelist())
            if package.testzip() or not {'[Content_Types].xml', 'ppt/presentation.xml', '_rels/.rels'} <= names:
                raise ValueError('invalid_pptx_package')
            types = ET.fromstring(package.read('[Content_Types].xml'))
            overrides = {element.get('PartName'): element.get('ContentType') for element in types
                         if element.tag.endswith('Override')}
            if overrides.get('/ppt/presentation.xml') != 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml':
                raise ValueError('invalid_pptx_main_content_type')
            if any(name.lstrip('/') not in names for name in overrides):
                raise ValueError('invalid_pptx_missing_declared_part')
            slides = sorted(name for name in names if re.fullmatch(r'ppt/slides/slide[1-9][0-9]*\.xml', name))
            totals = {'slides': len(slides), 'editable_shapes': 0, 'editable_texts': 0, 'connectors': 0, 'pictures': 0}
            for name in slides:
                slide = ET.fromstring(package.read(name))
                totals['editable_shapes'] += len(slide.findall('.//p:sp', NS))
                totals['editable_texts'] += sum(bool(text.text) for text in slide.findall('.//p:sp//a:t', NS))
                totals['connectors'] += len(slide.findall('.//p:cxnSp', NS))
                totals['pictures'] += len(slide.findall('.//p:pic', NS))
            if not totals['slides'] or not totals['editable_shapes'] or not totals['editable_texts']:
                raise ValueError('not_native_pptx')
            return totals
    except (zipfile.BadZipFile, KeyError, ET.ParseError, TypeError) as error:
        raise ValueError('invalid_pptx_package') from error


def _checked_bytes(record: dict, code: str, allowed_root: Path | None = None) -> bytes:
    """후속 단계가 실제 저장 바이트와 선언 SHA를 재확인한다."""
    path = Path(record['path'])
    authorized = Path(allowed_root or SANDBOX_ROOT).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(authorized) or path.parent.resolve() != Path(record['directory']).resolve():
        raise ValueError('invalid_artifact_path')
    data = path.read_bytes()
    if _sha(data) != record['sha256'] or len(data) != record['bytes']:
        raise ValueError(code)
    return data


def save_native_pptx(original: dict, pptx_bytes: bytes, *, pptx_revision_id: str,
                     source_image_sha256: str, filename: str = 'diagram.pptx', allowed_root: Path | None = None) -> dict:
    """동일 개정·동일 원본을 참조하는 네이티브 PPT 바이트를 보존한다."""
    if pptx_revision_id != original['revision_id']:
        raise ValueError('pptx_revision_mismatch')
    if source_image_sha256 != original['sha256']:
        raise ValueError('source_image_sha_mismatch')
    _checked_bytes(original, 'source_image_sha_mismatch', allowed_root)
    if filename != 'diagram.pptx':
        raise ValueError('pptx_extension_mismatch')
    native_content = _native_content(pptx_bytes)
    path = Path(original['directory']) / filename
    _write_immutable(path, pptx_bytes)
    return {'id': 'native_pptx', 'name': path.name, 'path': str(path), 'directory': original['directory'],
            'sha256': _sha(path.read_bytes()), 'bytes': len(pptx_bytes), 'content_type': PPTX_MIME,
            'revision_id': pptx_revision_id, 'source_image_sha256': source_image_sha256,
            'native_content': native_content}


def _atomic_bytes(path: Path, data: bytes) -> None:
    """완성된 파생 파일과 메타데이터를 같은 디렉터리에서 원자 교체한다."""
    if path.is_symlink():
        raise ValueError('symlink_artifact_rejected')
    if path.is_file() and path.read_bytes() == data:
        return
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class RenderError(RuntimeError):
    """원본·PPT를 보존하면서 렌더 실패 코드를 호출자에게 전달한다."""

    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def _drop_preview(directory: Path, allowed_root: Path | None) -> None:
    """실패한 미리보기의 파생물만 제거하고 원본·PPT는 건드리지 않는다."""
    if directory.resolve().is_relative_to(Path(allowed_root or SANDBOX_ROOT).resolve()):
        for name in ('ppt-preview.binding.json', 'ppt-preview.png', 'ppt-render.pdf'):
            (directory / name).unlink(missing_ok=True)


def _render_policy(soffice: str | None, dpi: int) -> str:
    """렌더 실행기·DPI·PDF 필터·래스터 라이브러리 정책을 해시로 고정한다."""
    import fitz

    executable = soffice or shutil.which('soffice')
    policy = {'executable': os.path.realpath(executable) if executable else None, 'dpi': dpi,
              'pdf_export': PDF_EXPORT, 'rasterizer_version': fitz.VersionBind}
    return _sha(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode())


def _cached_preview(original: dict, native: dict, soffice: str | None, dpi: int, allowed_root: Path | None) -> dict | None:
    """현재 원본/PPT/PNG/PDF 해시와 렌더 정책이 모두 일치할 때만 재사용한다."""
    try:
        directory = Path(native['directory'])
        preview = json.loads((directory / 'ppt-preview.binding.json').read_text(encoding='utf-8'))
        if (preview['revision_id'] != native['revision_id'] or preview['source_image_sha256'] != original['sha256']
                or preview['source_pptx_sha256'] != native['sha256'] or preview['directory'] != str(directory)
                or preview['path'] != str(directory / 'ppt-preview.png')
                or preview['renderer']['policy_sha256'] != _render_policy(soffice, dpi)):
            return None
        _checked_bytes(preview, 'preview_sha_mismatch', allowed_root)
        if _sha((directory / 'ppt-render.pdf').read_bytes()) != preview['source_pdf_sha256']:
            return None
        return preview
    except (OSError, ValueError, KeyError):
        return None


def render_preview(original: dict, native: dict, *, soffice: str | None = None, timeout: float = 90,
                   dpi: int = 144, allowed_root: Path | None = None, force: bool = False) -> dict:
    """렌더 실패를 명시하고 이전 미리보기나 원본 이미지를 대체 결과로 쓰지 않는다."""
    directory = Path(native['directory'])
    try:
        _checked_bytes(original, 'source_image_sha_mismatch', allowed_root)
        _checked_bytes(native, 'source_pptx_sha_mismatch', allowed_root)
        if (original['revision_id'] != native['revision_id']
                or native['source_image_sha256'] != original['sha256']
                or original['directory'] != native['directory']):
            raise ValueError('preview_binding_mismatch')
        if not force:
            cached = _cached_preview(original, native, soffice, dpi, allowed_root)
            if cached is not None:
                return cached
        _drop_preview(directory, allowed_root)
        return _render_preview(original, native, soffice=soffice, timeout=timeout, dpi=dpi, allowed_root=allowed_root)
    except RenderError:
        _drop_preview(directory, allowed_root)
        raise
    except subprocess.TimeoutExpired as error:
        _drop_preview(directory, allowed_root)
        raise RenderError('renderer_timeout', {'timeout_seconds': timeout}) from error
    except FileNotFoundError as error:
        _drop_preview(directory, allowed_root)
        raise RenderError('renderer_unavailable') from error
    except ValueError as error:
        _drop_preview(directory, allowed_root)
        raise RenderError(str(error)) from error
    except Exception as error:
        _drop_preview(directory, allowed_root)
        raise RenderError('renderer_failed', {'error_type': type(error).__name__}) from error


def _render_preview(original: dict, native: dict, *, soffice: str | None, timeout: float,
                    dpi: int, allowed_root: Path | None) -> dict:
    """실제 PPT 바이트를 LibreOffice PDF로 변환한 뒤 그 PDF만 PNG로 렌더한다."""
    import fitz

    _checked_bytes(original, 'source_image_sha_mismatch', allowed_root)
    pptx_bytes = _checked_bytes(native, 'source_pptx_sha_mismatch', allowed_root)
    if original['revision_id'] != native['revision_id'] or native['source_image_sha256'] != original['sha256']:
        raise ValueError('preview_binding_mismatch')
    executable = soffice or shutil.which('soffice')
    if not executable:
        raise RenderError('renderer_unavailable')
    directory = Path(native['directory'])
    with tempfile.TemporaryDirectory(prefix='.render-', dir=directory) as temporary:
        work = Path(temporary)
        home, temp, out = work / 'home', work / 'tmp', work / 'out'
        for folder in (home, temp, out):
            folder.mkdir()
        snapshot = work / 'diagram.pptx'
        snapshot.write_bytes(pptx_bytes)
        profile = '-env:UserInstallation=' + (work / 'profile').as_uri()
        env = dict(os.environ, HOME=str(home), TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp),
                   XDG_CONFIG_HOME=str(home / 'config'), XDG_CACHE_HOME=str(home / 'cache'),
                   PYTHONDONTWRITEBYTECODE='1')
        version = subprocess.run([executable, profile, '--headless', '--version'], cwd=work, env=env,
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=timeout)
        command = [executable, profile, '--headless', '--invisible', '--nologo', '--nodefault',
                   '--nofirststartwizard', '--nolockcheck', '--convert-to',
                   PDF_EXPORT,
                   '--outdir', str(out), str(snapshot)]
        result = subprocess.run(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        pdf_file = out / 'diagram.pdf'
        if result.returncode:
            raise RenderError('renderer_exit_nonzero', {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        if not pdf_file.is_file() or not pdf_file.stat().st_size:
            raise RenderError('renderer_no_pdf', {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        try:
            with fitz.open(pdf_file) as document:
                if len(document) != 1:
                    raise RenderError('renderer_page_count_mismatch', {'pages': len(document)})
                png = document[0].get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False).tobytes('png')
        except fitz.FileDataError as error:
            raise RenderError('renderer_invalid_pdf', {'error_type': type(error).__name__}) from error
        _checked_bytes(original, 'source_image_sha_mismatch', allowed_root)
        _checked_bytes(native, 'source_pptx_sha_mismatch', allowed_root)
        pdf_bytes = pdf_file.read_bytes()
        width, height = _png_dimensions(png)
        png_path, pdf_path, binding_path = directory / 'ppt-preview.png', directory / 'ppt-render.pdf', directory / 'ppt-preview.binding.json'
        preview = {'id': 'ppt_preview', 'name': png_path.name, 'path': str(png_path), 'directory': str(directory),
                   'content_type': 'image/png', 'sha256': _sha(png), 'bytes': len(png), 'width': width, 'height': height,
                   'revision_id': native['revision_id'], 'source_image_sha256': original['sha256'],
                   'source_pptx_sha256': native['sha256'], 'source_pdf_sha256': _sha(pdf_bytes),
                   'pdf_path': str(pdf_path), 'binding_path': str(binding_path), 'slide_number': 1,
                   'renderer': {'name': 'LibreOffice', 'version': version.stdout.strip(), 'method': 'pptx_to_pdf_to_png',
                                'policy_sha256': _render_policy(soffice, dpi), 'rasterizer': 'PyMuPDF', 'rasterizer_version': fitz.VersionBind,
                                'dpi': dpi, 'source_snapshot_sha256': _sha(snapshot.read_bytes()),
                                'conversion_command': command, 'exit_code': result.returncode,
                                'stdout': result.stdout, 'stderr': result.stderr}}
        _atomic_bytes(pdf_path, pdf_bytes)
        _atomic_bytes(png_path, png)
        _atomic_bytes(binding_path, (json.dumps(preview, sort_keys=True, indent=2) + '\n').encode('utf-8'))
        return preview


def _portal_context(assessment_id: int, state: str, diagram: str) -> None:
    """기존 Portal의 작업 식별자 계약을 적용한다."""
    if type(assessment_id) is not int or assessment_id <= 0 or state not in ('AS-IS', 'TO-BE') or diagram not in ('D1', 'D2'):
        raise ValueError('invalid_portal_context')


def create_metadata(original: dict, native: dict, preview: dict | None, *, assessment_id: int,
                    state: str, diagram: str, render_error: RenderError | None = None,
                    allowed_root: Path | None = None) -> dict:
    """호스트 경로를 노출하지 않는 두 보기·두 다운로드 표시 계약을 만든다."""
    _portal_context(assessment_id, state, diagram)
    _checked_bytes(original, 'source_image_sha_mismatch', allowed_root)
    _checked_bytes(native, 'source_pptx_sha_mismatch', allowed_root)
    if native['revision_id'] != original['revision_id'] or native['source_image_sha256'] != original['sha256'] or native['directory'] != original['directory']:
        raise ValueError('native_binding_mismatch')
    if preview is not None:
        if (preview['revision_id'] != native['revision_id'] or preview['source_pptx_sha256'] != native['sha256']
                or preview['source_image_sha256'] != original['sha256'] or preview['directory'] != native['directory']):
            raise ValueError('preview_binding_mismatch')
        _checked_bytes(preview, 'preview_sha_mismatch', allowed_root)
        receipt = json.loads((Path(native['directory']) / 'ppt-preview.binding.json').read_text(encoding='utf-8'))
        if receipt != preview:
            raise ValueError('preview_receipt_mismatch')
        if _sha((Path(native['directory']) / 'ppt-render.pdf').read_bytes()) != preview['source_pdf_sha256']:
            raise ValueError('preview_pdf_sha_mismatch')
    binding = {'assessment_id': assessment_id, 'state': state, 'diagram': diagram, 'revision_id': original['revision_id'],
               'original_image_sha256': original['sha256'], 'native_pptx_sha256': native['sha256']}
    binding_sha256 = _sha(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode())
    public_fields = {'id', 'name', 'content_type', 'sha256', 'bytes', 'width', 'height', 'revision_id', 'source_model',
                     'source_image_sha256', 'source_pptx_sha256', 'source_pdf_sha256', 'native_content', 'slide_number'}
    artifacts = {}
    for record in (original, native, preview):
        if record is not None:
            public = {key: value for key, value in record.items() if key in public_fields}
            public['binding_sha256'] = binding_sha256
            if 'renderer' in record:
                public['renderer'] = {key: record['renderer'][key] for key in ('name', 'version', 'method', 'dpi', 'source_snapshot_sha256', 'policy_sha256', 'rasterizer', 'rasterizer_version')}
            artifacts[record['id']] = public
    base = f'/api/aicaf/assessments/{assessment_id}/arch/{state}/{diagram}/file/'

    def artifact_url(record: dict, download: bool = False) -> str:
        """실제 파일 경로 대신 기존 API 문법과 개정·내용 해시를 사용한다."""
        query = {'rev': original['revision_id'], 'sha256': record['sha256']}
        if download:
            query['dl'] = '1'
        return base + record['name'] + '?' + urlencode(query)

    views = [{'id': 'original_image', 'label': '이미지 원본', 'kind': 'image', 'artifact_id': 'original_image',
              'enabled': True, 'url': artifact_url(original)},
             {'id': 'native_pptx', 'label': 'PPT 변환본', 'kind': 'ppt_preview', 'artifact_id': 'ppt_preview',
              'source_artifact_id': 'native_pptx', 'enabled': preview is not None,
              'url': artifact_url(preview) if preview is not None else None}]
    downloads = [{'id': record['id'], 'artifact_id': record['id'], 'label': label, 'enabled': True,
                  'url': artifact_url(record, True)} for record, label in
                 ((original, '이미지 원본 다운로드'), (native, '편집 가능 PPT 다운로드'))]
    return {'schema': 'aicaf.arch.dual-artifacts.v1', **binding, 'binding': binding, 'binding_sha256': binding_sha256,
            'status': 'done' if preview is not None else 'failed', 'render_status': 'ready' if preview is not None else 'failed',
            'render_error': {'code': render_error.code} if render_error is not None else None,
            'artifacts': artifacts, 'views': views, 'downloads': downloads,
            'files': [{'name': record['name'], 'bytes': record['bytes'], 'sha256': record['sha256'],
                       'content_type': record['content_type'], 'revision_id': record['revision_id']} for record in artifacts.values()]}


def publish_dual_artifacts(artifact_root: Path, image_bytes: bytes, pptx_bytes: bytes, *, revision_id: str,
                           pptx_revision_id: str, source_image_sha256: str, source_model: str,
                           assessment_id: int, state: str, diagram: str, soffice: str | None = None,
                           timeout: float = 90, dpi: int = 144, allowed_root: Path | None = None,
                           force: bool = False) -> dict:
    """선택된 한 개정의 원본/PPT를 보존하고 실제 미리보기와 공개 메타데이터를 만든다."""
    _portal_context(assessment_id, state, diagram)
    original = save_original(artifact_root, image_bytes, revision_id=revision_id, source_model=source_model, allowed_root=allowed_root)
    native = save_native_pptx(original, pptx_bytes, pptx_revision_id=pptx_revision_id,
                              source_image_sha256=source_image_sha256, allowed_root=allowed_root)
    manifest = Path(original['directory']) / 'artifact-meta.json'
    if manifest.exists():
        prior = json.loads(manifest.read_text(encoding='utf-8'))
        if any(prior[key] != value for key, value in (('assessment_id', assessment_id), ('state', state), ('diagram', diagram), ('revision_id', revision_id))):
            raise ValueError('revision_context_conflict')
    preview, render_error = None, None
    try:
        preview = render_preview(original, native, soffice=soffice, timeout=timeout, dpi=dpi, allowed_root=allowed_root, force=force)
    except RenderError as error:
        render_error = error
    metadata = create_metadata(original, native, preview, assessment_id=assessment_id, state=state, diagram=diagram,
                               render_error=render_error, allowed_root=allowed_root)
    _atomic_bytes(manifest, (json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))
    return metadata
