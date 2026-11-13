"""Windows 원본 Office 변환. 승인 PPT·Excel 원본은 읽기 전용으로 열고 별도 파일만 출력한다."""
from pathlib import Path
import os


def convert(source,output_dir,extension):
    if os.name!='nt':raise RuntimeError('native_office_requires_windows')
    source=Path(source).resolve();directory=Path(output_dir).resolve();directory.mkdir(parents=True,exist_ok=True)
    output=directory/(source.stem+'.'+extension)
    if output==source:raise ValueError('native_office_would_overwrite_source')
    if extension not in ('pdf','xlsx'):raise ValueError('unsupported_native_office_format')
    import pythoncom
    from win32com.client import DispatchEx
    pythoncom.CoInitialize();app=None;document=None;owned=False
    try:
        if extension=='pdf':
            if source.suffix.lower()!='.pptx':raise ValueError('native_pdf_requires_pptx')
            app=DispatchEx('PowerPoint.Application')
            # 기존 작업 중인 발표가 잡힌 경우 기존 Office를 종료하지 않고 실패한다.
            if app.Presentations.Count:raise RuntimeError('office_instance_not_isolated')
            owned=True;app.DisplayAlerts=1;app.AutomationSecurity=3
            document=app.Presentations.Open(str(source),ReadOnly=True,Untitled=False,WithWindow=False)
            # 공식 ppSaveAsPDF=32: learn.microsoft.com/office/vba/api/powerpoint.ppsaveasfiletype
            document.SaveAs(str(output),32)
        else:
            if source.suffix.lower()!='.xlsx':raise ValueError('native_xlsx_requires_xlsx')
            app=DispatchEx('Excel.Application')
            if app.Workbooks.Count:raise RuntimeError('office_instance_not_isolated')
            owned=True;app.Visible=False;app.DisplayAlerts=False;app.AskToUpdateLinks=False;app.AutomationSecurity=3
            document=app.Workbooks.Open(str(source),UpdateLinks=0,ReadOnly=True,IgnoreReadOnlyRecommended=True)
            app.CalculateFullRebuild()
            # 공식 xlOpenXMLWorkbook=51. 원본 경로와 다른 재계산 사본만 저장한다.
            document.SaveAs(str(output),FileFormat=51)
        if not output.is_file() or output.stat().st_size==0:raise RuntimeError('native_office_output_missing')
        return output
    finally:
        if document is not None:
            if extension=='pdf':document.Close()
            else:document.Close(SaveChanges=False)
        if app is not None and owned:app.Quit()
        document=None;app=None;pythoncom.CoUninitialize()

def convert_isolated(source,output_dir,extension,*,python=None,timeout=180):
    """Office 라이브러리가 없는 포털에서도 검증된 작업자 인터프리터를 사용한다."""
    import hashlib,json,subprocess
    if os.name!='nt':raise RuntimeError('native_office_requires_windows')
    source=Path(source).resolve();directory=Path(output_dir).resolve()
    if extension not in ('pdf','xlsx'):raise ValueError('unsupported_native_office_format')
    expected=directory/(source.stem+'.'+extension)
    if expected==source:raise ValueError('native_office_would_overwrite_source')
    worker=Path(python or os.environ.get('AICAF_ASIS_PYTHON') or
        (Path(__file__).resolve().parents[1]/'.venv-asis-maf/Scripts/python.exe'))
    if not worker.is_file():raise RuntimeError('native_office_worker_interpreter_unavailable')
    before=hashlib.sha256(source.read_bytes()).hexdigest();directory.mkdir(parents=True,exist_ok=True)
    keep={'PATH','PATHEXT','SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','TEMP','TMP','TMPDIR',
          'USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','APPDATA','LOCALAPPDATA','PROGRAMFILES',
          'PROGRAMFILES(X86)','PROGRAMDATA','ALLUSERSPROFILE','LANG','LC_ALL'}
    env={key:value for key,value in os.environ.items() if key.upper() in keep};env['PYTHONIOENCODING']='utf-8'
    command=[str(worker),'-B',str(Path(__file__).resolve()),str(source),str(directory),extension]
    try:run=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',env=env,timeout=timeout)
    except subprocess.TimeoutExpired as exc:raise RuntimeError('native_office_child_timeout_effect_unknown') from exc
    except OSError as exc:raise RuntimeError('native_office_child_failed_to_start') from exc
    if run.returncode:raise RuntimeError('native_office_child_failed:'+run.stderr[-1000:])
    lines=[line.split('=',1)[1] for line in run.stdout.splitlines() if line.startswith('NATIVE_OFFICE_RESULT=')]
    if len(lines)!=1:raise RuntimeError('native_office_child_result_invalid')
    record=json.loads(lines[0])
    if (Path(record.get('output','')).resolve()!=expected or not expected.is_file()
            or expected.stat().st_size==0 or record.get('sha256')!=hashlib.sha256(expected.read_bytes()).hexdigest()):
        raise RuntimeError('native_office_child_output_invalid')
    if hashlib.sha256(source.read_bytes()).hexdigest()!=before:raise RuntimeError('native_office_source_changed')
    return expected


if __name__=='__main__':
    import argparse,hashlib,json,sys
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser();parser.add_argument('source');parser.add_argument('output_dir')
    parser.add_argument('extension',choices=['pdf','xlsx']);args=parser.parse_args()
    output=convert(args.source,args.output_dir,args.extension)
    print('NATIVE_OFFICE_RESULT='+json.dumps({'output':str(output.resolve()),'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}),flush=True)
