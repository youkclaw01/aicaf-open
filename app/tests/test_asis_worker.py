"""장시간 작업의 상태·분리 실행·같은 개정 보존을 고객 데이터 없이 검증한다."""
from pathlib import Path
import os
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_model
import asis_view


def controller(tmp_path):
    return asis_view.Controller(ROOT=tmp_path,assessment=lambda aid:{},infra_case_dir=lambda cid:tmp_path,db=None)


def running_job(control):
    aid=999999;revision='r-20000101T000000-0000000000000000'
    folder=control.job_dir(aid,revision);folder.mkdir(parents=True)
    asis_view.atomic(folder/'job.json',{'run':{'assessment_id':aid},'revision_id':revision,
        'status':'running','started_epoch':time.time()-3600,'heartbeat_epoch':time.time(),
        'pid':os.getpid()})
    return aid,revision,folder


def test_recent_heartbeat_keeps_long_running_job_active(tmp_path):
    control=controller(tmp_path);aid,revision,folder=running_job(control)
    assert control.job(aid,revision)['status']=='running'


def test_verified_live_owner_keeps_stale_heartbeat_running(tmp_path):
    control=controller(tmp_path);aid,revision,folder=running_job(control)
    row=asis_view.read(folder/'job.json');assert isinstance(row,dict)
    row['heartbeat_epoch']=time.time()-3600
    asis_view.atomic(folder/'job.json',row)
    assert control.job(aid,revision)['status']=='running'


def test_dead_owner_marks_interrupted_and_keeps_candidate(tmp_path):
    control=controller(tmp_path);aid,revision,folder=running_job(control)
    row=asis_view.read(folder/'job.json');assert isinstance(row,dict)
    row.update(heartbeat_epoch=time.time()-3600,pid=999999999)
    asis_view.atomic(folder/'job.json',row)
    asis_view.atomic(control.folder(aid)/'state.json',{'candidate_revision_id':'previous-reviewed-candidate'})
    assert control.job(aid,revision)['status']=='interrupted'
    assert control.state(aid)['candidate_revision_id']=='previous-reviewed-candidate'
