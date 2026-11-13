"""소스 선별과 비밀값 검사의 경계를 합성 자료로 검증한다."""
import importlib.util
from pathlib import Path
import subprocess
import zipfile
import pytest

SPEC = importlib.util.spec_from_file_location('source_policy', Path(__file__).parents[1] / 'source_policy.py')
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


@pytest.mark.parametrize('extension',['pptx','xlsx'])
def test_private_key_in_document_archive_is_rejected(tmp_path,extension):
    path = tmp_path / ('template.'+extension)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('ppt/notesSlides/notesSlide1.xml', '-----BEGIN ' + 'PRIVATE KEY-----\nSYNTHETIC_ONLY\n')
    findings = policy.audit([path], tmp_path)
    assert findings and findings[0]['rule'] == 'private_key'
    assert all('SYNTHETIC_ONLY' not in str(row) for row in findings)


def test_nested_repository_is_rejected_instead_of_silent_omission(tmp_path):
    app = tmp_path / 'app'
    app.mkdir()
    (app / '.git').mkdir()
    path = app / 'server.py'
    path.write_text('VALUE=1\n', encoding='utf-8')
    assert any(row['rule'] == 'nested_repository' for row in policy.audit([path], tmp_path))


def test_operational_data_is_not_selected(tmp_path):
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'server.py').write_text('import feature\n', encoding='utf-8')
    (app / 'feature.py').write_text('VALUE = 1\n', encoding='utf-8')
    (app / 'start_new_revision.py').write_text('VALUE = 2\n', encoding='utf-8')
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'answers.json').write_text('{"synthetic":true}', encoding='utf-8')
    names = {p.relative_to(tmp_path).as_posix() for p in policy.sources(tmp_path)}
    assert names == {'app/server.py', 'app/feature.py'}


def test_data_path_is_rejected_even_if_explicitly_supplied(tmp_path):
    folder = tmp_path / 'data'
    folder.mkdir()
    path = folder / 'record.py'
    path.write_text('VALUE = 1\n', encoding='utf-8')
    assert policy.audit([path], tmp_path) == [{'path':'data/record.py','rule':'forbidden_path'}]


def test_symlink_is_not_uploaded(tmp_path):
    target = tmp_path / 'actual.py'
    target.write_text('VALUE=1\n', encoding='utf-8')
    alias = tmp_path / 'alias.py'
    alias.symlink_to(target)
    assert policy.audit([alias], tmp_path)[0]['rule'] == 'forbidden_path'


def test_default_deny_ignore_does_not_allow_unlisted_data(tmp_path):
    subprocess.run(['git','init','-q'],cwd=tmp_path,check=True)
    source = tmp_path / 'app' / 'server.py'
    source.parent.mkdir()
    source.write_text('VALUE=1\n',encoding='utf-8')
    secret = tmp_path / 'app' / '.env'
    secret.write_text('SYNTHETIC_ONLY=1\n',encoding='utf-8')
    (tmp_path / '.gitignore').write_text(policy.ignore_text([source], tmp_path),encoding='utf-8')
    result = subprocess.run(['git','check-ignore','app/.env'],cwd=tmp_path,capture_output=True,text=True,encoding='utf-8')
    assert result.returncode == 0
    source_check = subprocess.run(['git','check-ignore','app/server.py'],cwd=tmp_path,capture_output=True,text=True,encoding='utf-8')
    assert source_check.returncode == 1


def test_only_declared_and_hash_verified_portable_fixtures_are_selected(tmp_path):
    import hashlib,json
    folder=tmp_path/'app/tests/fixtures';folder.mkdir(parents=True)
    source=folder/'synthetic.json';source.write_text('{"source":"synthetic_test_only"}',encoding='utf-8')
    (folder/'unlisted-private.json').write_text('{"not_approved":true}',encoding='utf-8')
    manifest={'schema':'portable-test-fixtures/1','fixtures':[{'path':'synthetic.json','classification':'synthetic_test_only',
        'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]}
    (folder/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    names={p.relative_to(tmp_path).as_posix() for p in policy.sources(tmp_path)}
    assert names=={'app/tests/fixtures/manifest.json','app/tests/fixtures/synthetic.json'}


def test_portable_fixture_tampering_or_unapproved_classification_is_rejected(tmp_path):
    import hashlib,json,pytest
    folder=tmp_path/'app/tests/fixtures';folder.mkdir(parents=True)
    source=folder/'synthetic.json';source.write_text('{}',encoding='utf-8')
    record={'path':'synthetic.json','classification':'synthetic_test_only','sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
    target=folder/'manifest.json'
    for changes in ({'sha256':'0'*64},{'classification':'customer_data'},{'path':'../outside.json'}):
        target.write_text(json.dumps({'schema':'portable-test-fixtures/1','fixtures':[{**record,**changes}]}),encoding='utf-8')
        with pytest.raises(ValueError):policy.sources(tmp_path)
