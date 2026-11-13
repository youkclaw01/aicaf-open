"""장표 소유·원문 메타데이터 준비는 모든 픽셀 contact sheet를 먼저 만들지 않는다."""
from pathlib import Path
import sys
import hashlib
import json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
import asis_service


def test_page_metadata_does_not_build_pixel_contacts(tmp_path,monkeypatch):
    p=Presentation();slide=p.slides.add_slide(p.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1),Inches(1),Inches(4),Inches(1)).text='grounded fixture'
    p.save(tmp_path/'fixture.pptx')
    Image.new('RGB',(96,96),'white').save(tmp_path/'page.png')
    (tmp_path/'fixture.layout.json').write_text(json.dumps({'pages':[{'item_refs':['A0001']}]}),encoding='utf-8')
    manifest={'artifacts':[{'role':'asis_report_pptx','name':'fixture.pptx'}],
        'render':{'previews':[{'name':'page.png','sha256':hashlib.sha256((tmp_path/'page.png').read_bytes()).hexdigest()}]}}
    def no_contacts(*args,**kwargs):
        raise AssertionError('metadata extraction must not allocate contact pixels')
    monkeypatch.setattr(Image,'new',no_contacts)
    metadata=getattr(asis_service,'_page_metadata',None)
    assert callable(metadata),'ownership metadata must be prepared separately before parallel visual requests'
    pages=metadata(tmp_path,manifest)
    assert len(pages)==1 and pages[0]['texts']==['grounded fixture']
    assert pages[0]['item_refs']==['A0001'] and pages[0]['width']==pages[0]['height']==96
