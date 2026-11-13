"""작성·검토 단계의 재사용 키는 파이프라인 코드 해시에 의존하지 않는다.
운영 실측: 검토 형식 정리 같은 무관한 코드 수정 한 번이 이미 통과한 영역·방향 작업 1,400여 개의 재사용을 깨서 매번 수십 분을 다시 썼다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_pipeline
from asis_pipeline import Pipeline, STAGE_REUSE_CODE_BINDING
from asis_resume import digest
from test_asis_parallel_synthesis import DirectionRuntime, synthesis_fixture


def author_calls(runtime):
    return [c for c in runtime.calls if c['role'] in ('author', 'direction_author') and c['input']['stage_data'].get('direction_assignment')]


def test_a_second_run_reuses_the_direction_work_even_when_the_pipeline_source_differs(tmp_path, monkeypatch):
    data, plan = synthesis_fixture(tmp_path)
    first_runtime = DirectionRuntime(plan)
    asyncio.run(Pipeline(first_runtime, tmp_path / 'run').reviewed_synthesis(deepcopy_data(data)))
    assert len(author_calls(first_runtime)) == 2

    real_read = Path.read_bytes
    pipeline_file = Path(asis_pipeline.__file__).resolve()

    def different_source(self):
        raw = real_read(self)
        return raw + b'\n# a harmless later code change\n' if Path(self).resolve() == pipeline_file else raw
    monkeypatch.setattr(Path, 'read_bytes', different_source)
    second_runtime = DirectionRuntime(plan)
    asyncio.run(Pipeline(second_runtime, tmp_path / 'run').reviewed_synthesis(deepcopy_data(data)))
    assert author_calls(second_runtime) == [], 'unchanged inputs must be reused even though the pipeline source bytes changed'


def test_a_changed_input_is_still_redone(tmp_path):
    data, plan = synthesis_fixture(tmp_path)
    first = DirectionRuntime(plan)
    asyncio.run(Pipeline(first, tmp_path / 'run').reviewed_synthesis(deepcopy_data(data)))
    changed = deepcopy_data(data)
    changed['stage_data']['approved_areas']['areas'][0]['implication']['text'] = '근거가 바뀐 시사점이다.'
    second = DirectionRuntime(plan)
    asyncio.run(Pipeline(second, tmp_path / 'run').reviewed_synthesis(changed))
    assert author_calls(second), 'a changed input must never be served from the old checkpoint'


def test_resume_accepts_the_new_key_and_the_old_code_hash_key_but_nothing_else():
    """보존 검증은 이 기록이 만들어진 시점의 키 식(과거 코드 해시 식 또는 현재 코드 바인딩 식)과 정확히 일치할 때만 받아들인다."""
    import inspect
    import asis_resume
    pipeline_src = inspect.getsource(Pipeline._reviewed_stage).replace(' ', '')
    resume_src = inspect.getsource(asis_resume.ResumeContext.stage).replace(' ', '')
    assert "'code_binding':STAGE_REUSE_CODE_BINDING" in pipeline_src and "'code':Path(__file__)" not in pipeline_src, 'new work is keyed by the binding version'
    assert resume_src.count("'code_binding':STAGE_REUSE_CODE_BINDING") == 2, 'resume must accept the binding formula for both current and legacy records'
    assert "record.get('input_sha256')notincandidates" in resume_src, 'a record is accepted only if its key equals one of the exact candidates'
    assert 'startswith' not in resume_src and ' in record' not in resume_src, 'no prefix or partial matching of keys'
    assert STAGE_REUSE_CODE_BINDING and isinstance(STAGE_REUSE_CODE_BINDING, str)


def deepcopy_data(data):
    from copy import deepcopy
    return deepcopy(data)
