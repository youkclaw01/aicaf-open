"""제품 AS-IS 작업의 분리 실행과 읽기 전용 프로세스 확인. 개발 Agent를 실행하지 않는다."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def process_identity(pid):
    """PID 재사용을 구별한다. Windows의 os.kill(pid, 0)는 사용하지 않는다."""
    if not isinstance(pid,int) or isinstance(pid,bool) or pid<1:return None
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return 'unverifiable' if ctypes.get_last_error()==5 else None
        try:
            code=wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)):return 'unverifiable'
            if code.value!=259:return None
            times=[wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*(ctypes.byref(t) for t in times)):return 'unverifiable'
            return str((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)
        finally:kernel.CloseHandle(handle)
    try:
        proc=subprocess.run(['ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,encoding='utf-8',timeout=5)
        return proc.stdout.strip() or None
    except (OSError,subprocess.TimeoutExpired):return 'unverifiable'


def owner_proven_dead(row):
    """PID가 종료됐거나 검증된 생성 식별자가 달라졌을 때만 원소유자 종료로 판정한다."""
    if not isinstance(row,dict):return False
    pid=row.get('pid')
    if pid is None or (isinstance(pid,int) and not isinstance(pid,bool) and pid==0):return True
    if not isinstance(pid,int) or isinstance(pid,bool) or pid<1:return False
    current=process_identity(pid)
    if current is None:return True
    expected=row.get('process_identity')
    if current=='unverifiable' or not isinstance(expected,(str,int)) or isinstance(expected,bool):return False
    if str(expected) in ('','unverifiable'):return False
    return str(current)!=str(expected)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--assessment',required=True,type=int)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--report-kind', choices=('current_state','interview'))
    args=parser.parse_args()
    for stream in (sys.stdout,sys.stderr):
        configure=getattr(stream,'reconfigure',None)
        if configure:configure(encoding='utf-8')
    from asis_view import Controller
    # 생성은 이미 권한 확인을 거친 동결 입력만 사용한다. 운영 DB나 원본 수집 파일을 다시 쓰지 않는다.
    control=Controller(ROOT=Path(args.data_dir),assessment=None,infra_case_dir=None,db=None,report_kind=args.report_kind)
    result=control.generate(args.assessment,args.revision)
    assert isinstance(result,dict)
    print('asis_worker_finished:'+result['status'],flush=True)
    return 0 if result['status']=='generated_candidate' else 1


if __name__=='__main__':raise SystemExit(main())
