"""운영 코드 지문(implementation_fingerprint)이 워커가 실제로 import하는 저장소 모듈을 모두 포함하는지 확인한다.

지문에서 빠진 모듈은 배포 후 바뀌어도 asis-runtime-ready 검증이 알아채지 못한다.
"""
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]
STORE_MODULES = ('asis_store_link.py', 'asis_evidence_store.py', 'asis_analysis_store.py')


def fingerprint_names():
    text = (ROOT / 'asis_runtime.py').read_text(encoding='utf-8')
    block = text[text.index('names=('):text.index('paths=[root/name')]
    return set(re.findall(r"'([\w/.]+\.(?:py|js))'", block))


def test_every_store_module_the_worker_imports_is_in_the_fingerprint():
    names = fingerprint_names()
    for module in STORE_MODULES:
        assert module in names, module + ' is imported by the worker but not sealed by implementation_fingerprint'


def test_fingerprint_changes_when_a_store_module_changes(tmp_path, monkeypatch):
    import asis_runtime
    before = asis_runtime.implementation_fingerprint()
    target = ROOT / 'asis_analysis_store.py'
    original = target.read_bytes()
    try:
        target.write_bytes(original + b'\n# fingerprint probe\n')
        after = asis_runtime.implementation_fingerprint()
    finally:
        target.write_bytes(original)
    assert before and after and before != after


def test_modules_imported_by_the_view_step_are_all_present_on_disk():
    tree = ast.parse((ROOT / 'asis_view.py').read_text(encoding='utf-8'))
    imported = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import) and n.names[0].name.startswith('asis_')}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith('asis_')}
    assert 'asis_store_link' in imported
    for module in imported:
        assert (ROOT / (module + '.py')).is_file(), module


def imported_asis_modules(path):
    """함수 안의 지연 import까지 포함해 한 파일이 import하는 asis_* 모듈 이름을 모은다."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {n.name for n in node.names if n.name.startswith('asis_')}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('asis_'):
            found.add(node.module)
    return found


# 이번 변경 이전부터 지문 밖이던 모듈. 지문에 넣는 것이 설계 의도인지 확인되지 않아 목록으로 고정하고(보류), 새 누락은 허용하지 않는다.
# 판단이 끝나 지문에 넣으면 이 목록에서 지운다.
KNOWN_UNSEALED = {'asis_arbiter', 'asis_chat', 'asis_consolidate', 'asis_incidents'}


def unsealed_imports():
    sealed = {name[:-3] for name in fingerprint_names() if name.endswith('.py')}
    missing = {}
    for name in sorted(n for n in fingerprint_names() if n.endswith('.py')):
        path = ROOT / name
        if not path.exists():
            continue
        for module in imported_asis_modules(path):
            if module not in sealed and (ROOT / (module + '.py')).exists():
                missing.setdefault(module, []).append(name)
    return missing


def test_a_new_module_imported_by_a_sealed_module_must_be_sealed():
    """지문에 든 모듈이 (지연 import 포함) 끌어다 쓰는 새 asis_* 모듈이 지문에서 빠지면 배포 후 변경을 검증이 알아채지 못한다.
    사전 점검 모듈(asis_input_budget)이 asis_item_agents 안의 함수 내부 import로만 쓰이다 지문에서 빠진 사례를 고정한다."""
    new = {m: v for m, v in unsealed_imports().items() if m not in KNOWN_UNSEALED}
    assert not new, 'imported by sealed modules but not sealed: %s' % new


def test_the_known_unsealed_list_does_not_hide_a_module_that_is_now_sealed():
    """지문에 들어간 모듈이 허용 목록에 남아 있으면 목록이 낡은 것이다."""
    sealed = {name[:-3] for name in fingerprint_names() if name.endswith('.py')}
    assert not (KNOWN_UNSEALED & sealed), 'sealed now, remove from KNOWN_UNSEALED: %s' % sorted(KNOWN_UNSEALED & sealed)
