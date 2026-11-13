"""시험은 개인 Mac 보관 폴더가 아닌 명시 입력 또는 검증된 저장소 자료로 실행되어야 한다."""
from pathlib import Path
import ast
import hashlib
import json

ROOT=Path(__file__).resolve().parent


def test_test_modules_do_not_embed_retired_personal_archive_paths():
    offenders=[];archive='/'.join(('.hermes','brainstorm'));home_prefix='/'+'/'.join(('Users',''))
    for path in ROOT.glob('*.py'):
        tree=ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node,ast.Constant) and isinstance(node.value,str):
                if node.value.startswith(home_prefix) or archive in node.value:
                    offenders.append((path.name,node.lineno))
    assert not offenders,offenders


def test_portable_fixture_manifest_matches_every_allowed_binary_and_text():
    folder=ROOT/'fixtures';path=folder/'manifest.json'
    assert path.is_file(),'공통 시험 자료의 버전·분류·해시 원장이 없다.'
    manifest=json.loads(path.read_text(encoding='utf-8'));assert manifest['schema']=='portable-test-fixtures/1'
    names=[]
    for item in manifest['fixtures']:
        rel=Path(item['path']);assert not rel.is_absolute() and '..' not in rel.parts
        assert item['classification'] in ('synthetic_test_only','approved_questionnaire_template','product_code_baseline','product_contract_baseline')
        asset=folder/rel;assert asset.is_file()
        assert hashlib.sha256(asset.read_bytes()).hexdigest()==item['sha256'],str(rel)
        names.append(item['path'])
    assert len(names)==len(set(names)) and names
