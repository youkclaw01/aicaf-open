"""제품 소스만 선별하고 자격 증명·운영 데이터의 Git 추가를 차단한다."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_PREFIXES = ('asis_', 'arch_', 'assessment_', 'cost_', 'interview_')
ENTRY_POINTS = {'server', 'questions', 'scoring', 'kv_launch', 'collector_upload'}
DENIED_PARTS = {'data', 'evidence', 'recordings', 'revisions', 'checkpoints', '__pycache__', '.git', '.env', '.venv'}
SECRET_PATTERNS = {
    'private_key': re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----'),
    'github_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'azure_connection_key': re.compile(r'AccountKey=[A-Za-z0-9+/]{50,}={0,2}'),
    'credential_url': re.compile(r'https?://[^\s/@:]+:[^\s/@]+@'),
}


def portable_fixture_sources(app: Path) -> list[Path]:
    """분류·상대 경로·해시가 명시된 합성/공개 계약 자료만 선별한다."""
    folder=app/'tests/fixtures';manifest=folder/'manifest.json'
    if not manifest.is_file():return []
    if manifest.is_symlink() or folder.is_symlink():raise ValueError('fixture_manifest_symlink')
    value=json.loads(manifest.read_text(encoding='utf-8'))
    if value.get('schema')!='portable-test-fixtures/1' or not isinstance(value.get('fixtures'),list) or not value['fixtures']:
        raise ValueError('fixture_manifest_invalid')
    output=[manifest];seen=set()
    allowed={'synthetic_test_only','approved_questionnaire_template','product_code_baseline','product_contract_baseline'}
    for row in value['fixtures']:
        if not isinstance(row,dict) or row.get('classification') not in allowed:raise ValueError('fixture_classification_invalid')
        name=row.get('path');expected=row.get('sha256')
        if not isinstance(name,str) or not name or chr(92) in name or ':' in name:raise ValueError('fixture_path_invalid')
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts or relative.suffix not in ('.json','.py','.xlsx') or name in seen:
            raise ValueError('fixture_path_invalid')
        path=folder/relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError('fixture_file_invalid')
        if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('fixture_hash_mismatch')
        output.append(path);seen.add(name)
    return output


def sources(root: Path = ROOT) -> list[Path]:
    """제품 진입점과 실제 import 의존성에서 소스 목록을 산출한다."""
    app = root / 'app'
    modules = {p.stem: p for p in app.glob('*.py')}
    selected = {n for n in modules if n.startswith(PRODUCT_PREFIXES) or n in ENTRY_POINTS}
    pending = list(selected)
    while pending:
        current = pending.pop()
        tree = ast.parse(modules[current].read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for name in names:
                first = name.split('.')[0]
                if first in modules and first not in selected:
                    selected.add(first)
                    pending.append(first)
    result = {modules[n] for n in selected}
    result.update(portable_fixture_sources(app))
    result.update(p for p in (app / 'tests').glob('*') if p.is_file() and p.suffix in ('.py', '.js'))
    result.update(p for p in (app / 'static').glob('*') if p.is_file() and p.suffix in ('.js', '.css', '.html'))
    result.update(p for p in (app / 'asis_assets').rglob('*') if p.is_file() and p.suffix in ('.py', '.json', '.txt', '.md', '.pptx'))
    result.update(p for p in (app / 'templates').glob('*.pptx') if p.is_file())
    result.update(p for name in ('template.pptx', 'interview_template.pptx', 'cost_finops_sources.json') if (p := app / name).is_file())
    result.update(p for p in (root / 'tools').rglob('*.py') if '__pycache__' not in p.parts)
    result.update(p for p in (root / '.github' / 'workflows').glob('*.yml') if p.is_file())
    result.update(p for name in ('README.md', '.gitattributes', 'requirements-dev.txt') if (p := root / name).is_file())
    return sorted(result)


def audit(paths: list[Path], root: Path = ROOT) -> list[dict]:
    """값 자체는 출력하지 않고 검출 위치와 규칙만 기록한다."""
    nested = {parent for path in paths for parent in path.parents
              if root in parent.parents and (parent / '.git').exists()}
    if nested:
        return [{'path': folder.relative_to(root).as_posix(), 'rule': 'nested_repository'}
                for folder in sorted(nested)]
    findings = []
    for path in paths:
        relative = path.relative_to(root)
        if path.is_symlink() or any(part.lower() in DENIED_PARTS or part.lower().startswith('.env') for part in relative.parts):
            findings.append({'path': relative.as_posix(), 'rule': 'forbidden_path'})
            continue
        if path.suffix.lower() in ('.pptx','.xlsx'):
            try:
                with zipfile.ZipFile(path) as archive:
                    texts = [(member.filename, archive.read(member).decode('utf-8', errors='replace'))
                             for member in archive.infolist() if not member.is_dir()]
            except (OSError, zipfile.BadZipFile, RuntimeError):
                findings.append({'path': relative.as_posix(), 'rule': 'unreadable_template_archive'})
                continue
        else:
            texts = [('', path.read_text(encoding='utf-8-sig'))]
        for member, text in texts:
            for rule, pattern in SECRET_PATTERNS.items():
                for match in pattern.finditer(text):
                    row = {'path': relative.as_posix(), 'line': text.count('\n', 0, match.start()) + 1, 'rule': rule}
                    if member:
                        row['archive_member'] = member
                    findings.append(row)
    return findings


def ignore_text(paths: list[Path], root: Path = ROOT) -> str:
    """기본 거부 후 선별된 소스의 정확한 경로만 허용한다."""
    names = sorted({p.relative_to(root).as_posix() for p in paths} | {'.gitignore'})
    parents = sorted({parent.as_posix() for name in names for parent in Path(name).parents
                      if parent != Path('.')}, key=lambda name: (name.count('/'), name))
    return ('# tools/source_policy.py --stage 로 재생성한다.\n*\n'
            + ''.join('!/' + name + '/\n' for name in parents)
            + ''.join('!/' + name + '\n' for name in names))


def tracked_paths(root: Path = ROOT) -> set[str]:
    """명령 성공이 아니라 실제 Git 인덱스의 등록 경로를 확인한다."""
    output = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, check=True).stdout
    return {name for name in output.decode('utf-8').split('\0') if name}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', action='store_true')
    parser.add_argument('--check-tracked', action='store_true')
    args = parser.parse_args()
    paths = sources()
    findings = audit(paths)
    if not (ROOT / 'app' / 'server.py').is_file():
        findings.append({'path': 'app/server.py', 'rule': 'missing_product_tree'})
    allowed = {p.relative_to(ROOT).as_posix() for p in paths} | {'.gitignore'}
    if args.check_tracked:
        tracked = tracked_paths()
        findings.extend({'path': name, 'rule': 'unapproved_tracked_file'} for name in sorted(tracked - allowed))
        findings.extend({'path': name, 'rule': 'missing_tracked_source'} for name in sorted(allowed - tracked))
    if findings:
        print(json.dumps({'status': 'blocked', 'findings': findings}, ensure_ascii=True))
        return 1
    if args.stage:
        (ROOT / '.gitignore').write_text(ignore_text(paths), encoding='utf-8')
        subprocess.run(['git', 'add', '--', '.gitignore', *(p.relative_to(ROOT).as_posix() for p in paths)], cwd=ROOT, check=True)
        missing = allowed - tracked_paths()
        if missing:
            print(json.dumps({'status': 'blocked', 'missing_tracked_source': sorted(missing)}, ensure_ascii=True))
            return 1
    rows = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    print(json.dumps({'status': 'passed', 'source_files': len(rows), 'stage_requested': args.stage, 'manifest_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode('utf-8')).hexdigest()}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
