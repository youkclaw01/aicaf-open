"""공식 Office 상수를 이용한 Windows 변환 분기부터 RED로 검증한다."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_artifacts as artifacts

def test_windows_native_office_render_branch(tmp_path,monkeypatch):
    import platform
    import asis_office
    monkeypatch.setattr(platform,'system',lambda:'Windows')
    calls=[]
    def convert(source,dest,extension):
        calls.append((source,dest,extension));p=Path(dest)/(Path(source).stem+'.'+extension);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'unit-office-branch-only');return p
    monkeypatch.setattr(asis_office,'convert',convert)
    source=tmp_path/'source.pptx';source.write_bytes(b'unit-branch-only')
    result=artifacts.convert(source,tmp_path/'out','pdf',tmp_path/'profile')
    assert result.read_bytes()==b'unit-office-branch-only' and calls[0][2]=='pdf'
