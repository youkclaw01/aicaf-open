"""실모델 품질 검증이 아닌, 호출 계약·복구 경로 전용 합성 대역이다."""

def answer(payload):
    if payload.get('task')=='current_state_editorial_review':
        return {'verdict':'pass','content_checked':True,'render_checked':False,'defects':[]}
    if payload.get('task')!='current_state_editorial':return None
    area=payload['area_source'];rows=area['observations'];groups=[]
    if rows:
        groups=[{'title':'관측 현황과 판단 한계를 구분한다','current_state':'수집된 구성의 존재를 확인했다.',
            'technical_meaning':'실제 사용과 운영 효과는 별도 확인이 필요하다.','limitation':'합성 단위 시험의 표현이며 고객 사실이 아니다.',
            'observation_refs':[rows[0]['id']],
            'source_quotes':[{'observation_ref':rows[0]['id'],'quote':rows[0]['text'][:120]}]}]
    return {'area_id':area['area_id'],'headline':'관측 현황의 범위에서만 판단한다.','groups':groups,
        'appendix_only':[{'observation_ref':row['id'],'reason':'합성 단위 시험의 상세 원장 보존 경로를 검사한다.'} for row in rows[1:]]}
