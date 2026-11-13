"""독립 실물 결함의 현재 원본 회귀. 지정 표지의 위계를 실제 원본 PPT와 비교한다."""
from pathlib import Path
import json
import sys
from pptx import Presentation

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from private_archive_source import SOURCE,SEMANTIC_SOURCE,PRIVATE_ARCHIVE_REQUIRED,PRIVATE_SEMANTIC_REQUIRED


@PRIVATE_ARCHIVE_REQUIRED
def test_cover_title_uses_current_template_geometry_and_font(tmp_path):
    import asis_model,asis_analysis,asis_artifacts
    a=json.loads((SOURCE/'interview_E.json').read_text(encoding='utf-8'))['assessment']
    result=asis_analysis.analyze(asis_model.normalize(SOURCE,a,[],'cover-test',classification='archived_test_snapshot'))
    output=tmp_path/'cover.pptx';asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result),output)
    actual=Presentation(output).slides[0].shapes[1]
    donor=Presentation(asis_model.ASSET/'visual-v18.pptx').slides[0].shapes[1]
    assert (actual.left,actual.top,actual.width,actual.height)==(donor.left,donor.top,donor.width,donor.height)
    assert actual.text_frame.paragraphs[0].runs[0].font.size==donor.text_frame.paragraphs[0].runs[0].font.size
    assert 'AI Cost Governance' not in actual.text


@PRIVATE_ARCHIVE_REQUIRED
def test_actual_render_keeps_same_installed_korean_font(tmp_path):
    import fitz
    import asis_model,asis_analysis,asis_artifacts
    a=json.loads((SOURCE/'interview_E.json').read_text(encoding='utf-8'))['assessment']
    result=asis_analysis.analyze(asis_model.normalize(SOURCE,a,[],'font-test',classification='archived_test_snapshot'))
    manifest=asis_artifacts.build(__import__('visual_spec_stub').with_visual_specs(result),tmp_path)
    pdf=fitz.open(tmp_path/next(f['name'] for f in manifest['artifacts'] if f['role']=='asis_render_pdf'))
    unexpected=[]
    for number,page in enumerate(pdf,1):
        for block in page.get_text('dict')['blocks']:
            for line in block.get('lines',[]):
                for span in line.get('spans',[]):
                    if any('가'<=c<='힣' for c in span['text']) and 'malgun' not in span['font'].lower():
                        unexpected.append((number,span['font'],span['text']))
    assert not unexpected, unexpected[:5]
    assert any(link['kind']==fitz.LINK_GOTO for page in pdf for link in page.get_links())


@PRIVATE_SEMANTIC_REQUIRED
def test_semantic_many_to_many_refs_and_full_actions_are_present_in_native(tmp_path):
    import asis_artifacts
    result=json.loads(SEMANTIC_SOURCE.read_text(encoding='utf-8'))
    path=tmp_path/'trace.pptx';asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result),path)
    deck=Presentation(path)
    traces=[s for s in deck.slides if any('근거 관계 원장' in shape.text for shape in s.shapes if shape.has_text_frame)]
    joined='\n'.join(shape.text for slide in traces for shape in slide.shapes if shape.has_text_frame)
    actual_implications={identifier for direction in result['stages'][1]['directions'] for identifier in direction['area_implication_refs']}
    assert all(identifier in joined for identifier in actual_implications)
    assert 'O_' in joined
    body='\n'.join(shape.text for slide in deck.slides for shape in slide.shapes if shape.has_text_frame)
    compact=''.join(body.split())
    for action in result['stages'][2]['actions']:
        assert ''.join(action['deliverable'].split()) in compact
        for item in action['prerequisites']: assert ''.join(item.split()) in compact
    hyperlinks=[]
    with __import__('zipfile').ZipFile(path) as archive:
        from lxml import etree
        for name in archive.namelist():
            if name.startswith('ppt/slides/_rels/'):
                hyperlinks.extend(n.get('Target') for n in etree.fromstring(archive.read(name)) if n.get('Type','').endswith('/hyperlink'))
    assert all(criterion['source_url'] in hyperlinks for criterion in result['input']['criteria'])

