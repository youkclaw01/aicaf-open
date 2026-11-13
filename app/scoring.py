"""점수 계산 — 인터뷰 엑셀 '영역 점수'·'종합 결과' 시트와 같은 규칙."""
VALID = {"0", "1", "2", "3", "4", "미확인", "해당없음"}
STAGES = [
    ("Exploring", "탐색", 0, 29),
    ("Planning", "계획", 30, 49),
    ("Implementing", "실증", 50, 69),
    ("Scaling", "확산", 70, 84),
    ("Realizing", "가치 실현", 85, 100),
]


def _avg(xs):
    return round(sum(xs) / len(xs), 1) if xs else None


def compute(questions, answers):
    topics, areas = {}, {}
    unknown = na = answered = must_low = 0
    for q in questions:
        a = answers.get(q["id"]) or {}
        s = a.get("score")
        t = topics.setdefault(q["topic"], {"area": q["area"], "name": q["topic_name"], "scores": []})
        if s is None or s == "":
            continue
        answered += 1
        if s == "미확인":
            unknown += 1
        elif s == "해당없음":
            na += 1
        else:
            v = int(s)
            t["scores"].append(v)
            if q["must"] and v <= 1:
                must_low += 1
    for code, t in topics.items():
        t["score"] = round(sum(t["scores"]) / len(t["scores"]) / 4 * 100, 1) if t["scores"] else None
        areas.setdefault(t["area"], []).append(t["score"])
    area_score = {k: _avg([x for x in v if x is not None]) for k, v in areas.items()}
    total_q = len(questions)
    denom = answered - na
    unknown_ratio = round(unknown / denom, 3) if denom > 0 else 0.0
    vals = [area_score.get(k) for k in "ABCD"]
    overall = _avg(vals) if all(v is not None for v in vals) else None
    stage, reason = None, ""
    if answered < total_q:
        reason = f"인터뷰 미완료({answered}/{total_q})"
    elif unknown_ratio > 0.2:
        reason = "판정 보류(미확인 20% 초과)"
    elif overall is None:
        reason = "점수 부족"
    else:
        A, B, C, D = vals
        if overall >= 85 and D >= 80 and min(vals) >= 70 and must_low == 0:
            stage = "Realizing"
        elif overall >= 70 and B >= 70 and C >= 70 and must_low == 0:
            stage = "Scaling"
        elif overall >= 50 and B >= 50 and C >= 40 and must_low == 0:
            stage = "Implementing"
        elif overall >= 30 and A >= 50:
            stage = "Planning"
        else:
            stage = "Exploring"
        reason = "점수 구간과 영역 조건을 모두 만족하는 가장 높은 단계"
        if must_low:
            reason += f" · 필수 문항 1점 이하 {must_low}개로 최대 Planning"
    return {"topics": {k: {"area": v["area"], "name": v["name"], "score": v["score"]} for k, v in topics.items()},
            "areas": area_score, "overall": overall, "answered": answered, "total": total_q,
            "unknown": unknown, "na": na, "unknown_ratio": unknown_ratio, "must_low": must_low,
            "stage": stage, "reason": reason}
