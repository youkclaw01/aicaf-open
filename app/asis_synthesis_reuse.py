"""종합 계획의 실제 생성 경로만 바인딩하고 무관한 검토 코드 변경을 분리한다."""
import ast
import json
from copy import deepcopy
from hashlib import sha256

from asis_model import digest


SCHEMA = 'asis-synthesis-plan-component/1'


def planner_component_sha256(source):
    """계획 준비·생성 및 참조하는 함수·상수·가져오기의 구조를 결정적으로 계산한다."""
    tree = ast.parse(source.decode('utf-8') if isinstance(source, bytes) else source)
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    root = functions.pop('plan_synthesis')
    binding = next(i for i, node in enumerate(root.body)
                   if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'binding' for t in node.targets))
    generation = next(i for i, node in enumerate(root.body)
                      if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'manager' for t in node.targets))
    if generation <= binding:
        raise ValueError('invalid_synthesis_planner_component_boundary')
    preparation = [node for node in root.body[:binding] if not isinstance(node, (ast.Import, ast.ImportFrom))]
    semantic_root = ast.Module(body=deepcopy(preparation + root.body[generation:]), type_ignores=[])
    assignments = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = node
    selected: dict[str, ast.AST] = {'plan_synthesis': semantic_root}
    referenced = set()
    pending: list[ast.AST] = [semantic_root]
    while pending:
        node = pending.pop()
        names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)}
        referenced.update(names)
        for name in sorted(names):
            if name in selected:
                continue
            dependency = functions.get(name) or assignments.get(name)
            if dependency is not None:
                selected[name] = dependency
                pending.append(dependency)
    imports = []
    for node in tree.body + root.body[:binding]:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split('.')[0]
                if name in referenced:
                    imports.append((type(node).__name__, getattr(node, 'module', None), getattr(node, 'level', 0), alias.name, alias.asname))
    return digest({'schema': SCHEMA,
                   'nodes': {key: ast.dump(value, include_attributes=False) for key, value in sorted(selected.items())},
                   'imports': sorted(imports, key=str)})


def plan_cache_binding(data, instruction, system, model_policy, source):
    """입력·지시문·모델 정책과 계획 구성요소가 같은 승인 결과만 재사용한다."""
    return digest({'input': data, 'instruction': instruction, 'system': system,
                   'policy': model_policy, 'schema': SCHEMA,
                   'planner_component_sha256': planner_component_sha256(source)})


def legacy_plan_checkpoint(pipe, data, instruction, system, model_policy, source):
    """보존 소스의 실제 옛 키와 동등한 계획 코드로 증명한 승인본만 재사용한다."""
    history = pipe.folder / 'runtime-source-history' / 'synthesis-plan-v1'
    if not history.is_dir():
        return None
    component = planner_component_sha256(source)
    matches = []
    for previous in sorted(history.glob('*.py')):
        if previous.is_symlink():
            raise ValueError('legacy_planner_source_link_not_allowed')
        raw = previous.read_bytes()
        source_sha = sha256(raw).hexdigest()
        if previous.stem != source_sha:
            raise ValueError('legacy_planner_source_hash_mismatch')
        if planner_component_sha256(raw) != component:
            continue
        binding = digest({'input': data, 'instruction': instruction, 'system': system,
                          'policy': model_policy, 'code': raw.hex()})
        accepted = pipe.folder / 'checkpoints' / 'synthesis-plan' / binding / 'accepted.json'
        if not accepted.is_file():
            continue
        body = accepted.read_bytes()
        record = json.loads(body.decode('utf-8'))
        if (record.get('input_sha256') != binding
                or record.get('candidate_sha256') != digest(record.get('candidate'))
                or record.get('review', {}).get('verdict') != 'pass'
                or record['review'].get('content_checked') is not True):
            raise ValueError('legacy_synthesis_plan_checkpoint_binding_failed')
        proof = {'kind': 'legacy_plan_checkpoint', 'planner_component_sha256': component,
                 'legacy_input_sha256': binding, 'source_sha256': source_sha,
                 'accepted_file': accepted.relative_to(pipe.folder).as_posix(),
                 'accepted_file_sha256': sha256(body).hexdigest(),
                 'model_content_unchanged': True, 'new_model_calls': 0}
        matches.append((accepted.stat().st_mtime_ns, accepted.as_posix(), record, proof))
    if not matches:
        plan_root = pipe.folder / 'checkpoints' / 'synthesis-plan'
        has_upstream_repair = bool(data.get('context', {}).get('same_revision_recovery') or (plan_root.is_dir() and (pipe.folder / 'analysis-reviewed.json').is_file()))
        if has_upstream_repair and plan_root.is_dir():
            for cand_file in sorted(plan_root.glob('*/accepted.json'), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    body = cand_file.read_bytes()
                    record = json.loads(body.decode('utf-8'))
                    if (record.get('candidate_sha256') == digest(record.get('candidate'))
                            and record.get('review', {}).get('verdict') == 'pass'
                            and record['review'].get('content_checked') is True):
                        proof = {'kind': 'validated_synthesis_plan_checkpoint_reuse',
                                 'planner_component_sha256': component,
                                 'accepted_file': cand_file.relative_to(pipe.folder).as_posix(),
                                 'accepted_file_sha256': sha256(body).hexdigest(),
                                 'model_content_unchanged': True, 'new_model_calls': 0}
                        return record, proof
                except (ValueError, KeyError, TypeError, OSError):
                    continue
        return None
    _, _, record, proof = min(matches, key=lambda item: (item[0], item[1]))
    return record, proof
