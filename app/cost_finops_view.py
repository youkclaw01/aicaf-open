"""비용 v2 전용 프로젝트 저장소·동결 실행. 기존 고객 데이터와 비용 v1 파일은 읽기/보존한다."""
import asyncio
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid
from aiohttp import web

import cost_calc


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode('utf-8')


def atomic(path, value):
    """PID·임의 토큰 임시파일과 재시도로 Windows 열린 파일 충돌을 방지한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+f'.tmp-{os.getpid()}-{uuid.uuid4().hex}')
    temp.write_bytes(value if isinstance(value, bytes) else encoded(value))
    for attempt in range(6):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(.05*(attempt+1))


def read_json(path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding='utf-8-sig'))


def process_alive(pid):
    """Windows에서는 signal 0을 쓰지 않고 조회 전용 핸들로 생존 여부를 확인한다."""
    if not isinstance(pid,int) or pid < 1:
        return False
    if os.name == 'nt':
        import ctypes
        kernel=ctypes.windll.kernel32
        kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return False
        code=ctypes.c_ulong()
        try:
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


class Controller:
    def __init__(self, deps):
        self.deps = deps
        self.root = Path(deps['ROOT'])/'cost'/'finops'
        self.instance = secrets.token_hex(16)
        self.locks = {}
        self.guard = threading.Lock()

    def project_dir(self, aid):
        if not isinstance(aid,int) or aid < 1:
            raise web.HTTPNotFound()
        return self.root/str(aid)

    def lock(self, aid):
        with self.guard:
            return self.locks.setdefault(aid,threading.RLock())

    @contextmanager
    def mutation_lock(self, aid):
        """같은 ROOT를 사용하는 여러 프로세스에서도 옵션 CAS·작업 lease를 직렬화한다."""
        with self.lock(aid):
            folder=self.project_dir(aid)
            folder.mkdir(parents=True,exist_ok=True)
            with open(folder/'mutation.lock','a+b') as handle:
                if os.name == 'nt' and handle.tell()==0:
                    handle.write(b'0');handle.flush()
                deadline=time.monotonic()+15
                while True:
                    try:
                        handle.seek(0)
                        if os.name == 'nt':
                            import msvcrt
                            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                        break
                    except (BlockingIOError,PermissionError,OSError):
                        if time.monotonic()>deadline:
                            raise web.HTTPConflict(text='프로젝트 저장 잠금 대기 시간이 초과되었습니다.')
                        time.sleep(.05)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    def config(self, aid):
        try:
            return read_json(self.project_dir(aid)/'options.json', {'revision':0,'options':{'dataset':'resource'}})
        except (ValueError,OSError) as exc:
            raise web.HTTPConflict(text='저장 옵션을 읽을 수 없습니다. 원본 보존 후 명시적 복구가 필요합니다.') from exc

    def sources(self, a):
        """허용된 기존 케이스 조회 함수를 사용하며 원본은 절대 쓰지 않는다."""
        cid=a.get('infra_case_id')
        values={};statuses={};files={}
        folder=self.deps['infra_case_dir'](cid)/'Source' if cid else None
        for key,name in [('evidence','aicaf-evidence.json'),('topology','topology.json')]:
            p=folder/name if folder else None
            if p is None or not p.is_file():
                values[key]=None;statuses[key]={'status':'missing','reason':'연결된 수집 파일 없음'}
                continue
            try:
                raw=p.read_bytes()
                files[name]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
                val=json.loads(raw.decode('utf-8-sig'),parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if not isinstance(val,dict):
                    raise ValueError('invalid_schema')
                values[key]=val;statuses[key]={'status':'collected'}
            except PermissionError:
                values[key]=None;statuses[key]={'status':'permission_denied','reason':'수집 파일 읽기 권한 없음'}
            except (ValueError,UnicodeError,OSError):
                values[key]=None;statuses[key]={'status':'parse_failed','reason':'수집 파일 파싱 실패; 이전 결과로 대체하지 않음'}
        return values,statuses,files

    def collect(self, aid):
        a=self.deps['assessment'](aid)
        source,statuses,files=self.sources(a)
        with self.deps['db']() as conn:
            answers={r['qid']:dict(r) for r in conn.execute('select qid,score,summary,transcript from answers where assessment_id=?',(aid,))}
        questions=(self.deps['project_questions'](aid) if self.deps.get('project_questions')
                   else self.deps['questions'](a.get('track') or 'new'))
        return {'assessment':a,'source':source,'statuses':statuses,'files':files,'answers':answers,'questions':questions}

    def result(self, aid, options=None, revision=None, vector=None):
        vector=vector if vector is not None else self.collect(aid)
        a=vector['assessment'];source=vector['source'];statuses=vector['statuses'];files=vector['files']
        answers=vector['answers'];questions=vector['questions']
        saved=self.config(aid)
        ev=source['evidence']
        if ev is None:
            ev={'sections':{k:{'status':statuses['evidence']['status'],'ok':False,'reason':statuses['evidence']['reason']}
                            for k in ('costByResource','costMonthlyTotals')}}
        res=cost_calc.compute_finops(ev,a.get('track') or 'new',options if options is not None else saved['options'],source['topology'],questions,answers)
        res.update(customer=a['customer'],project_id=aid,revision=saved['revision'] if revision is None else revision,
                   source_status=statuses,input_files=files,data_classification=ev.get('dataClassification','collected_source'))
        res['analysis_id']=hashlib.sha256(encoded([res['analysis_id'],aid,a['customer'],files,res['revision']])).hexdigest()
        res['context'].update(tax_status='not_collected',credit_status='not_collected',discount_status='not_collected',
                              cost_definition='Cost Management 비용 조회 원장; 청구서 정산 총액과 동일하다고 단정하지 않음',
                              tag_key=res['options'].get('tag_key'))
        catalog=Path(__file__).with_name('cost_finops_sources.json')
        res['sources']=read_json(catalog,[])
        return res

    def save(self, aid, body):
        with self.mutation_lock(aid):
            vector=self.collect(aid)
            current=self.result(aid,vector=vector)
            if body.get('analysis_id')!=current['analysis_id'] or body.get('revision')!=current['revision']:
                raise web.HTTPConflict(text='입력·원본·버전이 변경되었습니다. 새로 조회한 뒤 저장해야 합니다.')
            revision=current['revision']+1
            result=self.result(aid,body.get('options'),revision,vector=vector)
            if hashlib.sha256(encoded(vector)).digest()!=hashlib.sha256(encoded(self.collect(aid))).digest():
                raise web.HTTPConflict(text='저장 검증 중 원본이 변경되었습니다. 조회 입력과 다른 데이터는 승인하지 않습니다.')
            record={'revision':revision,'options':result['options'],'analysis_id':result['analysis_id'],
                    'saved_at':datetime.now(timezone.utc).isoformat()}
            # 불변 버전을 먼저 기록한 뒤 단일 authoritative 포인터를 게시한다.
            atomic(self.project_dir(aid)/'versions'/f'{revision}-{result["analysis_id"]}.json',record)
            atomic(self.project_dir(aid)/'options.json',record)
            return result

    def job_dir(self, aid, job_id):
        import re
        if not isinstance(job_id,str) or not re.fullmatch(r'[0-9a-f]{32}',job_id):
            raise web.HTTPNotFound()
        return self.project_dir(aid)/'jobs'/job_id

    def snapshot(self, aid, job_id):
        folder=self.job_dir(aid,job_id)
        state=read_json(folder/'job.json')
        if not state:
            raise web.HTTPNotFound()
        raw=(folder/'analysis.json').read_bytes()
        if hashlib.sha256(raw).hexdigest()!=state['snapshot_sha256']:
            raise web.HTTPConflict(text='고정 분석 파일 무결성 불일치')
        return json.loads(raw.decode('utf-8'))

    def job(self, aid, job_id):
        folder=self.job_dir(aid,job_id)
        p=folder/'job.json'
        try:
            state=read_json(p)
            if not isinstance(state,dict) or 'status' not in state:
                if not folder.is_dir():
                    raise web.HTTPNotFound()
                raise ValueError('missing_state')
        except (ValueError,OSError):
            return {'job_id':job_id,'status':'corrupt','files':[],'error':'생성 상태 파일을 읽을 수 없습니다. 원본 상태는 보존하며 새 실행으로 복구합니다.',
                    'state_sha256':hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None}
        if state['status']=='running' and (not process_alive(state.get('pid')) or time.time()-state.get('beat',0)>120):
            state.update(status='interrupted',error='생성 작업이 중단되었습니다. 원본 실행은 보존되며 재생성할 수 있습니다.')
        for f in state.get('files',[]):
            f['url']=f'/api/aicaf/assessments/{aid}/cost/v2/jobs/{job_id}/files/{f["kind"]}'
        return state

    def history(self, aid):
        base=self.project_dir(aid)/'jobs'
        if not base.is_dir():
            return []
        result=[]
        for p in base.iterdir():
            if p.is_dir() and (p/'job.json').is_file():
                try:
                    result.append(self.job(aid,p.name))
                except (ValueError,OSError,web.HTTPException):
                    continue
        return sorted(result,key=lambda x:x.get('started',0),reverse=True)

    def create_job(self, aid, analysis_id, actor, start_worker=True):
        with self.mutation_lock(aid):
            frozen=self.result(aid)
            if frozen['analysis_id']!=analysis_id:
                raise web.HTTPConflict(text='분석 버전이 변경되었습니다. 현재 결과로 다시 생성해야 합니다.')
            active_path=self.project_dir(aid)/'active.json'
            try:
                active=read_json(active_path)
            except (ValueError,OSError):
                active=None
            if active and self.job(aid,active['job_id'])['status']=='running':
                raise web.HTTPConflict(text='현재 프로젝트에서 보고서를 생성 중입니다.')
            jid=uuid.uuid4().hex
            folder=self.job_dir(aid,jid)
            payload=encoded(frozen)
            state={'job_id':jid,'analysis_id':analysis_id,'status':'running','instance':self.instance,'pid':os.getpid(),
                   'started':time.time(),'beat':time.time(),'actor':actor,'customer':frozen['customer'],
                   'snapshot_sha256':hashlib.sha256(payload).hexdigest(),'files':[],
                   'steps':[{'name':'freeze','label':'분석 버전 고정','status':'done'},
                            {'name':'xlsx','label':'수식 Excel 작성·검사','status':'pending'},
                            {'name':'pptx','label':'템플릿 PPT 작성·검사','status':'pending'},
                            {'name':'integrity','label':'파일 해시·버전 검사','status':'pending'}]}
            atomic(folder/'analysis.json',payload)
            atomic(folder/'job.json',state)
            atomic(active_path,{'job_id':jid,'instance':self.instance})
            if start_worker:
                threading.Thread(target=self.run_job,args=(aid,jid),daemon=True).start()
            return self.job(aid,jid)

    def run_job(self, aid, jid):
        import io
        import zipfile
        folder=self.job_dir(aid,jid)
        state=read_json(folder/'job.json')
        stop=threading.Event()
        state_lock=threading.RLock()
        def update(**kw):
            with state_lock:
                state.update(kw,beat=time.time())
                atomic(folder/'job.json',deepcopy(state))
        def beat():
            while not stop.wait(2):
                update()
        monitor=threading.Thread(target=beat,daemon=True)
        monitor.start()
        try:
            import cost_finops_report as report
            frozen=self.snapshot(aid,jid)
            for idx,kind in [(1,'xlsx'),(2,'pptx')]:
                state['steps'][idx]['status']='running';update()
                output=getattr(report,f'build_{kind}')(frozen,frozen['customer'])
                required='xl/workbook.xml' if kind=='xlsx' else 'ppt/presentation.xml'
                if not isinstance(output,bytes) or not output:
                    raise ValueError(f'empty_{kind}')
                with zipfile.ZipFile(io.BytesIO(output)) as z:
                    if required not in z.namelist() or z.testzip():
                        raise ValueError(f'invalid_{kind}')
                atomic(folder/f'cost.{kind}',output)
                state['files'].append({'kind':kind,'bytes':len(output),'sha256':hashlib.sha256(output).hexdigest()})
                state['steps'][idx]['status']='done';update()
            state['steps'][3]['status']='running';update()
            self.snapshot(aid,jid)
            for f in state['files']:
                if hashlib.sha256((folder/f'cost.{f["kind"]}').read_bytes()).hexdigest()!=f['sha256']:
                    raise ValueError('file_hash_mismatch')
            state['steps'][3]['status']='done'
            stop.set();monitor.join(3)
            update(status='done',finished=time.time())
        except Exception as exc:
            stop.set();monitor.join(3)
            for s in state['steps']:
                if s['status']=='running':
                    s['status']='failed'
            update(status='failed',error=f'{type(exc).__name__}: {str(exc)[:240]}',finished=time.time(),files=[])


def register(app, **deps):
    """기존 포털 인증·멤버 권한·Origin 검사를 그대로 통과하는 v2 경로만 추가한다."""
    controller=Controller(deps)
    app['finops_controller']=controller
    @web.middleware
    async def finops_errors(req,handler):
        try:
            return await handler(req)
        except web.HTTPException as exc:
            if '/cost/v2' not in req.path or exc.status < 400:
                raise
            return web.json_response({'error':type(exc).__name__,'message':exc.text},status=exc.status,headers={'Cache-Control':'no-store'})
    app.middlewares.append(finops_errors)
    base='/api/aicaf/assessments/{aid}/cost/v2'
    async def auth(req, edit=False):
        try:
            aid=int(req.match_info['aid'])
        except (ValueError,KeyError):
            raise web.HTTPNotFound()
        role=deps['need_project'](req,aid,'interviewer' if edit else 'viewer')
        return aid,role
    def response(value):
        return web.Response(body=encoded(value),content_type='application/json',headers={'Cache-Control':'no-store'})
    async def current(req):
        aid,role=await auth(req)
        result=await asyncio.to_thread(controller.result,aid)
        result['can_edit']=role!='viewer'
        history=controller.history(aid)
        result['job']=history[0] if history else {'status':'none'}
        return response(result)
    async def save(req):
        aid,_=await auth(req,True)
        try:
            body=await req.json()
            if not isinstance(body,dict) or 'options' not in body:
                raise ValueError('options 입력이 필요합니다.')
            result=await asyncio.to_thread(controller.save,aid,body)
        except (ValueError,TypeError,KeyError) as exc:
            return web.json_response({'error':'validation_failed','message':str(exc)},status=422)
        deps['audit'](req['user']['upn'],aid,None,'finops-options',result['analysis_id'])
        result['can_edit']=True
        result['job']={'status':'none'}
        return response(result)
    async def build(req):
        aid,_=await auth(req,True)
        body=await req.json()
        if not isinstance(body,dict):
            raise web.HTTPUnprocessableEntity(text='분석 ID가 필요합니다.')
        result=await asyncio.to_thread(controller.create_job,aid,body.get('analysis_id'),req['user']['upn'])
        deps['audit'](req['user']['upn'],aid,None,'finops-build',result['job_id'])
        return response(result)
    async def job(req):
        aid,_=await auth(req)
        return response(controller.job(aid,req.match_info['jid']))
    async def history(req):
        aid,_=await auth(req)
        return response({'runs':controller.history(aid)})
    async def file(req):
        import io,zipfile,urllib.parse
        aid,_=await auth(req)
        jid=req.match_info['jid'];kind=req.match_info['kind']
        if kind not in ('xlsx','pptx'):
            raise web.HTTPNotFound()
        state=controller.job(aid,jid)
        expected=next((f for f in state.get('files',[]) if f['kind']==kind),None)
        p=controller.job_dir(aid,jid)/f'cost.{kind}'
        if state['status']!='done' or not expected or not p.is_file():
            raise web.HTTPNotFound(text='완료된 파일이 없습니다.')
        data=await asyncio.to_thread(p.read_bytes)
        if len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:
            raise web.HTTPConflict(text='다운로드 파일 무결성 불일치. 새로 생성해야 합니다.')
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if z.testzip():
                    raise ValueError()
        except (ValueError,zipfile.BadZipFile):
            raise web.HTTPConflict(text='다운로드 ZIP 검사 실패')
        mime={'xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','pptx':'application/vnd.openxmlformats-officedocument.presentationml.presentation'}[kind]
        name=f'AI_Assessment_Cost_{state["customer"]}_{jid[:8]}.{kind}'
        deps['audit'](req['user']['upn'],aid,None,'finops-download',jid+':'+kind)
        return web.Response(body=data,content_type=mime,headers={'Content-Disposition':"attachment; filename*=UTF-8''"+urllib.parse.quote(name),
                             'X-Content-SHA256':expected['sha256'],'ETag':'"'+expected['sha256']+'"','Cache-Control':'no-store'})
    app.add_routes([web.get(base,current),web.put(base+'/options',save),web.post(base+'/build',build),
                    web.get(base+'/history',history),web.get(base+'/jobs/{jid}',job),web.get(base+'/jobs/{jid}/files/{kind}',file)])
