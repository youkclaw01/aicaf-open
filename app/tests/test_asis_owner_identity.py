"""복구 소유자는 PID와 생성 식별자를 함께 비교한다. 신호와 원장 변경은 없다."""
import ast
import inspect
import pytest
import asis_worker as worker
import asis_view as view


@pytest.mark.parametrize('pid,recorded,current,expected',[
    (None,None,None,True),(0,None,None,True),
    (71,'old',None,True),(71,'old','old',False),
    (71,'old','new',True),(71,None,'new',False),
    (71,'','new',False),(71,'unverifiable','new',False),
    (71,'old','unverifiable',False),(71,False,'new',False),
    ('71','old','new',False),(True,'old','new',False),
])
def test_owner_death_requires_stopped_pid_or_verifiable_birth_mismatch(monkeypatch,pid,recorded,current,expected):
    helper=getattr(worker,'owner_proven_dead',None)
    assert callable(helper),'소유자 생성 식별자 판정 함수가 없다'
    monkeypatch.setattr(worker,'process_identity',lambda _:current)
    row={'pid':pid,'process_identity':recorded,'status':'failed'}
    original=dict(row)
    assert helper(row) is expected
    assert row==original


def test_every_recovery_entry_uses_same_owner_guard():
    names=('retry_existing','retry_after_code_fix','retry_unclaimed_review_setup','resume_failed_scope')
    for name in names:
        source=inspect.getsource(getattr(view.Controller,name))
        import textwrap
        tree=ast.parse(textwrap.dedent(source))
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='owner_proven_dead']
        assert len(calls)==1,name
        assert 'if process_identity(row.get' not in source,name


@pytest.mark.parametrize('mode',['first','code-fix'])
def test_product_retry_accepts_reused_pid_without_touching_new_owner(tmp_path,monkeypatch,mode):
    from test_asis_worker_retry import fixture,_exhausted,AID,REVISION
    control,folder,snapshot,row,kwargs=(fixture if mode=='first' else _exhausted)(tmp_path,monkeypatch)
    before=(folder/'job.json').read_bytes();calls=[]
    def current(pid):calls.append(pid);return 'new-unrelated-owner'
    monkeypatch.setattr(worker,'process_identity',current)
    operation=control.retry_existing if mode=='first' else control.retry_after_code_fix
    result=operation(AID,REVISION,**kwargs)
    assert result['status']=='queued' and result['pid'] is None
    assert result['revision_id']==REVISION and (folder/'snapshot.json').read_bytes()==snapshot
    assert (folder/'retry-history'/kwargs['retry_id']/'job-before.json').read_bytes()==before
    assert calls and all(pid==row['pid'] for pid in calls)
