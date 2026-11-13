"""비용 v2 계약 테스트. 숫자가 있는 픽스처는 실제 고객 데이터가 아닌 명시적 단위시험 입력이다."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cost_calc


def evidence(sections=None):
    return {
        "schema": "aicaf-evidence/v1", "subscriptionId": "test-subscription",
        "completedAtUtc": "2026-10-08T12:00:00Z",
        "costPeriod": {"fromUtc": "2026-09-01T00:00:00Z", "toUtc": "2026-09-30T23:59:59Z"},
        "sections": sections or {},
    }


def collected(records):
    return {"status": "collected", "api": "unit-test-fixture", "records": records}


def price_reference(ev, index):
    import cost_finops
    return cost_finops.price_catalogue(ev)[index]["ref"]


def analyze(ev, **options):
    fn = getattr(cost_calc, "compute_finops", None)
    assert callable(fn), "비용 v2 단일 계산 진입점 compute_finops가 아직 없다"
    import cost_finops
    # 시험 조치를 생성할 때 현재 픽스처를 명시적으로 바인딩하며 stale 값은 보존한다.
    for action in options.get("actions") or []:
        action.setdefault("evidence_snapshot", cost_finops.digest(ev))
    return fn(ev, "existing", options)


def test_actual_and_amortized_are_never_summed():
    ev = evidence({"costMonthlyTotals": collected([
        {"Cost": 10, "Currency": "USD", "costType": "ActualCost", "month": "2026-09"},
        {"Cost": 8, "Currency": "USD", "costType": "AmortizedCost", "month": "2026-09"},
    ])})
    a = analyze(ev, dataset="monthly", basis="ActualCost", currency="USD")
    assert a["summary"]["total"]["value"] == 10
    b = analyze(ev, dataset="monthly", basis="AmortizedCost", currency="USD")
    assert b["summary"]["total"]["value"] == 8


def test_permission_denied_amount_is_not_zero():
    ev = evidence({"costMonthlyTotals": {"status": "permission_denied", "reason": "Cost Management Reader required", "records": []}})
    a = analyze(ev, dataset="monthly", currency="USD")
    assert a["summary"]["total"]["value"] is None, "수집 불가 비용을 0으로 만들면 안 된다"
    assert a["summary"]["total"]["status"] == "permission_denied"


def test_resource_drilldown_uses_exact_inventory_join():
    rid = "/subscriptions/test-subscription/resourceGroups/RG-A/providers/Microsoft.Compute/virtualMachines/VM-A"
    ev = evidence({"costByResource": collected([
        {"Cost": 12.5, "Currency": "USD", "ResourceId": rid, "ServiceName": "Virtual Machines", "Meter": "test-meter"},
        {"Cost": 7.5, "Currency": "USD", "ResourceId": rid.replace("VM-A", "VM-B"), "ServiceName": "Virtual Machines", "Meter": "test-meter"},
    ]), "resourceTags": collected([{"id": rid, "tags": {"workload": "test-fixture"}}])})
    tp = {"resources": [{"id": rid.lower(), "name": "VM-A", "location": "koreacentral", "sku": {"name": "test-sku"}}]}
    a = cost_calc.compute_finops(ev, "existing", {"dataset": "resource", "basis": "ActualCost", "currency": "USD", "filters": {"region": "koreacentral"}}, tp)
    assert a["summary"]["total"]["value"] == 12.5, "필터는 선택한 grain의 실제 원장에만 적용해야 한다"
    assert len(a["rows"]) == 1
    r = a["rows"][0]
    assert r["region"] == "koreacentral" and r["resource_group"] == "rg-a"
    assert r["dimension_sources"]["region"] == "topology.resources.location"
    assert r["tags"]["workload"] == "test-fixture"


def test_source_grains_are_preserved_for_each_drilldown():
    cases = [
        ("service", "costByService", {"ServiceName": "Storage", "month": "2026-09"}, "service", "Storage", "ActualCost"),
        ("resource_group", "costByResourceGroup", {"ResourceGroupName": "RG-A", "month": "2026-09"}, "resource_group", "rg-a", "ActualCost"),
        ("tag", "costByTag", {"tagKey": "team", "TagValue": "A"}, "tag_value", "A", "ActualCost"),
        ("pricing", "costByPricingModel", {"PricingModel": "Reservation", "ChargeType": "Usage", "month": "2026-09"}, "pricing_model", "Reservation", "AmortizedCost"),
        ("ai_daily", "aiCostDaily", {"UsageDate": 20260907, "ResourceId": "/subscriptions/test-subscription/providers/Microsoft.CognitiveServices/accounts/AI-A", "Meter": "test Inp"}, "date", "2026-09-07", "ActualCost"),
    ]
    import cost_finops
    for name, sec, dims, dimension, expected, basis in cases:
        assert name in cost_finops.DATASETS, f"{name} 수집 grain의 원장 조회가 필요하다"
        ev = evidence({sec: collected([{**dims, "Cost": 13.2, "Currency": "USD"}])})
        a = analyze(ev, dataset=name, basis=basis, currency="USD")
        assert a["summary"]["total"]["value"] == 13.2
        assert a["rows"][0][dimension] == expected


def test_partial_period_for_undated_resource_is_rejected():
    import pytest
    ev = evidence({"costByResource": collected([{ "Cost": 20, "Currency": "USD", "ResourceId": "/subscriptions/test-subscription/providers/x/y"}])})
    with pytest.raises(ValueError, match="일별|전체기간"):
        analyze(ev, dataset="resource", currency="USD", start="2026-09-10", end="2026-09-30")


def test_tag_keys_never_form_one_additive_total():
    ev = evidence({"costByTag": collected([
        {"Cost": 10, "Currency": "USD", "tagKey": "team", "TagValue": "A"},
        {"Cost": 10, "Currency": "USD", "tagKey": "env", "TagValue": "prod"},
    ])})
    a = analyze(ev, dataset="tag", currency="USD", tag_key="team")
    assert a["summary"]["total"]["value"] == 10
    assert all(r["tag_key"] == "team" for r in a["rows"])


def test_period_filter_applies_to_monthly_source_rows():
    ev = evidence({"costByService": collected([
        {"Cost": 5, "Currency": "USD", "ServiceName": "Storage", "month": "2026-08"},
        {"Cost": 8, "Currency": "USD", "ServiceName": "Storage", "month": "2026-09"},
    ])})
    a = analyze(ev, dataset="service", currency="USD", start="2026-09-01", end="2026-09-30")
    assert a["summary"]["total"]["value"] == 8
    assert a["groups"][0]["value"] == 8
    assert len(a["rows"]) == 1


def test_unknown_filter_dimension_is_rejected_not_empty_zero():
    import pytest
    with pytest.raises(ValueError, match="교차|차원"):
        analyze(evidence(), dataset="monthly", filters={"region": "koreacentral"})


def test_incomplete_amount_yields_partial_subtotal_not_full_total():
    ev = evidence({"costMonthlyTotals": collected([
        {"Cost": 0, "Currency": "USD", "costType": "ActualCost", "month": "2026-08"},
        {"Currency": "USD", "costType": "ActualCost", "month": "2026-09"},
    ])})
    ev["costPeriod"]["fromUtc"] = "2026-08-01T00:00:00Z"
    a = analyze(ev, dataset="monthly", currency="USD")
    assert a["summary"]["total"]["value"] is None
    assert a["summary"]["known_subtotal"]["value"] == 0
    assert a["summary"]["total"]["status"] == "partial"


def test_no_automatic_savings_from_legacy_example_rates():
    ev = evidence({"costByResource": collected([{ "Cost": 100, "Currency": "USD", "ResourceId": "/subscriptions/test-subscription/providers/x/y", "Meter": "test"}])})
    a = analyze(ev, dataset="resource", currency="USD")
    assert a.get("comparison", {}).get("savings", {}).get("status") == "not_estimated"
    assert a["comparison"]["savings"]["value"] is None
    assert a["comparison"]["to_be"]["value"] == 100
    assert not a["actions"], "이전 예시 절감률로 실제 TO-BE 조치를 만들면 안 된다"


def test_confirmed_actions_share_residual_budget_without_double_count():
    ev = evidence({"costByResource": collected([{ "Cost": 100, "Currency": "USD", "ResourceId": "/subscriptions/test-subscription/providers/x/y", "Meter": "test"}])})
    action = {"kind": "confirmed_amount", "row_ids": ["resource_0"], "amount": 80, "currency": "USD", "basis": "ActualCost",
              "enabled": True, "scope_confirmed": True, "evidence": "unit-test-fixture (시험 입력)", "phase": "planned"}
    a = analyze(ev, dataset="resource", currency="USD", actions=[{**action, "id": "A", "name": "시험조치 A"}, {**action, "id": "B", "name": "시험조치 B"}],
                recurring_cost=0, recurring_cost_evidence="시험 입력: 추가 운영비 없음")
    assert a["comparison"]["savings"]["value"] == 100
    assert a["comparison"]["to_be"]["value"] == 0
    assert a["actions"][1]["overlap_removed"]["value"] == 60
    assert sum(x["applied"]["value"] for x in a["actions"]) == 100


def test_provenance_capabilities_and_advisor_overlap_are_explicit():
    ev = evidence({"costByResource": collected([{ "Cost": 100, "Currency": "USD", "ResourceId": "/subscriptions/test-subscription/providers/x/y", "Meter": "test"}]),
                   "advisorRecommendations": collected([
                       {"category": "Cost", "id": "advisor-a", "resourceId": "/subscriptions/test-subscription", "problem": "Reservation alternative", "annualSavingsAmount": "120", "savingsCurrency": "USD"},
                       {"category": "Cost", "id": "advisor-b", "resourceId": "/subscriptions/test-subscription", "problem": "Reservation alternative", "annualSavingsAmount": "180", "savingsCurrency": "USD"},
                   ])})
    a = analyze(ev, dataset="resource", currency="USD")
    assert a.get("inputs", {}).get("evidence_sha256")
    assert a.get("datasets", {}).get("resource", {}).get("grain")
    assert len(a.get("candidates", [])) == 2
    assert all(x["status"] == "scope_not_resolved" for x in a["candidates"])
    assert a["comparison"]["savings"]["value"] is None
    assert a.get("forecast", {}).get("status") == "missing"


def test_currency_isolation_and_unknown_exchange_rate():
    ev = evidence({"costMonthlyTotals": collected([
        {"Cost": 10, "Currency": "USD", "costType": "ActualCost", "month": "2026-09"},
        {"Cost": 1000, "Currency": "KRW", "costType": "ActualCost", "month": "2026-09"},
    ])})
    a = analyze(ev, dataset="monthly", currency="KRW")
    assert a["summary"]["total"]["value"] == 1000
    assert a.get("display_fx", {}).get("value") is None
    b = analyze(ev, dataset="monthly", currency="USD", fx={"target_currency": "KRW", "rate": 100, "date": "2026-09-30", "source": "unit-test-fixture"})
    assert b["display_fx"]["value"] == 1000
    assert b["summary"]["total"]["value"] == 10


def test_token_observations_keep_uncollected_cache_unknown():
    ev = evidence({"aiMetricsTokensDaily": collected([
        {"resourceId": "/subscriptions/test-subscription/providers/x/AI-A", "time": "2026-09-02T00:00:00Z", "dimensions": {"modeldeploymentname": "model-a"}, "metric": "ProcessedPromptTokens", "total": 2000},
        {"resourceId": "/subscriptions/test-subscription/providers/x/AI-A", "time": "2026-09-02T00:00:00Z", "dimensions": {"modeldeploymentname": "model-a"}, "metric": "GeneratedTokens", "total": 500},
    ]), "aiDeployments": collected([{"accountId": "/subscriptions/test-subscription/providers/x/AI-A", "name": "model-a", "modelName": "test-model", "skuName": "DataZoneStandard"}])})
    a = analyze(ev, currency="USD")
    assert len(a.get("ai", {}).get("tokens", [])) == 1
    t = a["ai"]["tokens"][0]
    assert t["input_tokens"]["value"] == 2000
    assert t["output_tokens"]["value"] == 500
    assert t["cached_tokens"]["value"] is None
    assert t["requests"]["value"] is None
    assert t["deployment_type"] == "DataZoneStandard"
    assert t["retail_estimate"]["value"] is None


def test_new_customer_never_gets_fabricated_hourly_cost_or_automation():
    qs = [{"id": "A1-1", "area": "A", "topic_name": "업무", "question": "처리 규모"}]
    a = cost_calc.compute_finops(evidence(), "new", {}, None, qs, {"A1-1": {"transcript": "한 건에 5분 정도 걸리고 하루 1,200건쯤 됩니다."}})
    assert a.get("new", {}).get("seed", {}).get("daily_requests") == 1200
    assert a["new"]["hourly_cost"]["value"] is None
    assert a["new"]["roi_months"]["value"] is None
    assert a["new"]["adoption_cost"]["value"] is None


def test_new_roi_uses_explicit_quantities_and_official_price_rows():
    import pytest
    ev = evidence({"costMonthlyTotals": collected([{"Cost": 50, "Currency": "USD", "costType": "ActualCost", "month": "2026-09"}]),
        "pricesModels": collected([
            {"meterName": "test Inp", "retailPrice": 1, "currencyCode": "USD", "unitOfMeasure": "1M Tokens", "type": "Consumption", "tierMinimumUnits": 0},
            {"meterName": "test Opt", "retailPrice": 2, "currencyCode": "USD", "unitOfMeasure": "1M Tokens", "type": "Consumption", "tierMinimumUnits": 0},
        ])})
    opts = {"dataset": "monthly", "currency": "USD", "new": {"baseline_month": "2026-09", "monthly_requests": 100, "input_tokens": 1000, "output_tokens": 100,
            "monthly_work_hours": 20, "hourly_cost": 10, "automation_share": .5, "build_cost": 30, "support_cost": 0, "support_cost_evidence": "unit-test-fixture",
            "price_in_ref": price_reference(ev, 0), "price_out_ref": price_reference(ev, 1)}}
    a = cost_calc.compute_finops(ev, "new", opts)
    expected_ai = 100 * 1000 / 1_000_000 * 1 + 100 * 100 / 1_000_000 * 2
    expected_benefit = 20 * 10 * .5 - expected_ai
    assert a["new"]["adoption_cost"]["value"] == pytest.approx(50 + 20 * 10 * (1-.5) + expected_ai)
    assert a["new"]["net_benefit"]["value"] == pytest.approx(expected_benefit)
    assert a["new"]["roi_months"]["value"] == pytest.approx(30 / expected_benefit)
    assert a["new"]["annual_net_benefit"]["value"] == pytest.approx(expected_benefit * 12)
    nodes = {n["id"]: n for n in a["calculation"]}
    assert nodes["new_adoption_cost"]["kind"] == "formula"


def test_observed_token_repricing_stays_separate_from_actual_bill():
    import pytest
    account = "/subscriptions/test-subscription/providers/x/AI-A"
    ev = evidence({"costMonthlyTotals": collected([{"Cost": 100, "Currency": "USD", "costType": "ActualCost", "month": "2026-09"}]),
        "aiMetricsTokensDaily": collected([
            {"resourceId": account, "time": "2026-09-02T00:00:00Z", "dimensions": {"modeldeploymentname": "mini"}, "metric": "ProcessedPromptTokens", "total": 2000},
            {"resourceId": account, "time": "2026-09-02T00:00:00Z", "dimensions": {"modeldeploymentname": "mini"}, "metric": "GeneratedTokens", "total": 500},
        ]), "aiDeployments": collected([{"accountId": account, "name": "mini", "modelName": "gpt-5.4-mini", "skuName": "DataZoneStandard"}]),
        "pricesModels": collected([
            {"meterName": "5.4 mini Inp Dz", "retailPrice": 1, "currencyCode": "USD", "unitOfMeasure": "1M Tokens", "type": "Consumption", "tierMinimumUnits": 0},
            {"meterName": "5.4 mini Opt Dz", "retailPrice": 2, "currencyCode": "USD", "unitOfMeasure": "1M Tokens", "type": "Consumption", "tierMinimumUnits": 0},
            {"meterName": "5.4 mini Inp Gl", "retailPrice": .5, "currencyCode": "USD", "unitOfMeasure": "1M Tokens", "type": "Consumption", "tierMinimumUnits": 0},
            {"meterName": "5.4 mini Opt Gl", "retailPrice": 1, "currencyCode": "USD", "unitOfMeasure": "1M Tokens", "type": "Consumption", "tierMinimumUnits": 0},
        ])})
    a = analyze(ev, currency="USD", ai_mappings=[{"account": account, "deployment": "mini", "price_in_ref": price_reference(ev, 0), "price_out_ref": price_reference(ev, 1),
              "target_in_ref": price_reference(ev, 2), "target_out_ref": price_reference(ev, 3), "target_confirmed": True, "evidence": "unit-test-fixture: 같은 토큰을 처리하는 가격 비교 가정"}])
    t = a["ai"]["tokens"][0]
    assert t["retail_estimate"]["value"] == pytest.approx(2000/1_000_000*1 + 500/1_000_000*2)
    assert t["retail_to_be"]["value"] == pytest.approx(2000/1_000_000*.5 + 500/1_000_000*1)
    assert a["summary"]["total"]["value"] == 100
    assert a["comparison"]["savings"]["value"] is None


def test_seeded_new_graph_has_unique_topological_nodes():
    qs = [{"id": "A1-1", "area": "A", "topic_name": "업무", "question": "처리 규모"}]
    a = cost_calc.compute_finops(evidence(), "new", {"new": {"work_days": 20}}, None, qs, {"A1-1": {"transcript": "한 건에 5분 정도 걸리고 하루 1,200건쯤 됩니다."}})
    seen = set()
    for n in a["calculation"]:
        assert n["id"] not in seen, "동일 ID를 덮어쓰면 Excel 참조가 다른 계산을 가리킨다"
        if n["expression"]:
            assert all(k in seen for k in n["expression"]["args"]), "선행 입력/수식만 참조해야 한다"
        seen.add(n["id"])


def test_allocation_reconciles_unknown_tags_without_losing_cost():
    rid = "/subscriptions/test-subscription/providers/x/A"
    ev = evidence({"costByResource": collected([
        {"Cost": 20, "Currency": "USD", "ResourceId": rid}, {"Cost": 30, "Currency": "USD", "ResourceId": rid + "B"},
    ]), "resourceTags": collected([{"id": rid, "tags": {"workload": "test-fixture"}}])})
    a = analyze(ev, dataset="resource", currency="USD", allocation_key="workload")
    assert a.get("allocation", {}).get("allocated")["value"] == 20
    assert a["allocation"]["unallocated"]["value"] == 30
    assert a["allocation"]["total"]["value"] == 50
    assert a["allocation"]["semantics"] == "current_inventory_tags_not_historical_billing_tags"


def test_net_saving_includes_confirmed_recurring_cost():
    ev = evidence({"costByResource": collected([{"Cost": 100, "Currency": "USD", "ResourceId": "/subscriptions/test-subscription/providers/x/y"}])})
    action = {"id": "A", "name": "시험조치", "kind": "confirmed_amount", "row_ids": ["resource_0"], "amount": 80,
              "currency": "USD", "basis": "ActualCost", "enabled": True, "scope_confirmed": True, "evidence": "unit-test-fixture"}
    a = analyze(ev, dataset="resource", currency="USD", actions=[action], recurring_cost=20, recurring_cost_evidence="unit-test-fixture")
    assert a["comparison"]["to_be"]["value"] == 40
    assert a["comparison"]["savings"]["value"] == 60
    assert a["comparison"]["gross_savings"]["value"] == 80
    assert a["comparison"]["savings_rate"]["value"] == 60 / 100


def test_source_refresh_invalidates_scoped_action():
    ev = evidence({"costByResource": collected([{"Cost": 100, "Currency": "USD", "ResourceId": "/subscriptions/test-subscription/providers/x/y"}])})
    action = {"id": "A", "kind": "confirmed_amount", "row_ids": ["resource_0"], "amount": 80,
              "currency": "USD", "basis": "ActualCost", "enabled": True, "scope_confirmed": True, "evidence": "unit-test-fixture", "evidence_snapshot": "stale"}
    a = analyze(ev, dataset="resource", actions=[action], recurring_cost=0, recurring_cost_evidence="unit-test-fixture")
    assert a["actions"][0]["status"] == "stale_source"
    assert a["comparison"]["savings"]["value"] is None


def test_annualization_is_explicit_and_ledger_driven():
    ev = evidence({"costByResource": collected([{"Cost": 100, "Currency": "USD", "ResourceId": "/subscriptions/test-subscription/providers/x/y"}])})
    action = {"id": "A", "kind": "confirmed_amount", "row_ids": ["resource_0"], "amount": 30,
              "currency": "USD", "basis": "ActualCost", "enabled": True, "scope_confirmed": True, "evidence": "unit-test-fixture"}
    a = analyze(ev, dataset="resource", actions=[action], recurring_cost=0, recurring_cost_evidence="unit-test-fixture", projection={"enabled": True, "evidence": "unit-test-fixture: 동일 사용량 유지 가정"}, transition_cost=60, transition_cost_evidence="unit-test-fixture")
    assert a.get("projection", {}).get("annual_savings", {}).get("value") == 30 / 30 * 365
    assert a["projection"]["payback_months"]["value"] is not None
    assert a["projection"]["classification"] == "same_daily_usage_scenario_not_forecast_actual"


def test_options_validator_rejects_nonfinite_unknown_and_wrong_basis():
    import pytest
    ev = evidence({"costByResource": collected([{"Cost": 10, "Currency": "USD"}])})
    for bad in ({"dataset": "resource", "basis": "AmortizedCost"}, {"dataset": "no_such_dataset"},
                {"new": {"hourly_cost": float("nan")}}, {"new": {"hourly_cost": -1}}, {"undocumented": 50},
                {"projection": {"horizon_months": 10000}}):
        with pytest.raises(ValueError):
            cost_calc.compute_finops(ev, "existing", bad)


def test_empty_collection_has_no_cost_total_or_subtotal():
    a = analyze(evidence({"costMonthlyTotals": collected([])}), currency="USD")
    assert a["summary"]["total"]["value"] is None
    assert a["summary"]["known_subtotal"]["value"] is None


def test_summary_numbers_do_not_create_transcript_seed():
    qs=[{'id':'A1-1','area':'A'}]
    ans={'A1-1':{'transcript':'숫자를 답하지 않았음','summary':'하루 1,200건, 건당 5분'}}
    r=cost_calc.compute_finops(evidence(), 'new', {}, None, qs, ans)
    assert not r['new']['seed']


def test_verified_seed_quote_contains_both_numeric_spans():
    qs=[{'id':'A1-1','area':'A'}]
    raw='원문 '+('설명 '*90)+'하루 1,200건, 건당 5분입니다.'
    ans={'A1-1':{'transcript':raw,'summary':''}}
    r=cost_calc.compute_finops(evidence(), 'new', {}, None, qs, ans)
    seed=r['new']['seed']
    assert '1,200' in seed['quote'] and '5분' in seed['quote']
    assert seed['quote'] in raw


def test_snapshot_tag_filter_matches_key_value_and_missing_dimension():
    rid='/subscriptions/unit-fixture/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/unit-fixture'
    ev=evidence({'costByResource':collected([{'Cost':10,'Currency':'USD','ResourceId':rid}]),
                 'resourceTags':collected([{'id':rid,'tags':{'workload':'unit-fixture'}}])})
    r=analyze(ev,dataset='resource',filters={'tag':{'key':'workload','value':'unit-fixture'}})
    assert r['summary']['total']['value']==10
    missing=analyze(ev,dataset='resource',filters={'region':'__missing__'})
    assert missing['summary']['total']['value']==10


def test_grouping_invalid_dimension_is_rejected():
    import pytest
    with pytest.raises(ValueError,match='차원|집계'):
        analyze(evidence(),dataset='monthly',group_by='region')


def test_missing_basis_currency_and_date_prevent_complete_total():
    cases=[('monthly','costMonthlyTotals',{'Cost':10,'Currency':'USD','month':'2026-09'}),
           ('monthly','costMonthlyTotals',{'Cost':10,'costType':'ActualCost','month':'2026-09'}),
           ('ai_daily','aiCostDaily',{'Cost':10,'Currency':'USD'})]
    for dataset,key,bad in cases:
        good={'Cost':100,'Currency':'USD','costType':'ActualCost','month':'2026-09','UsageDate':20260907}
        r=analyze(evidence({key:collected([good,bad])}),dataset=dataset)
        assert r['summary']['total']['value'] is None
        assert r['summary']['known_subtotal']['value']==100


def test_partial_baseline_cannot_produce_full_tobe_and_projection():
    src=collected([{'Cost':100,'Currency':'USD','ResourceId':'/subscriptions/unit-fixture/resourceGroups/rg/providers/Test/test/unit-fixture'}])
    src['status']='partial'
    ev=evidence({'costByResource':src})
    action={'name':'시험','kind':'confirmed_amount','amount':80,'currency':'USD','basis':'ActualCost','row_ids':['resource_0'],
            'enabled':True,'scope_confirmed':True,'evidence':'unit-test-fixture'}
    r=analyze(ev,dataset='resource',actions=[action],recurring_cost=0,recurring_cost_evidence='unit-test-fixture',projection={'enabled':True,'evidence':'unit-test-fixture'})
    assert r['summary']['known_subtotal']['value']==100
    for metric in ('to_be','savings','savings_rate'):
        assert r['comparison'][metric]['value'] is None
    assert r['projection']['annual_savings']['value'] is None


def test_failed_tag_records_are_not_allocation_evidence():
    rid='/subscriptions/unit-fixture/resourceGroups/rg/providers/Test/test/unit-fixture'
    tags=collected([{'id':rid,'tags':{'team':'unit-fixture'}}]);tags['ok']=False
    ev=evidence({'costByResource':collected([{'Cost':100,'Currency':'USD','ResourceId':rid}]),'resourceTags':tags})
    r=analyze(ev,dataset='resource',allocation_key='team')
    assert r['allocation']['allocated']['value'] in (None,0)
    assert r['rows'][0]['tags']=={}


def test_price_reference_stays_same_for_identical_row_after_reordering():
    import cost_finops
    rows=[{'meterName':'5.4 mini Inp Dz','retailPrice':1,'currencyCode':'USD','unitOfMeasure':'1M Tokens','type':'Consumption','tierMinimumUnits':0},
          {'meterName':'5.4 mini Opt Dz','retailPrice':2,'currencyCode':'USD','unitOfMeasure':'1M Tokens','type':'Consumption','tierMinimumUnits':0}]
    ev=evidence({'pricesModels':collected(rows)})
    before={r['meter']:r['ref'] for r in cost_finops.price_catalogue(ev)}
    ev['sections']['pricesModels']['records']=list(reversed(rows))
    after={r['meter']:r['ref'] for r in cost_finops.price_catalogue(ev)}
    assert before==after


def test_cached_or_batch_rate_cannot_price_unqualified_total_prompt_tokens():
    import cost_finops
    bad={'meterName':'5.4 mini Cached Inp Batch Gl','retailPrice':0.1,'currencyCode':'USD','unitOfMeasure':'1M Tokens','type':'Consumption','tierMinimumUnits':0}
    ev=evidence({'pricesModels':collected([bad])})
    catalog=cost_finops.price_catalogue(ev)
    assert cost_finops.chosen_token_price(catalog,catalog[0]['ref'],'input','USD') is None


def test_new_mixed_model_pair_cannot_produce_adoption_cost():
    ev=evidence({'pricesModels':collected([
        {'meterName':'5.4 mini Inp Dz','retailPrice':1,'currencyCode':'USD','unitOfMeasure':'1M Tokens','type':'Consumption','tierMinimumUnits':0},
        {'meterName':'6 Opt Gl','retailPrice':2,'currencyCode':'USD','unitOfMeasure':'1M Tokens','type':'Consumption','tierMinimumUnits':0}])})
    options={'new':{'monthly_requests':100,'input_tokens':1000,'output_tokens':100,'price_in_ref':price_reference(ev,0),'price_out_ref':price_reference(ev,1)}}
    r=cost_calc.compute_finops(ev,'new',options)
    assert r['new']['ai_cost']['value'] is None
