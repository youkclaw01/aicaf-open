"""승인 보고서의 페이지·패널 문법을 유지하는 결정적 네이티브 표현 계층."""
from copy import deepcopy
import hashlib,json,zipfile
from pathlib import Path
from pptx import Presentation
from pptx.oxml import parse_xml
from pptx.enum.shapes import MSO_CONNECTOR
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches
from asis_model import ASSET


def presentation(result,destination,*,section=None):
    from asis_artifacts import text,box,wrapped_lines,paragraphs_pages,sha,CONTENT_HASH,TEMPLATE_HASH,COLOR,native_diagram_rows
    from asis_analysis import AREA_NAMES
    import asis_consolidated_deck as consolidated
    source=ASSET/'content-asis-e.pptx'
    if sha(source)!=CONTENT_HASH or sha(ASSET/'visual-v18.pptx')!=TEMPLATE_HASH:raise ValueError('approved_source_binding_failed')
    deck=Presentation(source);donors=list(deck.slides)
    # 관계 파트는 원본 그대로 살아 있고 표시 순서만 새 페이지들로 구성한다.
    for sid in list(deck.slides._sldIdLst):
        deck.part.drop_rel(sid.rId)
        deck.slides._sldIdLst.remove(sid)
    data=result['input'];ctx=data['context'];stages=result['stages'];manifest=[]
    integrated = ctx.get('report_kind') == 'integrated'
    report_name = '현황분석 종합 및 시사점 및 개선과제 보고서' if integrated else '현황 분석 보고서'
    policy=result.get('layout_policy',{})
    from asis_report_visuals import architecture_pages,synthesis_overviews,action_overviews
    def clone(index,family,title,subtitle):
        title=consolidated.to_polite_korean(title)
        subtitle=consolidated.to_polite_korean(subtitle)
        donor=donors[index];s=deck.slides.add_slide(donor.slide_layout)
        for sh in list(s.shapes):s.shapes._spTree.remove(sh._element)
        mapping={}
        for rel in donor.part.rels.values():
            if rel.reltype.endswith('/notesSlide') or rel.reltype.endswith('/slideLayout'):continue
            mapping[rel.rId]=s.part.relate_to(rel.target_ref if rel.is_external else rel.target_part,rel.reltype,is_external=rel.is_external)
        for sh in donor.shapes:
            node=parse_xml(sh._element.xml)
            for child in node.iter():
                for key,val in list(child.attrib.items()):
                    if key.startswith('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}') and val in mapping:child.set(key,mapping[val])
            s.shapes._spTree.insert_element_before(node,'p:extLst')
        # 고정 머리말·꼬리말 이외의 샘플 고객 본문을 제거한다.
        for sh in list(s.shapes):
            top=sh.top/914400
            if 2.15<=top<6.99:s.shapes._spTree.remove(sh._element)
        for sh in s.shapes:
            if not sh.has_text_frame:continue
            top=sh.top/914400
            value=title if .80<=top<=1.20 else subtitle if 1.50<=top<=1.90 else 'AI Assessment · '+report_name if top>7 and sh.left/914400<10 else '' if top>7 else None
            if top<.60:
                kickers={'cover':'AI ASSESSMENT  /  '+('Summary Report' if integrated else '현황 분석 보고서'),'executive':'EXECUTIVE SUMMARY',
                    'scope':'Ⅱ  /  현황 분석 · 범위','structure':'Ⅱ  /  현황 분석 · 구성',
                    'framework_waf':'Ⅱ  /  현황 분석 · 2.1 외부 환경 분석',
                    'framework_landing_zone':'Ⅱ  /  현황 분석 · 2.1 외부 환경 분석',
                    'architecture_overview':'Ⅱ  /  현황 분석 · 2.2 내부 환경 분석 · 아키텍처',
                    'inventory':'Ⅱ  /  현황 분석 · 2.2 내부 환경 분석',
                    'area_fact':'Ⅱ  /  현황 분석 · 2.2 내부 환경 분석',
                    'maturity_spider':'Ⅱ  /  현황 분석 · 인터뷰 성숙도 종합 진단',
                    'fact_synthesis':'Ⅱ  /  현황 분석 · 2.3 시사점 종합',
                    'fact_synthesis_overview':'Ⅱ  /  현황 분석 · 2.3 시사점 종합',
                    'area_diagnosis_insight':'Ⅱ  /  현황 분석 · 2.3 시사점 종합',
                    'diagnosis_action':'Ⅲ  /  진단·개선과제',
                    'diagnosis_action_overview':'Ⅲ  /  진단·개선과제 · 과제 도출',
                    'action_overview':'Ⅲ  /  진단·개선과제 · 구현 방안',
                    'sources':'SOURCES'}
                value=kickers.get(family,'별첨  /  근거·상세 원장')
                sh.name='approved_kicker:'+family
            if value is not None:
                runs=[r for p in sh.text_frame.paragraphs for r in p.runs]
                if runs:
                    runs[0].text=value
                    for r in runs[1:]:r.text=''
                    if .80<=top<=1.20 and len(value)>30:
                        from pptx.util import Pt as _Pt
                        runs[0].font.size=_Pt(18 if len(value)>48 else 20 if len(value)>40 else 22)
                else:sh.text_frame.paragraphs[0].add_run().text=value
        marker=text(s,'',.62,.28,.01,.01,9);marker.name='approved_family:'+family
        manifest.append({'family':family,'donor_page':index+1,'title':title,'slide':s})
        note(s,{'family':family,'source_page':index+1})
        return s
    def note(s,record):
        frame=s.notes_slide.notes_text_frame
        if frame is None:
            from xml.sax.saxutils import escape
            payload=escape(json.dumps(record,ensure_ascii=False))
            node=parse_xml('<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:nvSpPr><p:cNvPr id="2" name="Notes body"/><p:cNvSpPr/><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>'+payload+'</a:t></a:r></a:p></p:txBody></p:sp>')
            s.notes_slide.shapes._spTree.insert_element_before(node,'p:extLst')
            frame=s.notes_slide.notes_text_frame
        if frame is None:raise ValueError('approved_notes_body_missing')
        frame.text=json.dumps(record,ensure_ascii=False)
        allowed = set(data['allocated_ids']['diagnoses']) | set(data['allocated_ids']['actions'])
        def referenced(value):
            if isinstance(value, dict):
                own = {value['id']} & allowed if isinstance(value.get('id'), str) else set()
                return own | set().union(*(referenced(v) for v in value.values()))
            if isinstance(value, (list, tuple)):
                return set().union(*(referenced(v) for v in value))
            return {value} & allowed if isinstance(value, str) else set()
        refs = referenced(record)
        for entry in manifest:
            if entry['slide'] is s:
                entry['item_refs'] = sorted(set(entry.get('item_refs', [])) | refs)
                break
    def write(s,value,x,y,w,h,pt=11.2,bold=False):
        value=consolidated.to_polite_korean(value)
        lines=wrapped_lines(value,w,pt)
        if len(lines)*(pt/72*1.18)>h+.03:raise ValueError('approved_text_capacity:'+value[:40])
        return text(s,'\n'.join(lines),x,y,w,h,pt,bold)
    def line(s,start,end,key):
        shape=s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,Inches(start[0]),Inches(start[1]),Inches(end[0]),Inches(end[1]))
        shape.name='approved_relation:'+key;shape.line.color.rgb=__import__('pptx').dml.color.RGBColor.from_string(COLOR['primary'])
        node=shape._element;parent=node.getparent();parent.remove(node);parent.insert(2,node)
    def block_pages(title,blocks,family='detail',donor=7,width=11.96,max_lines=17,item_refs=()):
        capacity=min(max_lines,int(policy.get('body_max_lines',max_lines)))
        polite_blocks=[(consolidated.to_polite_korean(k),consolidated.to_polite_korean(v)) for k,v in blocks]
        for i,page in enumerate(paragraphs_pages(polite_blocks,width=width,pt=11.7,max_lines=capacity)):
            s=clone(donor,family,title+(' · 계속' if i else ''),'확인 사실 · 기준 대조 · 고객 영향 · 개선 조건');y=2.30
            for label,value in page:
                text(s,label,.72,y,width,.28,11.2,True,'primary');y+=.34
                lines=wrapped_lines(value,width,11.7);height=max(.22,len(lines)*.215)
                text(s,'\n'.join(lines),.72,y,width,height,11.7);y+=height+.15
            if y>6.90:raise ValueError('approved_detail_capacity')
            note(s,{'family':family,'blocks':page})
            manifest[-1]['item_refs'] = list(item_refs)
    if section in (None,'front'):
        # 표지는 승인된 원본 객체의 위계를 유지한다.
        s=clone(0,'cover',str(ctx['customer'])+('\n종합 보고서' if integrated else '\nAS-IS 현황 분석'),
                '현황분석과 실제 인터뷰를 결합한 종합 판단·시사점·개선과제' if integrated else 'AI 도입·활용 개선을 위한 현재 환경·근거 기반 분석')
        for sh in list(s.shapes):
            if 3.0<sh.top/914400<6.8:s.shapes._spTree.remove(sh._element)
        text(s,report_name if integrated else '수집 구성 · 활용 관측 · 비용 · 기준 대조 · 개선과제',.70,3.55,11.90,.50,21.6)
        text(s,'업무 목표·효과는 확인 근거와 미확인을 구분하여 제시',.70,4.40,11.60,.90,20.7)
        summary=stages[3]['summary']
        def _exec_title(hl,limit=54):
            import re as _etre
            t=consolidated._clean_no_paren(hl) if hasattr(consolidated,'_clean_no_paren') else str(hl or '').strip()
            for m in ('병존하므로, ','확인되고 ','확보되어 있으나, '):
                if m in t:
                    _,tail=t.split(m,1)
                    if len(tail)<=limit:return tail
                    tc=tail.replace('실운영 대조 검증을 선행 조건으로 ','실운영 검증 기반 ').replace('단계적으로 정비해야 한다.','단계적으로 정비한다.')
                    if len(tc)<=limit:return tc
            return consolidated.concise_headline(t,limit)
        exec_title=_exec_title(summary['headline'],54) if integrated else summary['headline']
        s=clone(1,'executive',exec_title,'현재 구성과 판단 범위에 기반한 우선 의사결정')
        metrics=ctx.get('inventory_summary',[])
        by_type={r['type']:r['count'] for r in metrics}
        tiles=[('전체 리소스',str(sum(r['count'] for r in metrics))),('AI 계정',str(by_type.get('microsoft.cognitiveservices/accounts',0))),('VM',str(by_type.get('microsoft.compute/virtualmachines',0))),('업무 목표·효과','미확인' if not data['goals'] else '진술 근거')]
        for i,(label,value) in enumerate(tiles):
            x=.72+(i%2)*2.95;y=2.32+(i//2)*1.62
            box(s,x,y,2.80,1.48,'panel');text(s,value,x+.15,y+.08,2.5,.60,30,True,'primary');text(s,label,x+.15,y+.72,2.55,.60,11.2)
        if integrated:
            import re as _exec_re
            kp_labels={'S_network':'네트워크·컴퓨트·데이터','S_ai':'AI·품질·보안·거버넌스','S_telemetry':'관측·자동화·비용·FinOps'}
            def _exec_msg(msg,limit=104):
                t=consolidated._clean_no_paren(msg) if hasattr(consolidated,'_clean_no_paren') else str(msg or '').strip()
                sents=[x.strip()+'다.' for x in _exec_re.split(r'다\.(?=\s|$)',t) if x.strip()]
                for st in reversed(sents):
                    if len(st)<=limit:return st
                    if '삼고, ' in st and '기반하여 ' in st:
                        cand=st.split('삼고, ',1)[0]+'삼아 '+st.split('기반하여 ',1)[1]
                        if len(cand)<=limit:return cand
                return consolidated.concise_headline(t,limit)
            def _exec_kp(k,limit=82):
                lbl=kp_labels.get(k['id'])
                if not lbl:
                    m=_exec_re.match(r'^\s*\[([^\]]+)\]',str(k.get('text','')))
                    lbl=m.group(1).strip() if m else str(k['id']).replace('S_','')
                raw_k=str(k.get('text','')).replace('`','').strip()
                raw_k=_exec_re.sub(r'^\s*\[[^\]]{1,40}\]\s*','',raw_k)
                for _ in range(2):raw_k=_exec_re.sub(r'\s*\([^()]*\)','',raw_k)
                raw_k=_exec_re.sub(r'\s+',' ',raw_k).strip(' ,;')
                sents=[x.strip()+'다.' for x in _exec_re.split(r'다\.(?=\s|$)',raw_k) if x.strip()]
                last_s=sents[-1] if sents else raw_k
                for m in ('따라서 ','이에 따라 ','선행 확인하여 ','확인하여 '):
                    if m in last_s:
                        _,body=last_s.split(m,1)
                        if len(body)<=limit:return (lbl,body)
                        for ending in (' 단계적으로 이행해야 한다.',' 추진해야 한다.',' 정비해야 한다.'):
                            if body.endswith(ending):
                                core=body[:-len(ending)].strip(' ,')
                                items=[x.strip() for x in _exec_re.split(r',\s*|\s+및\s+',core) if x.strip()]
                                for cnt in range(len(items),0,-1):
                                    li=items[cnt-1]
                                    lch=li[-1] if li else ''
                                    bat=('가'<=lch<='힣') and ((ord(lch)-0xAC00)%28!=0)
                                    cand=', '.join(items[:cnt])+('을' if bat else '를')+ending
                                    if len(cand)<=limit:return (lbl,cand)
                return (lbl,consolidated.concise_headline(raw_k,limit))
            exec_blocks=[('현황 판단',consolidated.to_polite_korean(_exec_msg(summary['message'],104)))]+[(lbl,consolidated.to_polite_korean(val)) for lbl,val in [_exec_kp(k,82) for k in summary['key_points'][:3]]]
            y=2.32
            for label,value in exec_blocks:
                text(s,label,6.75,y,5.93,.22,10.5,True,'primary');y+=.23
                ls=wrapped_lines(value,5.93,10.2);h=max(.20,len(ls)*.185);text(s,'\n'.join(ls),6.75,y,5.93,h,10.2);y+=h+.07
            box(s,.72,5.65,11.96,.50,'light')
            n_dirs=len(stages[1].get('directions',[])) if len(stages)>1 and isinstance(stages[1],dict) else 0
            n_acts=len(stages[2].get('actions',[])) if len(stages)>2 and isinstance(stages[2],dict) else 0
            write(s,f'{n_dirs}개 종합 개선 방향(2.3)으로 묶어 목표 구성과 {n_acts}개 개선 과제로 연결합니다.',.94,5.71,11.52,.38,11.2,True)
            text(s,'상세 판정과 근거 값은 첨부 엑셀(Integrated-Backdata.xlsx)',.72,6.72,9.20,.24,10,False,'muted')
        else:
            exec_pages=paragraphs_pages([('현황 판단',consolidated.to_polite_korean(summary['message']))]+[(k['id'],consolidated.to_polite_korean(k['text'])) for k in summary['key_points']],width=5.93,pt=11.7,max_lines=13)
            y=2.32
            for label,value in exec_pages[0]:
                text(s,label,6.75,y,5.93,.25,11.2,True);y+=.29
                ls=wrapped_lines(value,5.93,11.7);h=max(.22,len(ls)*.215);text(s,'\n'.join(ls),6.75,y,5.93,h,11.7);y+=h+.12
        note(s,{'summary':summary,'same_analysis_revision':data['run']})
        if not integrated:
            for page in exec_pages[1:]:block_pages('종합 판단 · 상세',page,'executive',1)
            block_pages('의사결정 조건',[(str(i+1),v) for i,v in enumerate(summary['decision_points'])],'executive',1)
        s=clone(2,'structure','외부 기준과 내부 현황을 연결해 개선 방향을 판단한다','현재 구성·활용·비용을 업무 목표 및 공식 기준과 대조')
        lanes=[('도입·운영 목표','업무·품질·허용 중단·책임·효과'),('현재 환경','네트워크·컴퓨트·데이터·AI 서비스'),('공통 통제','보안·거버넌스·관측·품질·운영'),('비용·경제성','지출·사용량·단위경제성·비AI 대안')]
        for i,(label,detail) in enumerate(lanes):
            y=2.40+i*.73
            box(s,.72,y,3.10,.56,'panel');write(s,label,.84,y+.10,2.86,.35,11.2,True)
            line(s,(3.86,y+.28),(4.40,y+.28),'scope-'+str(i))
            box(s,4.44,y,3.40,.56,'light');write(s,detail,4.56,y+.08,3.16,.43,10.4)
        for i,(label,detail) in enumerate([('2.1 외부 기준','WAF AI · CAF AI · AI Landing Zones'),('2.2 내부 현황','전체 구성·인벤토리·영역별 Fact Finding'),('2.3 시사점·과제','다대다 근거·진단·유지/변경/추가/확인')]):
            y=2.40+i*1.00;box(s,9.30,y,3.38,.82,'panel');write(s,label+'\n'+detail,9.42,y+.10,3.14,.65,10.4,True)
            if i<2:line(s,(10.99,y+.85),(10.99,y+.97),'report-'+str(i))
        note(s,{'family':'structure','run':data['run'],'domains':list(AREA_NAMES),'same_analysis_revision':True})
        metadata={r['id']:r for r in json.loads((ASSET/'criteria-metadata.json').read_text(encoding='utf-8'))}
        from collections import Counter
        waf=[c for c in data['criteria'] if metadata.get(c['id'],{}).get('framework','').endswith('AI workloads design principles')]
        pillar_groups=[('안정성','WAF-AI-RE'),('보안','WAF-AI-SE'),('비용','WAF-AI-CO'),('운영 우수성','WAF-AI-OE'),('성능 효율','WAF-AI-PE')]
        s=clone(3,'framework_waf','AI 워크로드의 설계 기준은 기능·비기능 요구를 함께 다룬다','공식 원칙과 고객 적용 판정은 구분하며 원문·권고 강도를 원장에 보존')
        for i,(label,prefix) in enumerate(pillar_groups):
            selected=[c for c in waf if c['id'].startswith(prefix)];x=.72+i*2.424
            box(s,x,2.32,2.264,.52,'light');write(s,label+' · '+str(len(selected)),x+.08,2.38,2.104,.36,13.5,True)
            box(s,x,2.90,2.264,3.02,'panel')
            def _waf_short(q):
                import re as _wre
                s0=q.split('. ')[0].rstrip('.')
                s0=_wre.sub(r',\s*according to\b.*$','',s0)
                s0=_wre.sub(r',\s*including\b.*$','',s0)
                s0=_wre.sub(r',\s*alerts,\s*and\s*model\s*retraining\b.*$','',s0)
                s0=_wre.sub(r'\s*\([^()]*\)','',s0)
                s0=_wre.sub(r'\s+that\s+access\s+the\s+system\b.*$','',s0)
                s0=_wre.sub(r'\s+by\s+(?:doing|using|implementing|enforcing)\b.*$','',s0)
                return s0.strip()+'.'
            values='\n'.join(c['id'].rsplit('-',1)[-1]+' · '+_waf_short(c['quote']) for c in selected)
            ls=wrapped_lines(values or '이번 기준 원장에 없음',2.10,10.0)[:17]
            sh=text(s,'\n'.join(ls),x+.08,2.94,2.10,2.94,10.0)
            from pptx.util import Pt as _Pt
            for p in sh.text_frame.paragraphs:p.space_after=_Pt(0);p.line_spacing=1.0
            note(s,{'family':'framework_waf','principles':waf,'interpretation':'공식 점수·인증이 아님'})
        box(s,.72,6.00,11.96,.50,'light');write(s,'시사점 · 모델·데이터·추론 경로의 위험은 일반 인프라 설정만으로 판단할 수 없다.',.94,6.08,11.52,.33,11.2,True)
        ailz=[c for c in data['criteria'] if c['id'].startswith('AILZ-')]
        groups=Counter(metadata.get(c['id'],{}).get('framework','').rsplit('(',1)[-1].rstrip(')') for c in ailz)
        s=clone(4,'framework_landing_zone','AI 기반은 설계 영역별 요구와 현재 구성의 차이로 판단한다','WAF AI·CAF AI·AI Landing Zones의 원칙, 보안 보조 기준, 자체 해석을 분리')
        for i,(label,count) in enumerate(groups.items()):
            y=2.32+i*.355;text(s,label,.72,y,2.40,.30,10.4)
            width=3.00*count/max(groups.values(),default=1);box(s,3.15,y+.06,width,.20,'light');text(s,str(count),3.25+width,y,.40,.30,10.4,True,'primary')
        framework_groups=[('WAF AI','워크로드의 안정성·보안·비용·운영·성능'),('CAF AI','업무 전략·준비·거버넌스·운영·확산'),('AI Landing Zones','AI 기반의 구성·네트워크·ID·관측 설계'),('보안 보조 기준','MCSB와 관측 통제는 공식 인증과 구분')]
        for i,(label,detail) in enumerate(framework_groups):
            y=2.32+i*.86;box(s,7.0,y,5.68,.74,'panel');write(s,label+'\n'+detail,7.14,y+.08,5.40,.58,10.4,True)
        box(s,.72,6.15,11.96,.45,'light');write(s,'원칙 수와 구성 관측 수는 충족률이 아니다. 적용 조건·현재 근거·판단 한계를 별도로 대조한다.',.94,6.22,11.52,.31,11.2,True)
        note(s,{'family':'framework_landing_zone','criteria':ailz,'group_counts':dict(groups),'official_score':None})
        raw_scope=ctx.get('topology_scope')
        scope_str=', '.join(f"{r.get('name')} ({r.get('id')})" for r in raw_scope if isinstance(r,dict)) if isinstance(raw_scope,list) else str(raw_scope)
        block_pages('분석 범위·수집 시점·한계',[
            ('대상',ctx['customer']+' / '+scope_str),
            ('수집 시점',str(ctx.get('topology_collected_at'))+' / '+str(ctx.get('evidence_collected_at'))),
            ('판단 경계',' · '.join(ctx.get('analysis_limits',[])) if integrated else '\n'.join(ctx.get('analysis_limits',[]))),
            ('수집 한계',' · '.join([consolidated.concise_headline(x,95) for x in ctx.get('known_limitations',[])[:2]]) if integrated else '\n'.join(ctx.get('known_limitations',[]))),
            ('고객 목표',('인터뷰 응답 '+str(len(data['goals']))+'건 결합 분석(전체 원문은 동일 개정 Excel 보존)' if integrated and data['goals'] else '\n'.join(g['quote'] for g in data['goals'])) or '현재 고객의 업무 목표·우선순위·품질·효과 인터뷰는 미확인')],'scope',2)
        if integrated:
            from asis_goal_policy import draw_goal_policy_slide
            draw_goal_policy_slide(deck, donors, manifest, result)
        from asis_report_visuals import architecture_pages, synthesis_overviews, action_overviews
        architecture_pages(data, clone, note, line)
        s=clone(6,'inventory','자원 구성과 기준별 판단 범위는 서로 다른 원장으로 대조한다','리소스 수는 구성 범위이며 기준 수·점수·업무 성과가 아니다')
        inventory=ctx.get('inventory_summary',[])
        inventory_pages=[inventory[i:i+12] for i in range(0,len(inventory),12)] or [[]]
        assessments=ctx.get('criterion_assessments',[])
        for page_index,rows in enumerate(inventory_pages):
            if page_index:s=clone(6,'inventory','수집 자원 유형별 구성 · 계속','같은 ID의 리소스는 자원 인벤토리에서 한 번만 집계')
            text(s,'수집 인벤토리',.72,2.30,5.60,.30,13.5,True,'primary')
            for i,row in enumerate(rows):
                x=.72+(i%2)*2.9;y=2.82+(i//2)*.49
                text(s,str(row['count']),x,y,.65,.32,20,True,'primary')
                label=row['type'].rsplit('/',1)[-1]
                ls=wrapped_lines(label,2.12,10);text(s,'\n'.join(ls),x+.70,y,2.12,.44,10)
            text(s,'분석 영역별 기준 대조',6.75,2.30,5.90,.30,13.5,True,'primary')
            for i,(key,label) in enumerate(AREA_NAMES.items()):
                selected=[r for r in assessments if key in r['area_refs']]
                counts=Counter(r['status'] for r in selected);y=2.82+i*.32
                text(s,label,6.75,y,1.50,.23,10.4)
                width=1.45*len(selected)/max(1,len(assessments));box(s,8.30,y+.03,max(.04,width),.16,'light')
                text(s,str(len(selected))+' · 구성 일부 '+str(counts.get('partial',0))+' · 미확인 '+str(counts.get('unknown',0)),9.85,y,2.80,.23,10)
            box(s,.72,6.12,11.96,.50,'light');write(s,'시사점 · 관측된 자원·설정과 실제 통제 효과·업무 성과를 분리하여 개선 대상을 결정한다.',.94,6.20,11.52,.33,11.2,True)
            note(s,{'family':'inventory','inventory_rows':rows,'criterion_assessments':assessments,'run':data['run']})
    # 본문은 승인된 왼쪽 구성·오른쪽 Fact Finding·하단 4단계 판단을 실제 출력에 유지한다.
    from math import floor
    from pptx.util import Pt
    def chunks(value,width,pt,height):
        lines=wrapped_lines(value,width,pt)
        capacity=max(1,floor(height/(pt/72*1.18)))
        return ['\n'.join(lines[i:i+capacity]) for i in range(0,len(lines),capacity)] or ['']
    def fitted_text(s,value,x,y,w,h,pt=10.4,bold=False,color='ink',name=None):
        shape=text(s,consolidated.to_polite_korean(value),x,y,w,h,pt,bold,color)
        for paragraph in shape.text_frame.paragraphs:
            paragraph.space_after=Pt(0);paragraph.line_spacing=1.05
        if name:shape.name=name
        return shape
    def _area_card_text(value,limit=70,prefer_tail=False,role='fact'):
        import re as _re
        cleaned=consolidated._clean_no_paren(value) if hasattr(consolidated,'_clean_no_paren') else str(value).replace('`','').strip()
        if len(cleaned)<=limit:return consolidated.to_polite_korean(cleaned)
        sents=consolidated._complete_sentences(cleaned) if hasattr(consolidated,'_complete_sentences') else [_re.sub(r'^(첫째|둘째|셋째|넷째|다섯째),\s*','',s.strip())+'다.' for s in _re.split(r'다\.(?=\s|$)',cleaned) if s.strip()]
        for target in (list(reversed(sents)) if prefer_tail else sents):
            t_pol=consolidated.to_polite_korean(target)
            if len(t_pol)<=limit:return t_pol
            t_np=consolidated.to_polite_korean(consolidated._clean_no_paren(target) if hasattr(consolidated,'_clean_no_paren') else target)
            if len(t_np)<=limit:return t_np
            if not prefer_tail and '유형별로는 ' in target:
                head,rest=target.split('유형별로는 ',1)
                items=[x.strip() for x in rest.split(',') if x.strip()]
                if len(items)>3:
                    ending=items[-1].split(' ')[-1] if ' ' in items[-1] else '확인됩니다.'
                    cand=consolidated.to_polite_korean((head.strip()+' 유형별로는 '+', '.join(items[:3])+' 등이 '+ending).strip())
                    if len(cand)<=limit:return cand
            if not prefer_tail and '확인한 결과, ' in target:
                _,rest=target.split('확인한 결과, ',1)
                r_pol=consolidated.to_polite_korean(rest)
                if len(r_pol)<=limit:return r_pol
        if hasattr(consolidated,'_compact_complete_sentence'):
            return consolidated.to_polite_korean(consolidated._compact_complete_sentence(sents[-1] if (prefer_tail and sents) else (sents[0] if sents else cleaned),limit))
        return consolidated.to_polite_korean(consolidated.concise_headline(cleaned,limit))
    dirs_primary_by_area={}
    dirs_any_by_area={}
    if integrated and len(stages)>1 and isinstance(stages[1],dict):
        for d_row in stages[1].get('directions',[]):
            irefs=d_row.get('area_implication_refs',[])
            if irefs:dirs_primary_by_area.setdefault(irefs[0].replace('I_',''),[]).append(d_row)
            for ir in irefs:dirs_any_by_area.setdefault(ir.replace('I_',''),[]).append(d_row)
    for area in stages[0]['areas']:
        if section not in (None,'area-'+area['area_id']):continue
        key=area['area_id'];nodes=native_diagram_rows(area,data);implication=area['implication']
        if integrated and area['observations']:
            obs0=area['observations'][0]
            observation_blocks=[(obs0['id'],_area_card_text(obs0['text'],265,False))]
            observations=[observation_blocks]
        else:
            observation_blocks=[(o['id'],o['text']) for o in area['observations']]
            observations=paragraphs_pages(observation_blocks,width=6.13,pt=11.2,max_lines=9) or [[]]
        nodegroups=[nodes[i:i+4] for i in range(0,len(nodes),4)] or [[]]
        if integrated:
            refs=implication['criterion_refs']
            crit_head=' · '.join(refs[:3])+(' 외 '+str(len(refs)-3)+'건' if len(refs)>3 else '')
            criteria_text=crit_head+'\n원칙 적용 조건·현재 구성 구분'
            hl=area['headline']
            for hsep,c_end in (('기반은 일부 확보되어 있으나, ','기반이 일부 확보되어 있다.'),('기반은 확보되어 있으나, ','기반이 확보되어 있다.'),
                               ('가시성은 일부 확보되었으나, ','가시성이 일부 확보되어 있다.'),('구성은 확보되어 있으나, ','구성이 확보되어 있다.'),
                               ('기반은 갖추어져 있으나, ','기반이 갖추어져 있다.'),('적용되어 있으나, ','적용되어 있다.'),
                               ('운영 중이나, ','운영 중이다.'),('존재하나 ','존재한다.'),('있으나, ','확보되어 있다.')):
                if hsep in hl:
                    p0,p1=hl.split(hsep,1);curr_src=p0.strip()+' '+c_end;gap_src=p1.strip();break
            else:
                curr_src,gap_src=area['observations'][0]['text'],implication['text']
            area_dirs=dirs_primary_by_area.get(key) or dirs_any_by_area.get(key) or []
            rec_text=consolidated._dir_title_card(area_dirs[0]['title'],64) if (area_dirs and hasattr(consolidated,'_dir_title_card')) else _area_card_text(implication['change_direction'],64,True,'rec')
            imp_sents=consolidated._complete_sentences(consolidated._clean_no_paren(implication['impact'])) if hasattr(consolidated,'_clean_no_paren') else [implication['impact']]
            imp_text=next((s for s in imp_sents if len(s)<=136),None)
            if not imp_text:
                for s_imp in imp_sents:
                    for m_imp in ('있으나 ','과정에서, ','통해 ','경우, ','반면 '):
                        if m_imp in s_imp:
                            t_imp=s_imp.split(m_imp,1)[1].strip()
                            if 25<=len(t_imp)<=136:imp_text=t_imp;break
                    if imp_text:break
            if not imp_text:imp_text=_area_card_text(implication['impact'],136,False,'imp')
            chain=[criteria_text,_area_card_text(curr_src,64,False,'curr'),_area_card_text(gap_src,64,False,'gap'),rec_text]
            chain_pages=[[v] for v in chain]
            direction_pages=['시사점 · '+imp_text]
        else:
            criteria_text=' · '.join(implication['criterion_refs'])+'\n공식 원칙의 적용 조건과 현재 구성·실제 효과를 구분'
            chain=[criteria_text,'\n'.join(o['text'] for o in area['observations']),implication['text'],implication['change_direction']]
            chain_pages=[chunks(value,2.585,10.4,.64) for value in chain]
            direction_pages=chunks('시사점 · '+implication['change_direction'],11.52,11.2,.38)
        total=1 if integrated else max(len(observations),len(nodegroups),len(direction_pages),*(len(p) for p in chain_pages))
        for index in range(total):
            donors_by_area={'network':7,'compute':8,'data':9,'ai':10,'secgov':11,'telemetry':12,'quality':12,'operating':14,'cost':13}
            slide_title=_area_card_text(gap_src,52,False,'title') if integrated else area['headline']+(' · 계속' if index else '')
            s=clone(donors_by_area[key],'area_fact',slide_title,AREA_NAMES[key]+' · 관측 사실과 적용 판단을 구분')
            diagram=box(s,.72,2.30,5.60,2.20,'panel');diagram.name='approved_area_diagram:'+key
            text(s,'현재 구성·관측 대상',.86,2.35,5.30,.24,10,True,'primary')
            group=nodegroups[min(index,len(nodegroups)-1)]
            for j,(label,detail,dashed) in enumerate(group):
                x=.86+(j%2)*2.66;y=2.78+(j//2)*.76
                box(s,x,y,2.50,.66,'white',dashed)
                clean_lbl=str(label).strip()
                if len(clean_lbl)>24:clean_lbl=clean_lbl.replace('ManagementSubnet','MgmtSubnet').replace('Management','Mgmt')
                clean_dtl=str(detail).replace('NSG 참조 값 없음 · UDR 참조 값 없음','NSG·UDR 참조 값 없음').rstrip(' ·').strip()
                if len(clean_dtl)>22 and ' · ' in clean_dtl:clean_dtl=clean_dtl.split(' · ')[0].strip()
                fitted_text(s,clean_lbl[:24],x+.08,y+.04,2.34,.28,10,True)
                fitted_text(s,clean_dtl[:24],x+.08,y+.35,2.34,.26,10)
            if index<len(observations):
                text(s,'Fact Finding',6.55,2.30,6.13,.30,13.5,True,'primary');y=2.72
                for obs_idx,(identifier,value) in enumerate(observations[index],1):
                    hdr_label=('핵심 관측 사실' if len(observations[index])==1 else f'관측 사실 {obs_idx}') if integrated else identifier
                    fitted_text(s,hdr_label,6.55,y,6.13,.20,10,True,'primary','approved_fact_panel:'+identifier);y+=.23
                    ls=wrapped_lines(value,6.13,11.2);h=max(.20,len(ls)*.19)
                    fitted_text(s,'\n'.join(ls),6.55,y,6.13,h,11.2);y+=h+.08
            else:
                fitted_text(s,'앞선 동일 영역 장표의 관측 근거를 유지하며 적용 판단을 이어서 설명한다.',6.55,2.72,6.13,.52,11.2,name='approved_fact_panel:continuation')
            for i,label in enumerate(('기준','현재','부족한 점','권고')):
                x=.72+i*3.045
                cell=box(s,x,4.98,2.825,1.04,'light' if i in (0,3) else 'panel');cell.name='approved_judgment_cell:'+label
                fitted_text(s,label,x+.10,5.02,2.625,.24,11.0,True,'primary')
                value=chain_pages[i][index] if index<len(chain_pages[i]) else '앞선 동일 영역 장표의 '+label+' 판단 유지'
                ls_cell=wrapped_lines(value,2.625,10.0)
                fitted_text(s,'\n'.join(ls_cell),x+.10,5.26,2.625,.72,10.0)
                if i<3:line(s,(x+2.845,5.49),(x+3.025,5.49),'judgment-'+key+'-'+str(i))
            box(s,.72,6.12,11.96,.50,'light')
            value=direction_pages[index] if index<len(direction_pages) else '시사점 '+implication['id']+' · 앞선 판단과 개선 방향 유지'
            fitted_text(s,value,.94,6.18,11.52,.38,10.8,True,'primary')
            refs_footer=implication['criterion_refs'][:3]+(['외 '+str(len(implication['criterion_refs'])-3)+'건'] if integrated and len(implication['criterion_refs'])>3 else implication['criterion_refs'][3:])
            obs_list=[o['id'] for o in area['observations']]
            obs_ids=(' · '.join(obs_list[:4])+' 외 '+str(len(obs_list)-4)+'건') if integrated and len(obs_list)>4 else ' · '.join(obs_list)
            text(s,'근거: '+' · '.join(refs_footer)+(' · '+implication['id']+' · '+obs_ids if integrated else ''),.72,6.72,9.20,.24,10,False,'muted')
            note(s,{'family':'area_fact','area':area,'run':data['run'],'continuation':index+1,'pages':total,'visible_nodes':group})
        if not integrated:
            criterion_index={c['id']:c for c in data['criteria']}
            official=[('Microsoft 공식 원문 · '+cid,criterion_index[cid]['quote']+'\n권고 강도: '+criterion_index[cid]['modality']+'\n'+criterion_index[cid]['source_url']) for cid in implication['criterion_refs']]
            block_pages(AREA_NAMES[key]+' · 시사점과 적용 판단',[(implication['id'],implication['text']),('고객 영향',implication['impact']),('변경·추가·유지·확인 방향',implication['change_direction']),('분석상 적용 해설','구성·관측·원칙의 적용 조건은 별도이며 아래 공식 원문과 권고 강도를 보존한다.')]+official,'area_detail',donors_by_area[key])
    if integrated and section in (None,'synthesis'):
        from asis_maturity_spider import draw_maturity_spider_slide
        draw_maturity_spider_slide(clone,note,text,box,wrapped_lines,line,result)
    if section in (None,'synthesis'):
        # 시사점 종합 1장: 영역마다 한 줄. 영역별 진단·시사점 합본 장표가 상세를 맡는다(이슈·방향·관찰 단위 장표를 만들지 않는다).
        import asis_consolidated_deck as consolidated
        implications={a['implication']['id']:{**a['implication'],'headline':a['headline']} for a in stages[0]['areas']}
        by_area=consolidated.topics_by_area(result)
        counts={area:len(rows) for area,rows in by_area.items()}
        s=clone(15,'fact_synthesis_overview','영역별 진단 결과에서 도출된 시사점을 종합한다','분석 영역 → 진단 주제 수 → 영역 시사점 · 상세는 영역별 진단·시사점 장표')
        rows=consolidated.insight_synthesis_blocks({a['area_id']:implications[a['implication']['id']] for a in stages[0]['areas']},counts)
        for index,row in enumerate(rows):
            y=2.28+index*.47
            box(s,.72,y,1.90,.43,'panel');write(s,AREA_NAMES[row['area_id']],.82,y+.04,1.70,.35,10.4,True)
            box(s,2.70,y,8.60,.43,'white');write(s,row['headline'],2.80,y+.04,8.40,.35,10.4)
            box(s,11.38,y,1.30,.43,'light');write(s,'주제 '+str(row['topics'])+'개',11.46,y+.04,1.14,.35,10.4,True)
            line(s,(2.62,y+.21),(2.68,y+.21),'insight-'+row['area_id'])
        note(s,{'family':'fact_synthesis_overview','rows':rows,'run':data['run']})
        # 영역별 진단 결과 → 도출된 시사점 합본: 진단 주제마다 한 장
        directions_by_id={d['id']:d for d in stages[1]['directions']}
        diagnoses_by_id={d['id']:d for d in stages[2]['diagnoses']}
        page_plan={}
        for area_id,topics in by_area.items():
            pages=consolidated.area_topic_pages(area_id,topics,diagnoses_by_id,implications,directions_by_id,visuals=result.get('visual_specs'))
            page_plan[area_id]=pages
        consolidated.check_page_limits(page_plan,len(stages[2]['actions']))
        v_facts=consolidated.visual_facts_of(data)
        for area_id,pages in page_plan.items():
            for page in pages:
                nodes,edges=consolidated.overview_for(data) if page['visual']['form']=='architecture' else ((),())
                consolidated.draw_area_topic(clone,note,text,box,wrapped_lines,AREA_NAMES[area_id],page,len(pages),line,nodes=nodes,edges=edges,facts=v_facts)
    if section in (None,'actions'):
        # 과제 도출 1장 + 과제마다 구현방안 오버뷰 1장
        import asis_consolidated_deck as consolidated
        by_area=consolidated.topics_by_area(result)
        topic_of_diagnosis={t['id']:t for rows in by_area.values() for t in rows}
        derived=consolidated.action_derivation_blocks(consolidated.actions_with_areas(result,by_area))
        s=clone(16,'diagnosis_action_overview','진단 주제에서 도출된 개선과제를 실행 단위로 묶는다','과제 · 연결 영역 · 연결 진단 주제 수 · 상세는 과제별 구현방안 장표')
        for index,row in enumerate(derived):
            y=2.22+index*.205
            text(s,row['id'],.72,y,.95,.19,10,True,'primary');text(s,consolidated.concise_action_title(row['title'],47),1.72,y,6.68,.19,10)
            text(s,' · '.join(AREA_NAMES[a] for a in row['areas'][:2])+(' 외 '+str(len(row['areas'])-2)+'개' if len(row['areas'])>2 else ''),8.48,y,3.12,.19,10)
            text(s,'진단 '+str(row['topics']),11.70,y,.98,.19,10,True,'primary',PP_ALIGN.RIGHT)
        note(s,{'family':'diagnosis_action_overview','rows':derived,'run':data['run']})
        for action in stages[2]['actions']:
            consolidated.draw_action_overview(clone,note,text,box,wrapped_lines,action,topic_of_diagnosis,stages[2]['diagnoses'],AREA_NAMES,connect=line)
    if section in (None,'appendix') and not integrated:
        block_pages('미확인과 결론의 제한',[(u['topic'],u['reason']+'\n'+u['required_evidence']) for u in result['unresolved']],'limitations',17)
        # 명시 구성 관계는 누락 없이 별첨 네이티브 행과 원문 Notes로 보존한다.
        from asis_artifacts import configuration_relationships
        for area_key in ('network','ai'):
            graph=configuration_relationships(data,area_key)
            nodes={n['id'].casefold():n for n in graph['nodes']}
            for edge in graph['edges']:
                left=nodes[edge['from_id'].casefold()];right=nodes[edge['to_id'].casefold()]
                s=clone(7,'configuration',AREA_NAMES[area_key]+' · 수집 구성관계 상세','구성 참조 · 실제 통신 검증 아님')
                for node,x,w in ((left,.72,5.25),(right,6.68,6.00)):
                    ls=wrapped_lines(node['label'],w-.24,10.4);h=max(.8,len(ls)*.2+.4)
                    box(s,x,2.9,w,h,'panel',not node['collected'])
                    text(s,'\n'.join(ls),x+.12,3.0,w-.24,len(ls)*.2,10.4,True)
                    text(s,node.get('detail',''),x+.12,3.1+len(ls)*.2,w-.24,.25,10.0)
                line(s,(5.98,3.25),(6.62,3.25),edge['id'])
                connector=next(sh for sh in s.shapes if sh.name=='approved_relation:'+edge['id'])
                connector.name='configuration_edge:'+edge['id']+':0'
                overview=s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,Inches(5.98),Inches(3.5),Inches(6.62),Inches(3.5))
                overview.name=('area_configuration_edge:' if area_key=='network' else 'ai_configuration_edge:')+edge['id']
                note(s,{**graph,'edges':[edge]})
        # 직접 사실→종합 관계도 관찰 존재와 무관하게 정본 선언을 보존한다.
        for action in stages[2]['actions']:
            direction_ids=set(action['direction_refs'])
            related=[d for d in stages[1]['directions'] if d['id'] in direction_ids]
            edges=[(f,d['id'],False) for d in related for f in d['fact_refs']]
            edges.extend((i,d['id'],False) for d in related for i in d['area_implication_refs'])
            edges.extend((j,action['id'],False) for j in action['diagnosis_refs'])
            s=clone(16,'trace',action['id']+' · 근거 관계 원장','본문 설명의 안정적 ID와 직접 선언 관계 · 전체 내용은 동일 개정 Excel')
            observations=[o for area in stages[0]['areas'] for o in area['observations'] if set(o['fact_refs']) & set(action['fact_refs'])]
            edges.extend((f,o['id'],False) for o in observations for f in o['fact_refs'] if f in action['fact_refs'])
            identifiers=list(dict.fromkeys(x for e in edges for x in e[:2]))
            text(s,'\n'.join(identifiers),.72,2.4,11.96,3.5,10.0)
            note(s,{'action_id':action['id'],'edges':edges})
            s.notes_slide.notes_text_frame.text='원장 참조(직접 선언 및 공통 Fact 교집합 파선 구분)\n'+json.dumps({'action_id':action['id'],'edges':edges},ensure_ascii=False)
        # 사용한 공식 기준은 본문에 표시하고 전수 기준은 Excel 원장에 보존한다.
        used={c for a in stages[0]['areas'] for c in a['implication']['criterion_refs']}|{c for d in stages[1]['directions'] for c in d['criterion_refs']}
        block_pages('적용 공식 기준',[(c['id'],c['quote']+'\n'+c['source_url']) for c in data['criteria'] if c['id'] in used],'sources',19)
    official_urls={c['source_url'] for c in data['criteria']}
    for page in deck.slides:
        for shape in page.shapes:
            if shape.has_text_frame:
                compact=''.join(shape.text.split())
                url=next((u for u in official_urls if ''.join(u.split()) in compact),None)
                if url:shape.click_action.hyperlink.address=url
    # 전수 기준의 출처 URL도 별첨에서 탐색 가능하게 보존한다.
    if section in (None,'appendix'):
        s=clone(19,'sources','공식 자료','공식 기준의 원문·버전·적용 조건은 동일 개정 Excel에 보존')
        for i,url in enumerate(sorted(official_urls)):
            link=text(s,url,.72,2.30+i*.30,11.96,.25,10)
            link.click_action.hyperlink.address=url
    for i,s in enumerate(deck.slides,1):
        if i<len(deck.slides):
            link=text(s,'다음 상세',10.1,6.78,1.6,.22,9,False,'muted')
            link.click_action.target_slide=deck.slides[i]
        text(s,f'{i:02d} / {len(deck.slides):02d}',11.75,7.11,.96,.22,9,False,'muted',PP_ALIGN.RIGHT)
    deck.core_properties.subject=data['run']['source_bundle_sha256'];deck.core_properties.title='AI Assessment · '+report_name if integrated else 'AI Assessment · AS-IS 현황 분석'
    # 제거된 donor의 미디어가 관계로 재연결될 때 imageN 번호가 새 이미지와 충돌할 수 있다.
    # 관계는 Part 객체를 참조하므로 저장 전에 이미지 파트명만 고유하게 재배정한다.
    from pptx.opc.packuri import PackURI
    media=[part for part in deck.part.package.iter_parts() if str(part.partname).startswith('/ppt/media/')]
    for index,part in enumerate(media,1):
        suffix=Path(str(part.partname)).suffix
        part.partname=PackURI('/ppt/media/approved-media-'+str(index)+suffix)
    deck.save(destination)
    with zipfile.ZipFile(destination) as z:
        if z.testzip():raise ValueError('approved_ppt_zip_failed')
    Path(destination).with_suffix('.layout.json').write_text(json.dumps({'content_template_sha256':CONTENT_HASH,'visual_template_sha256':TEMPLATE_HASH,
        'pages':[{k:v for k,v in r.items() if k!='slide'} for r in manifest],'actual_render_verified':False},ensure_ascii=False,indent=2),encoding='utf-8')
    return len(deck.slides)
