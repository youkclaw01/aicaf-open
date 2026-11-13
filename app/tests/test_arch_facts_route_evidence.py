"""AS-IS D2가 UDR 미수집/존재를 '없음'으로 바꾸지 않는 순수 회귀 시험."""
import copy
import json
import unittest
from pathlib import Path
import arch_facts as F


class FactsRouteEvidenceTests(unittest.TestCase):
    def test_udr_presence_or_missing_capture_never_becomes_observed_default_egress(self):
        path=Path(__file__).resolve().parent/'fixtures/architecture/topology.synthetic.json'
        original=json.loads(path.read_text(encoding='utf-8'))
        for attached in (False, True):
            topo=copy.deepcopy(original)
            if attached:
                subnet=topo['configurations']['subnets'][0]
                subnet['routeTableId']='/subscriptions/unit/resourceGroups/unit/providers/Microsoft.Network/routeTables/RT-BLOCK'
                topo['configurations']['routeTables']=[{'id':subnet['routeTableId'],'routes':[{'addressPrefix':'0.0.0.0/0','nextHopType':'None'}]}]
            before=copy.deepcopy(topo)
            sk=F.skeleton(topo,{'sections':{}},'existing','AS-IS','D2')
            self.assertEqual(topo,before)
            self.assertFalse(any(e.get('label')=='기본 아웃바운드' and e.get('style')=='solid' for e in sk['edges']))
            self.assertFalse(any('서브넷 UDR 없음' in n.get('text','') for n in sk.get('notes',[])))

    def test_waf_detection_mode_triggers_f4_and_enriches_from_reports(self):
        path = Path(__file__).resolve().parent / 'fixtures/architecture/topology.synthetic.json'
        topo = json.loads(path.read_text(encoding='utf-8'))
        agw_id = '/subscriptions/unit/resourcegroups/unit/providers/microsoft.network/applicationgateways/ag-unit'
        waf_id = '/subscriptions/unit/resourcegroups/unit/providers/microsoft.network/applicationgatewaywebapplicationfirewallpolicies/waf-unit'
        topo['resources'].append({'id': agw_id, 'name': 'ag-unit', 'type': 'microsoft.network/applicationgateways', 'location': 'koreacentral', 'resourceGroup': 'unit'})
        topo.setdefault('publicExposureModel', {})['publicEndpoints'] = [{'endpointId': 'ep1', 'resourceId': agw_id, 'endpointType': 'application_gateway_frontend', 'exposureRole': 'public_ingress_configured'}]
        topo['configurations']['applicationGateways'] = [{'id': agw_id, 'firewallPolicyId': waf_id}]
        ev_det = {'sections': {'wafPolicies': {'status': 'collected', 'records': [{'id': waf_id, 'state': 'Enabled', 'mode': 'Detection'}]}}}
        ev_prev = {'sections': {'wafPolicies': {'status': 'collected', 'records': [{'id': waf_id, 'state': 'Enabled', 'mode': 'Prevention'}]}}}
        f_det = F.findings(F.Facts(topo, ev_det))
        f_prev = F.findings(F.Facts(topo, ev_prev))
        self.assertTrue(any(x['id'] == 'F4' and '탐지 모드' in x['title'] for x in f_det))
        self.assertFalse(any(x['id'] == 'F4' for x in f_prev))
        t_det = F.tasks(F.Facts(topo, ev_det), 'existing', f_det)
        cs_rep = {'stages': [{'areas': [{'area_id': 'network', 'headline': '네트워크 현황', 'observations': [{'id': 'O_P0004', 'text': 'WAF-01 정책은 Detection 모드다.', 'criterion_refs': ['AILZ-N-R5']}]}]}]}
        int_rep = {'stages': [{}, {}, {'actions': [{'id': 'A_ITEM_0016', 'title': 'WAF Prevention 전환 및 진입 보호 보완', 'criterion_refs': ['AILZ-N-R5'], 'rationale': '탐지 모드는 차단을 수행하지 않으므로 Prevention 전환이 필요합니다.'}]}]}
        ef, et = F.enrich_from_reports(f_det, t_det, cs_rep, int_rep)
        f4 = next(x for x in ef if x['id'] == 'F4')
        self.assertEqual(f4.get('observation_refs'), ['O_P0004'])
        t4 = next(t for t in et if t['id'] == 'T-NET-04')
        self.assertIn('A_ITEM_0016', t4['from'])
        self.assertEqual(t4.get('action_refs'), ['A_ITEM_0016'])


if __name__=='__main__':
    unittest.main()
