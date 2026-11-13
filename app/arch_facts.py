"""아키텍처 작도 — 수집 사실(topology.json + aicaf-evidence.json) → 작도 골격(skeleton).

표준: arch/ARCH-DRAWING-STANDARD.md (v1.0). 이 모듈이 정하는 것은 '무엇이 있고 어디에 속하는가'뿐이다.
- 판단은 ARM 타입·관계 타입·설정 값으로만 한다(이름 추측 금지 — 고객명·리전 문자열·이름 접두어 0건).
- 이름·개수·CIDR·리전은 증거에서 그대로 복사한다. 반복 리소스는 '카드 1장 + 개수'로 묶는다(§S6).
- 모든 노드에 region·row·parent·evidence·reason(배치 근거)을 붙인다(§5).
- TO-BE 는 AS-IS 를 그대로 두고 06-D 블록 트리거(판정)로 생긴 과제의 추가/변경/제거만 얹는다(§6).
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

# ---- 타입 분류(코드 표, 이름 판단 없음) ----
HIDDEN = {  # 배관: 그림에서는 접고 관계·배지로만 남긴다
    "microsoft.network/networkinterfaces", "microsoft.compute/disks", "microsoft.compute/virtualmachines/extensions",
    "microsoft.compute/virtualmachines/runcommands", "microsoft.compute/sshpublickeys", "microsoft.compute/snapshots",
    "microsoft.network/publicipaddresses", "microsoft.network/networksecuritygroups",
    "microsoft.network/privatednszones/virtualnetworklinks", "microsoft.eventgrid/systemtopics",
    "microsoft.app/managedenvironments/managedcertificates", "microsoft.compute/proximityplacementgroups",
    "microsoft.portal/dashboards", "microsoft.domainregistration/domains",
    "microsoft.certificateregistration/certificateorders", "microsoft.network/routetables",
    "microsoft.insights/actiongroups", "microsoft.network/applicationgatewaywebapplicationfirewallpolicies",
    "microsoft.network/firewallpolicies",
}
EDGE_TYPES = {"microsoft.network/frontdoors", "microsoft.cdn/profiles", "microsoft.network/azurefirewalls",
              "microsoft.network/ddosprotectionplans", "microsoft.network/trafficmanagerprofiles"}
COMMON_NET = {"microsoft.network/privatednszones", "microsoft.network/networkwatchers",
              "microsoft.network/dnsresolvers", "microsoft.network/trafficmanagerprofiles", "microsoft.network/natgateways"}
GATE_TYPES = {"microsoft.network/bastionhosts", "microsoft.apimanagement/service",
              "microsoft.network/virtualnetworkgateways", "microsoft.network/applicationgateways"}
APP_TYPES = {"microsoft.compute/virtualmachines", "microsoft.compute/virtualmachinescalesets",
             "microsoft.containerservice/managedclusters", "microsoft.app/managedenvironments",
             "microsoft.app/containerapps", "microsoft.app/jobs", "microsoft.web/sites", "microsoft.web/serverfarms",
             "microsoft.containerregistry/registries", "microsoft.cognitiveservices/accounts",
             "microsoft.machinelearningservices/workspaces"}
DATA_TYPES = {"microsoft.storage/storageaccounts", "microsoft.sql/servers", "microsoft.sql/managedinstances",
              "microsoft.documentdb/databaseaccounts", "microsoft.kusto/clusters", "microsoft.databricks/workspaces",
              "microsoft.cache/redis", "microsoft.dbforpostgresql/flexibleservers",
              "microsoft.dbformysql/flexibleservers", "microsoft.search/searchservices",
              "microsoft.datafactory/factories", "microsoft.eventhub/namespaces", "microsoft.servicebus/namespaces",
              "microsoft.netapp/netappaccounts"}
MGMT_TYPES = {"microsoft.keyvault/vaults": "mgmt/keyvault", "microsoft.operationalinsights/workspaces": "mgmt/log-analytics",
              "microsoft.managedidentity/userassignedidentities": "mgmt/managed-identity",
              "microsoft.automation/automationaccounts": "microsoft.automation/automationaccounts",
              "microsoft.recoveryservices/vaults": "microsoft.recoveryservices/vaults",
              "microsoft.insights/components": "microsoft.insights/components"}
PUBLIC_DNS = "microsoft.network/dnszones"
SERVICE_NAME = {  # 카드 라벨에 쓰는 Azure 공식 서비스명
    "microsoft.compute/virtualmachines": "Virtual Machine", "microsoft.compute/virtualmachinescalesets": "VM Scale Set",
    "microsoft.containerservice/managedclusters": "AKS", "microsoft.app/managedenvironments": "Container Apps Environment",
    "microsoft.app/containerapps": "Container App", "microsoft.app/jobs": "Container Apps Job",
    "microsoft.web/sites": "App Service", "microsoft.containerregistry/registries": "Container Registry",
    "microsoft.cognitiveservices/accounts": "Foundry", "microsoft.storage/storageaccounts": "Storage Account",
    "microsoft.kusto/clusters": "Data Explorer", "microsoft.keyvault/vaults": "Key Vaults",
    "microsoft.operationalinsights/workspaces": "Log Analytics", "microsoft.managedidentity/userassignedidentities": "Managed Identity",
    "microsoft.automation/automationaccounts": "Automation", "microsoft.recoveryservices/vaults": "Recovery Services Vault",
    "microsoft.network/loadbalancers": "Load Balancer", "microsoft.network/privateendpoints": "Private Endpoint",
    "microsoft.network/privatednszones": "Private DNS Zone", "microsoft.network/networkwatchers": "Network Watcher",
    "microsoft.network/dnszones": "Public DNS Zone", "microsoft.network/azurefirewalls": "Azure Firewall",
    "microsoft.network/bastionhosts": "Bastion", "microsoft.apimanagement/service": "API Management",
    "microsoft.network/frontdoors": "Front Door", "microsoft.cdn/profiles": "Front Door / CDN",
    "microsoft.network/applicationgateways": "Application Gateway", "microsoft.sql/servers": "SQL Server",
    "microsoft.documentdb/databaseaccounts": "Cosmos DB", "microsoft.search/searchservices": "AI Search",
    "microsoft.insights/components": "Application Insights", "microsoft.network/virtualnetworkgateways": "VPN Gateway",
    "microsoft.network/natgateways": "NAT Gateway", "microsoft.app/jobs": "Container Apps Job",
    "microsoft.network/virtualnetworks": "Virtual Network", "microsoft.network/trafficmanagerprofiles": "Traffic Manager",
    "microsoft.network/dnsresolvers": "DNS Private Resolver", "microsoft.network/ddosprotectionplans": "DDoS Protection",
    "microsoft.databricks/workspaces": "Databricks", "microsoft.cache/redis": "Redis",
    "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL", "microsoft.dbformysql/flexibleservers": "MySQL",
    "microsoft.machinelearningservices/workspaces": "Machine Learning", "microsoft.datafactory/factories": "Data Factory",
    "microsoft.eventhub/namespaces": "Event Hubs", "microsoft.servicebus/namespaces": "Service Bus",
    "microsoft.containerservice/managedclusters": "AKS", "microsoft.web/serverfarms": "App Service Plan",
}


def svc_name(t):
    """서비스 표시명. 표에 없으면 ARM 타입 꼬리를 그대로 쓰지 않고 'Azure <타입>' 형태로(검사 A12 가 원형 노출을 잡는다)."""
    if t in SERVICE_NAME:
        return SERVICE_NAME[t]
    tail = leaf(t).split("#")[0]
    return "Azure " + tail[:1].upper() + tail[1:]
ROW_NAME = {1: "외부 경계", 2: "진입·관문", 3: "애플리케이션", 4: "데이터"}


def lid(x):
    return (x or "").lower()


def leaf(x):
    return (x or "").rstrip("/").split("/")[-1]


_TYPES = {}  # 수집 리소스의 실제 type(Facts 가 채운다)


def rtype(rid):
    """ARM ID → 타입(소문자). 수집된 리소스면 수집 type 그대로, 아니면 ID 의 '네임스페이스/타입/이름/하위타입…' 규칙."""
    rid = lid(rid)
    if rid in _TYPES:
        return _TYPES[rid]
    s = rid.split("/providers/")[-1].split("/")
    parts = [s[0]] + s[1::2] if s else []
    return "/".join(parts) if len(parts) >= 2 else ""


def load(case_dir):
    """케이스 Source 의 topology.json(필수)·aicaf-evidence.json(선택)."""
    src = Path(case_dir) / "Source"
    topo = next(iter(sorted(src.glob("topology*.json"))), None)
    if not topo:
        raise FileNotFoundError("topology.json 이 없습니다(인프라 분석 탭에서 수집 결과를 올려 주세요).")
    with open(topo, encoding="utf-8-sig") as fh:
        t = json.load(fh)
    ev = {}
    p = src / "aicaf-evidence.json"
    if p.is_file():
        with open(p, encoding="utf-8-sig") as fh:
            ev = json.load(fh)
    return t, ev, str(topo)


def _sec(ev, name):
    s = (ev.get("sections") or {}).get(name) or {}
    return s.get("records") or [] if s.get("status") == "collected" else []


class Facts:
    """수집 사실을 그림 단위로 정리한 결과. 그리기 전에 한 번만 만든다."""

    def __init__(self, topo, ev):
        self.t, self.ev = topo, ev
        c = topo.get("configurations") or {}
        self.c = c
        self.res = {lid(r["id"]): r for r in topo.get("resources") or []}
        _TYPES.clear()
        _TYPES.update({k: lid(v["type"]) for k, v in self.res.items()})
        self.rels = topo.get("relationships") or []
        self.subnets = {lid(s["id"]): s for s in c.get("subnets") or []}
        self.vnets = {lid(v["id"]): v for v in c.get("virtualNetworks") or []}
        self.nics = {}
        for n in c.get("networkInterfaces") or []:
            self.nics.setdefault(lid(n["id"]), n)  # NIC 는 ip 설정마다 한 줄 → 첫 줄만
        # NIC → 소유자(VM·PE). 관계 표가 아니라 설정 값으로 푼다(VM networkProfile, NIC managedBy).
        self.nic_owner = {}
        for p in c.get("platformServiceConfiguration") or []:
            for nid in ((p.get("networkProfile") or {}).get("networkInterfaceIds") or []):
                self.nic_owner[lid(nid)] = lid(p["id"])
        for r in topo.get("resources") or []:
            if lid(r["type"]) == "microsoft.network/networkinterfaces" and r.get("managedBy"):
                self.nic_owner.setdefault(lid(r["id"]), lid(r["managedBy"]))
        self.pip_of_nic = {lid(r["targetId"]) for r in self.rels if r["type"] == "public_ip_assigned_to"
                           and rtype(r["targetId"]) == "microsoft.network/networkinterfaces"}
        self.pip_of_res = {lid(r["targetId"]) for r in self.rels if r["type"] == "public_ip_assigned_to"}
        self.pe_target = {lid(r["sourceId"]): lid(r["targetId"]) for r in self.rels if r["type"] == "private_endpoint_target"}
        self.pe_subnet = {lid(p["id"]): lid(p.get("subnetId")) for p in c.get("privateEndpointConfiguration") or []}
        self.cae_subnet = {}
        for e in _sec(ev, "containerAppEnvironments"):
            if e.get("infrastructureSubnetId"):
                self.cae_subnet[lid(e["id"])] = lid(e["infrastructureSubnetId"])
        self.ca_env = {lid(a["id"]): lid(a.get("environmentId")) for a in c.get("containerApps") or []}
        self.ingress = [e for e in (topo.get("publicExposureModel") or {}).get("publicEndpoints") or []
                        if e.get("exposureRole") == "public_ingress_configured"]
        self.paas_public = {}
        for p in (c.get("paasNetworkAccess") or []) + _sec(ev, "paasNetworkExtended") + (c.get("foundryAccountConfiguration") or []):
            if p.get("publicNetworkAccess"):
                self.paas_public[lid(p["id"])] = p["publicNetworkAccess"]
        self.zones = sorted({z for r in topo.get("resources") or [] for z in (r.get("zones") or []) if z})
        # 사설 IP → 소유 리소스(VM·PE). AppGW 백엔드 풀 주소를 VM 으로 푸는 데 쓴다(설정 근거)
        self.ip_owner = {}
        for nid, n in self.nics.items():
            pass
        for n in c.get("networkInterfaces") or []:
            ow = self.nic_owner.get(lid(n["id"]))
            if ow and n.get("privateIpAddress"):
                self.ip_owner[n["privateIpAddress"]] = ow
        self.agw_backends = defaultdict(list)  # appgw id → [(pool, 대상 리소스 id)]
        for a in c.get("applicationGatewayConfiguration") or []:
            for p_ in a.get("backendAddressPools") or []:
                for ad in p_.get("backendAddresses") or []:
                    ow = self.ip_owner.get(ad.get("ipAddress") or "")
                    if ow:
                        self.agw_backends[lid(a["id"])].append((p_.get("name"), ow))
                for ic in p_.get("backendIpConfigurationIds") or []:
                    nic = lid(ic).split("/ipconfigurations/")[0]
                    if self.nic_owner.get(nic):
                        self.agw_backends[lid(a["id"])].append((p_.get("name"), self.nic_owner[nic]))
        self.nat_subnets = defaultdict(list)
        for r in self.rels:
            if r["type"] == "nat_gateway_subnet_association":
                self.nat_subnets[lid(r["sourceId"])].append(lid(r["targetId"]))
        for s_ in c.get("subnets") or []:
            if s_.get("natGatewayId"):
                self.nat_subnets[lid(s_["natGatewayId"])].append(lid(s_["id"]))

    # ---- 위치 ----
    def region_of(self, rid):
        r = self.res.get(lid(rid))
        return lid((r or {}).get("location")) or "global"

    def subnet_of(self, rid):
        """리소스가 속한 서브넷(없으면 None). VM=NIC, PE=설정, Container Apps=환경 인프라 서브넷."""
        rid = lid(rid)
        t = rtype(rid)
        if t == "microsoft.network/privateendpoints":
            return self.pe_subnet.get(rid)
        if t == "microsoft.network/applicationgateways":
            for sid, s in self.subnets.items():  # 서브넷 설정의 applicationGatewayIpConfigurationIds(설정 근거)
                if any(lid(x).startswith(rid + "/") for x in s.get("applicationGatewayIpConfigurationIds") or []):
                    return sid
            return None
        if t == "microsoft.app/managedenvironments":
            return self.cae_subnet.get(rid)
        if t in ("microsoft.app/containerapps",):
            return self.cae_subnet.get(self.ca_env.get(rid, ""))
        if t == "microsoft.app/jobs":
            envs = list(self.cae_subnet)
            # 작업(job)의 환경 ID 는 수집 항목에 없다 → 같은 RG 의 환경이 하나뿐일 때만 붙이고 근거에 '추정' 기록
            rg = lid(self.res.get(rid, {}).get("resourceGroup"))
            same = [e for e in envs if lid(self.res.get(e, {}).get("resourceGroup")) == rg]
            return self.cae_subnet[same[0]] if len(same) == 1 else None
        for nid, own in self.nic_owner.items():
            if own == rid and nid in self.nics:
                return lid(self.nics[nid].get("subnetId"))
        return None

    def has_public_ip(self, rid):
        rid = lid(rid)
        if rid in self.pip_of_res:
            return True
        return any(own == rid and nid in self.pip_of_nic for nid, own in self.nic_owner.items())


# ---- 발견(AS-IS 문제 표시) · 과제(TO-BE) : 06-D 블록 트리거를 코드로 ----
def findings(f):
    """판정 결과 미흡 항목(주황 테두리 + 발견 ID). 근거 원칙 ID 는 원칙 카탈로그(130)의 ID."""
    out = []
    ai = [r for r in f.res if rtype(r) == "microsoft.cognitiveservices/accounts"]
    pub = [r for r, v in f.paas_public.items() if v.lower() == "enabled" and r in f.res
           and rtype(r) in (APP_TYPES | DATA_TYPES)]
    if pub:
        out.append({"id": "F1", "title": "공용 네트워크 접근 허용 PaaS", "principle": "AILZ-N-R3 · WAF-AI-SE4",
                    "resources": sorted(pub), "count": len(pub), "block": "NET-PE"})
    if not any(rtype(r) == "microsoft.network/azurefirewalls" for r in f.res):
        n = len(f.vnets)
        if n:
            out.append({"id": "F2", "title": "아웃바운드 통제(Azure Firewall) 없음", "principle": "AILZ-N-R7 · AILZ-N-R9",
                        "resources": sorted(f.vnets), "count": n, "block": "NET-HUB"})
    exposed_vm = sorted(r for r in f.res if rtype(r) == "microsoft.compute/virtualmachines" and f.has_public_ip(r))
    if exposed_vm and not any(rtype(r) == "microsoft.network/bastionhosts" for r in f.res):
        out.append({"id": "F3", "title": "VM 공용 IP 직접 노출(Bastion 없음)", "principle": "AILZ-N-R2",
                    "resources": exposed_vm, "count": len(exposed_vm), "block": "NET-BASTION"})
    # WAF 판정: Front Door/CDN 또는 Prevention 모드의 WAF 정책이 붙은 Application Gateway(firewallPolicyId 및 wafPolicies 설정 근거)
    waf_policies = {lid(w.get("id")): w for w in _sec(f.ev, "wafPolicies") if w.get("id")}
    def _agw_waf_prevents(g):
        pid = lid(g.get("firewallPolicyId"))
        if not pid:
            return False
        w = waf_policies.get(pid)
        if not w:
            return True
        return (w.get("state") or "enabled").lower() == "enabled" and (w.get("mode") or "").lower() == "prevention"
    waf = any(rtype(r) in ("microsoft.network/frontdoors", "microsoft.cdn/profiles") for r in f.res) or any(
        _agw_waf_prevents(g) for g in f.c.get("applicationGateways") or [])
    ing = sorted({lid(e["resourceId"]) for e in f.ingress})
    if ing and not waf:
        det = any(lid(g.get("firewallPolicyId")) in waf_policies and (waf_policies[lid(g.get("firewallPolicyId"))].get("mode") or "").lower() == "detection"
                  for g in f.c.get("applicationGateways") or [])
        out.append({"id": "F4", "title": "공개 진입점 WAF 탐지 모드(차단 미적용)" if det else "공개 진입점 WAF 미경유",
                    "principle": "AILZ-N-R5 · AILZ-N-R1", "resources": ing, "count": len(ing), "block": "NET-EDGE"})
    if ai and not any(rtype(r) == "microsoft.apimanagement/service" for r in f.res):
        out.append({"id": "F5", "title": "AI 게이트웨이(API Management) 없음", "principle": "AILZ-N-R6",
                    "resources": sorted(ai), "count": len(ai), "block": "GW-APIM"})
    diag = _sec(f.ev, "diagnosticSettingsExtended")
    to_law = {lid(d.get("targetResourceId")) for d in diag if d.get("workspaceId")}
    no_law = sorted(r for r in ai if r not in to_law)
    if no_law:
        out.append({"id": "F6", "title": "AI 진단 로그 Log Analytics 미연결", "principle": "AILZ-M-R4 · MCSB-AOAI-LT-4",
                    "resources": no_law, "count": len(no_law), "block": "OPS-DIAG"})
    return out


TASKS = {  # 06-D 블록 → 과제(ID·이름·행). 추가되는 개체와 변경되는 개체를 코드로 정한다.
    "NET-PE": ("T-NET-01", "AI·데이터 엔드포인트 사설화"),
    "NET-HUB": ("T-NET-03", "Hub 방화벽·아웃바운드 통제"),
    "NET-BASTION": ("T-NET-05", "관리 접근 Bastion 전환"),
    "NET-EDGE": ("T-NET-04", "공개 진입점 Front Door·WAF"),
    "GW-APIM": ("T-AIS-01", "AI 게이트웨이(API Management)"),
    "OPS-DIAG": ("T-OPS-01", "AI 진단 로그 → Log Analytics"),
    "AIS-FDY": ("T-AIS-02", "AI 랜딩존 Spoke(Foundry 표준)"),
}


def tasks(f, track, fnd):
    out = []
    for x in fnd:
        tid, name = TASKS[x["block"]]
        out.append({"id": tid, "name": name, "block": x["block"], "from": x["id"], "principle": x["principle"],
                    "resources": x["resources"]})
    ai = [r for r in f.res if rtype(r) == "microsoft.cognitiveservices/accounts"]
    if track == "new":
        # 신규 도입: AI 랜딩존 Spoke 를 새로 붙인다(§6, 06-D 'AI 미사용' 열). 게이트웨이도 함께.
        out.append({"id": "T-AIS-02", "name": TASKS["AIS-FDY"][1], "block": "AIS-FDY", "from": "인터뷰(신규구축)",
                    "principle": "AILZ-RO-R4 · AILZ-D-R1", "resources": []})
        if not any(t["block"] == "GW-APIM" for t in out):
            out.append({"id": "T-AIS-01", "name": TASKS["GW-APIM"][1], "block": "GW-APIM", "from": "인터뷰(신규구축)",
                        "principle": "AILZ-N-R6", "resources": []})
    if any(t["block"] == "NET-PE" for t in out) or track == "new":
        out.append({"id": "T-NET-02", "name": "Private DNS 중앙화", "block": "NET-DNS", "from": "T-NET-01 선행",
                    "principle": "AILZ-N-R8", "resources": []})
    seen, uniq = set(), []
    for t in out:
        if t["id"] not in seen:
            seen.add(t["id"]); uniq.append(t)
    return sorted(uniq, key=lambda t: t["id"])


_CRITERION_BLOCKS = {
    "AILZ-N-R3": "NET-PE", "WAF-AI-SE4": "NET-PE", "MCSB-AOAI-DP-3": "NET-PE", "MCSB-AOAI-DP-4": "NET-PE",
    "AILZ-N-R8": "NET-DNS",
    "AILZ-N-R7": "NET-HUB", "AILZ-N-R9": "NET-HUB",
    "AILZ-N-R5": "NET-EDGE", "AILZ-N-R1": "NET-EDGE",
    "AILZ-N-R2": "NET-BASTION",
    "AILZ-N-R6": "GW-APIM",
    "AILZ-M-R4": "OPS-DIAG", "MCSB-AOAI-LT-4": "OPS-DIAG",
}


def enrich_from_reports(fnd, tks, current_state_report=None, integrated_report=None):
    """현황분석(AS-IS) 및 종합 시사점·개선과제(Integrated) 보고서 내용을 findings·tasks 에 결합한다."""
    cs_areas = ((current_state_report or {}).get("stages") or [{}])[0].get("areas") or []
    if not cs_areas and integrated_report:
        cs_areas = ((integrated_report or {}).get("stages") or [{}])[0].get("areas") or []
    obs_by_crit = defaultdict(list)
    headline_by_area = {}
    for a in cs_areas:
        aid = a.get("area_id") or ""
        if a.get("headline"):
            headline_by_area[aid] = a["headline"]
        for o in a.get("observations") or []:
            for c in o.get("criterion_refs") or []:
                obs_by_crit[c].append((o.get("id") or "", o.get("text") or ""))
    out_fnd = []
    for x in fnd:
        row = dict(x)
        crits = [c.strip() for c in (row.get("principle") or "").split("·") if c.strip()]
        matched_obs = []
        for c in crits:
            for oid, otxt in obs_by_crit.get(c, []):
                if oid and oid not in [m[0] for m in matched_obs]:
                    matched_obs.append((oid, otxt))
        if matched_obs:
            row["observation_refs"] = [m[0] for m in matched_obs[:4]]
            first_sentence = matched_obs[0][1].split("다.")[0].strip() + "다." if "다." in matched_obs[0][1] else matched_obs[0][1][:160]
            row["asis_detail"] = f"{matched_obs[0][0]}: {first_sentence}"
        out_fnd.append(row)

    actions = []
    int_stages = (integrated_report or {}).get("stages") or []
    if len(int_stages) >= 3:
        actions = int_stages[2].get("actions") or []
    act_by_block = defaultdict(list)
    for act in actions:
        for c in act.get("criterion_refs") or []:
            blk = _CRITERION_BLOCKS.get(c)
            if blk and act not in act_by_block[blk]:
                act_by_block[blk].append(act)
    fnd_by_block = {x["block"]: x for x in out_fnd}
    out_tks = []
    for tk in tks:
        row = dict(tk)
        blk = row.get("block")
        acts = act_by_block.get(blk, [])
        if acts:
            a0 = acts[0]
            aid = a0.get("id") or a0.get("action_id") or ""
            atitle = a0.get("title") or ""
            fx = fnd_by_block.get(blk)
            from_prefix = f"{fx['id']}({fx['title']})" if fx else row.get("from", "")
            row["from"] = f"{from_prefix} → {aid}: {atitle}"[:140] if aid else f"{from_prefix} → {atitle}"[:140]
            row["action_refs"] = [a.get("id") or a.get("action_id") for a in acts if a.get("id") or a.get("action_id")]
            if a0.get("rationale"):
                row["rationale"] = a0["rationale"]
        out_tks.append(row)
    return out_fnd, out_tks


# ---- 골격 ----
def _card(nodes, nid, **kw):
    nodes.append(dict(id=nid, **kw))


def _short(name, n=26):
    return name if len(name) <= n else name[: n - 1] + "…"


def skeleton(topo, ev, track, state, diagram, task_override=None, findings_override=None):
    """작도 골격(spec 스키마 §7). 모델은 이 골격의 reason·문구만 다듬고, 노드·개수·이름은 바꾸지 못한다."""
    f = Facts(topo, ev)
    fnd = findings_override if findings_override is not None else findings(f)
    tks = task_override if task_override is not None else tasks(f, track, fnd)
    hit = defaultdict(list)
    for x in fnd:
        for r in x["resources"]:
            hit[r].append(x["id"])
    nodes, containers, edges, notes = [], [], [], []
    # 1) 그림에 올릴 리소스(배관 제외)를 그룹 키로 모은다
    drawn = [r for r in f.res if rtype(r) not in HIDDEN and rtype(r) not in ("microsoft.network/virtualnetworks",)]
    regions = Counter()
    for r in drawn:
        if rtype(r) in MGMT_TYPES or rtype(r) in COMMON_NET or rtype(r) == PUBLIC_DNS:
            continue
        regions[f.region_of(r)] += 1
    for v in f.vnets:
        regions[f.region_of(v)] += 5  # VNet 가 있는 리전이 주 리전
    regions.pop("global", None)
    reg_order = [r for r, _ in regions.most_common()]
    if not reg_order:
        reg_order = ["global"]
    prim = reg_order[0]
    regs = [{"id": f"r{i+1}", "name": n, "order": i + 1} for i, n in enumerate(reg_order)]
    rid_of = {r["name"]: r["id"] for r in regs}

    def rg_label(rid):
        return (f.res.get(rid) or {}).get("resourceGroup") or ""

    # 2) VNet → 서브넷 컨테이너
    sub_occ = defaultdict(list)
    for r in drawn:
        if rtype(r) in MGMT_TYPES or rtype(r) in COMMON_NET:
            continue
        s = f.subnet_of(r)
        if s and s in f.subnets:
            sub_occ[s].append(r)
    idle_vnets = []  # 워크로드가 하나도 없는 VNet → 카드 1장(+개수)으로 접는다(§S6 반복 압축, 공간은 증거 있는 흐름에)
    for vid, v in sorted(f.vnets.items(), key=lambda kv: leaf(kv[0])):
        if not any(lid(s.get("vnetId")) == vid and sub_occ.get(sid) for sid, s in f.subnets.items()):
            idle_vnets.append(vid)
            continue
        reg = f.region_of(vid)
        vn = (f.res.get(vid) or {}).get("name") or leaf(vid)
        vbox = {"id": "v:" + vid, "level": "vnet", "parent": rid_of.get(reg, regs[0]["id"]),
                "label": vn, "sub": ", ".join(v.get("addressPrefixes") or []), "evidence": vid,
                "style": "normal", "badges": [], "ddos": bool(v.get("ddosProtectionEnabled")),
                "rg": rg_label(vid)}
        containers.append(vbox)
        for sid, s in sorted(f.subnets.items(), key=lambda kv: kv[0]):
            if lid(s.get("vnetId")) != vid:
                continue
            occ = sub_occ.get(sid, [])
            if not occ:  # 점유 리소스가 없는 서브넷은 그리지 않고 VNet 칩에 개수만(이름은 spec.md 에 남김)
                vbox.setdefault("empty_subnets", []).append(s["name"])
                continue
            # 역할로 행 배정(§2.3): 진입(AppGW/공용 LB)=R1 · 관문/관리접근(Bastion·APIM·점프 VM=공용 IP VM 만 있는 서브넷)=R2
            # · 애플리케이션=R3 · 데이터(PE 전용·데이터 PaaS)=R4. 판단은 타입·공용 IP 설정으로만(이름 추측 금지)
            types = {rtype(o) for o in occ}
            vms = [o for o in occ if rtype(o) == "microsoft.compute/virtualmachines"]
            is_jump = bool(vms and len(vms) == len(occ) and all(f.has_public_ip(o) for o in vms) and not any(
                o in {ow for lst in f.agw_backends.values() for _, ow in lst} for o in vms))
            if types <= {"microsoft.network/privateendpoints"}:
                row = 4
                role_zone = "private_pe"
            elif types & {"microsoft.network/applicationgateways", "microsoft.network/azurefirewalls"}:
                row = 1
                role_zone = "ingress_egress"
            elif types & {"microsoft.network/bastionhosts"} or is_jump:
                row = 2
                role_zone = "mgmt_access"
            elif types & GATE_TYPES:
                row = 2
                role_zone = "ingress_egress"
            else:
                row = 3
                role_zone = "workload"
            badges = []
            if s.get("nsgId"):
                badges.append("nsg")
            if any(sid in v for v in f.nat_subnets.values()):
                badges.append("nat")
            if "microsoft.network/privateendpoints" in types:
                badges.append("pe")
            sub_rgs = sorted({rg_label(o) for o in occ if rg_label(o)})
            containers.append({"id": "s:" + sid, "level": "subnet", "parent": "v:" + vid, "label": s["name"],
                               "sub": ", ".join(s.get("addressPrefixes") or []), "row": row, "role_zone": role_zone,
                               "rg": sub_rgs[0] if len(sub_rgs) == 1 else (f"RG {len(sub_rgs)}개" if sub_rgs else rg_label(vid)),
                               "evidence": sid, "style": "normal", "badges": badges, "empty": not occ})
            # 서브넷 안 카드: 타입별로 묶는다(반복 압축) + WARA 블루프린트 수준 3줄 상세 기술 메타데이터 생성
            byt = defaultdict(list)
            for o in occ:
                byt[rtype(o)].append(o)
            for t, ids in sorted(byt.items()):
                names = [(f.res[i].get("name") or leaf(i)) for i in sorted(ids)]
                label = names[0] if len(ids) == 1 else f"{svc_name(t)} ×{len(ids)}"
                rgs_in_card = sorted({rg_label(i) for i in ids if rg_label(i)})
                rg_tag = rgs_in_card[0] if len(rgs_in_card) == 1 else (f"RG {len(rgs_in_card)}개" if rgs_in_card else "")
                chips = []
                if t == "microsoft.network/privateendpoints":
                    tg = [f.pe_target.get(i) for i in ids]
                    chips = [("→ " + (f.res[x]["name"] if x in f.res else leaf(x) + " (범위 밖)")) for x in tg if x]
                    if len(ids) > 3:
                        chips = chips[:2] + [f"외 {len(ids) - 2}개 사설 연결 · {rg_tag}" if rg_tag else f"외 {len(ids) - 2}개 사설 연결"]
                elif t == "microsoft.network/applicationgateways":
                    agw_cfg = next((g for g in f.c.get("applicationGatewayConfiguration") or [] if lid(g.get("id")) == ids[0]), {})
                    n_l = len(agw_cfg.get("listeners") or [])
                    n_p = len(f.agw_backends.get(ids[0], []))
                    waf_policies = {lid(w.get("id")): w for w in _sec(f.ev, "wafPolicies") if w.get("id")}
                    agw_meta = next((g for g in f.c.get("applicationGateways") or [] if lid(g.get("id")) == ids[0]), {})
                    wp = waf_policies.get(lid(agw_meta.get("firewallPolicyId")))
                    wmode = f"WAF {(wp.get('mode') or 'Enabled')}" if wp else "Public Frontend"
                    chips.append(f"{wmode} · 리스너 {n_l}개" if n_l else wmode)
                    chips.append(f"백엔드 연결 {n_p}개 · {'Public IP' if any(f.has_public_ip(i) for i in ids) else 'Private'}")
                    if rg_tag:
                        chips.append(f"Owner: {rg_tag}")
                elif len(ids) > 1:
                    chips = [", ".join(names[:2]) + (f" 외 {len(names)-2}" if len(names) > 2 else "")]
                if t == "microsoft.compute/virtualmachines":
                    sz = [((next((p for p in f.c.get("platformServiceConfiguration") or [] if lid(p["id"]) == i), {})
                            .get("hardwareProfile") or {}).get("vmSize")) for i in ids]
                    priv_ips = [n.get("privateIpAddress") for i in ids for nid, n in f.nics.items()
                                if f.nic_owner.get(nid) == i and n.get("privateIpAddress")]
                    if sz and sz[0]:
                        chips.append("SKU : " + sz[0])
                    net_tags = []
                    if priv_ips:
                        net_tags.append(priv_ips[0] if len(priv_ips) == 1 else f"{priv_ips[0]} 외")
                    if any(f.has_public_ip(i) for i in ids):
                        net_tags.append("Public IP")
                    else:
                        net_tags.append("Private Only")
                    if net_tags:
                        chips.append(" · ".join(net_tags))
                    if rg_tag and len(chips) < 3:
                        chips.append(f"Owner: {rg_tag}")
                _card(nodes, "n:" + ids[0], parent="s:" + sid, type=t, service=svc_name(t),
                      label=label, count=len(ids), chips=chips[:3], rg=rg_tag, role_zone=role_zone,
                      members=sorted(ids), region=reg, row=row,
                      evidence=sorted(ids), finding=sorted({h for i in ids for h in hit.get(i, [])}),
                      reason=f"{ROW_NAME[row]} 행: 서브넷 {s['name']} 소속(" +
                      ("NIC" if t.startswith("microsoft.compute") else "설정") + " 근거)")
    for c in containers:
        if c.get("empty_subnets"):
            c["sub"] = (c["sub"] + " · " if c["sub"] else "") + f"빈 서브넷 {len(c['empty_subnets'])}개"
    # 3) VNet 밖 리소스: 행 그룹(리전별), 타입별 묶음
    rest = defaultdict(list)
    for r in drawn:
        t = rtype(r)
        if t in MGMT_TYPES or t in COMMON_NET or t == PUBLIC_DNS:
            continue
        s = f.subnet_of(r)
        if s and s in f.subnets:
            continue
        reg = f.region_of(r)
        if reg == "global":
            reg = prim
        if t in EDGE_TYPES or (t == "microsoft.network/loadbalancers" and f.has_public_ip(r)):
            row = 1
        elif t in GATE_TYPES or t == "microsoft.network/loadbalancers":
            row = 2
        elif t in DATA_TYPES:
            row = 4
        else:
            row = 3
        rest[(reg, row, t)].append(r)
    for (reg, row, t), ids in sorted(rest.items()):
        gid = f"g:{rid_of.get(reg, regs[0]['id'])}:{row}"
        rgs = sorted({rg_label(i) for i in ids if rg_label(i)})
        rg_tag = rgs[0] if len(rgs) == 1 else (f"RG {len(rgs)}개" if rgs else "")
        if not any(c["id"] == gid for c in containers):
            containers.append({"id": gid, "level": "group", "parent": rid_of.get(reg, regs[0]["id"]),
                               "label": "Regional PaaS / Operations" if row == 3 else ("Durable data / security" if row == 4 else "외부 진입"),
                               "row": row, "style": "normal", "rg": rg_tag, "badges": []})
        names = [(f.res[i].get("name") or leaf(i)) for i in sorted(ids)]
        label = names[0] if len(ids) == 1 else f"{svc_name(t)} ×{len(ids)}"
        chips = (names[:2] if len(ids) > 1 else [])
        skus = sorted({((f.res[i].get("sku") or {}).get("name") or "") for i in ids if (f.res[i].get("sku") or {}).get("name")})
        if skus and len(chips) < 2:
            chips.append("SKU : " + ", ".join(skus[:2]))
        if rg_tag:
            chips.append(rg_tag)
        pub = [i for i in ids if (f.paas_public.get(i) or "").lower() == "enabled"]
        if pub:
            chips.append(f"PNA Enabled ({len(pub)}/{len(ids)})")
        else:
            pe_cnt = sum(1 for pe_t in f.pe_target.values() if pe_t in ids)
            if pe_cnt:
                chips.append(f"Private Endpoint 연결 ({pe_cnt}개)")
        _card(nodes, "n:" + sorted(ids)[0], parent=gid, type=t, service=svc_name(t), label=label,
              count=len(ids), chips=chips[:3], rg=rg_tag, members=sorted(ids), region=reg, row=row, evidence=sorted(ids),
              finding=sorted({h for i in ids for h in hit.get(i, [])}),
              reason=f"{ROW_NAME[row]} 행: {'공용 프런트엔드(public IP)' if row == 1 else 'VNet 밖 PaaS, 타입 ' + t}")
    # 4) 외부·공통 네트워크·관리
    external = [{"id": "x:internet", "kind": "internet", "label": "Internet", "side": "top-right"}]
    if any(rtype(r) == "microsoft.network/ddosprotectionplans" for r in f.res) or any(v.get("ddosProtectionEnabled") for v in f.vnets.values()):
        external.append({"id": "x:ddos", "kind": "ddos", "label": "DDoS Protection", "side": "top-right"})
    common = defaultdict(list)
    for r in drawn:
        if rtype(r) in COMMON_NET or rtype(r) == PUBLIC_DNS:
            common[rtype(r)].append(r)
    if idle_vnets:
        common["microsoft.network/virtualnetworks"] = idle_vnets
    if common:
        containers.append({"id": "c:common", "level": "group", "parent": rid_of[prim], "label": "Common Network Service",
                           "row": 1, "style": "yellow", "badges": []})
        for t, ids in sorted(common.items()):
            idle = t == "microsoft.network/virtualnetworks"
            c_names = [(f.res.get(i) or {}).get("name", leaf(i)) for i in sorted(ids)]
            c_rgs = sorted({rg_label(i) for i in ids if rg_label(i)})
            c_rg_tag = c_rgs[0] if len(c_rgs) == 1 else (f"RG {len(c_rgs)}개" if c_rgs else "")
            c_chips = list(c_names[:2])
            if t == "microsoft.network/natgateways":
                sub_cnt = sum(len(set(f.nat_subnets.get(i, []))) for i in ids)
                c_chips.append(f"서브넷 {sub_cnt}개 아웃바운드 연결" if sub_cnt else "Standard Egress")
            elif t == "microsoft.network/privatednszones":
                c_chips = [c.replace("privatelink.", "") for c in c_names[:2]] + ([f"외 {len(ids)-2}개 Zone · VNet 링크"] if len(ids) > 2 else ["VNet 링크 연결"])
            elif t == PUBLIC_DNS:
                c_chips.append(f"Public DNS · Owner: {c_rg_tag}" if c_rg_tag else "Public DNS Zone")
            _card(nodes, "n:" + sorted(ids)[0], parent="c:common", type=t, service="VNet(워크로드 없음)" if idle else svc_name(t),
                  label=("VNet(워크로드 없음)" if idle else svc_name(t)) + (f" ×{len(ids)}" if len(ids) > 1 else ""), count=len(ids),
                  chips=c_chips[:3], rg=c_rg_tag,
                  members=sorted(ids), region=prim, row=1, evidence=sorted(ids),
                  finding=sorted({h for i in ids for h in hit.get(i, [])}),
                  reason="NIC·PE·환경이 붙지 않은 VNet(접음)" if idle else "공통 네트워크 서비스(흐름 축 밖, §2.2)")
    mg = []
    ten_recs = _sec(ev, "tenant")
    if ten_recs:
        t_dom = (ten_recs[0].get("defaultDomain") or ten_recs[0].get("displayName") or "Tenant Auth")
        mg.append(("mgmt/entra", "Entra ID", "evidence:tenant", 1, [t_dom, "Azure RBAC · OIDC / Identity"]))
    mi = [r for r in f.res if rtype(r) == "microsoft.managedidentity/userassignedidentities"]
    if mi:
        mi_names = [(f.res.get(i) or {}).get("name", leaf(i)) for i in sorted(mi)]
        mg.append(("mgmt/managed-identity", "Managed Identity" + (f" ×{len(mi)}" if len(mi) > 1 else ""), sorted(mi), len(mi),
                   [", ".join(mi_names[:2]), "User-Assigned Identity · 무자격증명 인증"]))
    if f.c.get("policyAssignments"):
        mg.append(("mgmt/policy", "Policy", "configurations.policyAssignments", len(f.c["policyAssignments"]),
                   [f"정책 할당 {len(f.c['policyAssignments'])}건", "Subscription 거버넌스 감사"]))
    if _sec(ev, "costMonthlyTotals"):
        mg.append(("mgmt/cost", "Cost Management", "evidence:costMonthlyTotals", 1,
                   ["월별 비용 추이 · 리소스별 원장", "FinOps 예산·최적화 감시"]))
    if any((p.get("pricingTier") or "").lower() == "standard" for p in _sec(ev, "defenderPlans")):
        std_cnt = sum(1 for p in _sec(ev, "defenderPlans") if (p.get("pricingTier") or "").lower() == "standard")
        mg.append(("mgmt/defender", "Defender for Cloud", "evidence:defenderPlans", 1,
                   [f"Standard 플랜 {std_cnt}종 활성화", "CSPM · 워크로드 위협 보호"]))
    for t, key in MGMT_TYPES.items():
        if t == "microsoft.managedidentity/userassignedidentities":
            continue
        ids = [r for r in f.res if rtype(r) == t]
        if ids:
            m_names = [(f.res.get(i) or {}).get("name", leaf(i)) for i in sorted(ids)]
            m_rgs = sorted({rg_label(i) for i in ids if rg_label(i)})
            pe_linked = sum(1 for pe_t in f.pe_target.values() if pe_t in ids)
            m_chips = [", ".join(m_names[:2])]
            if pe_linked:
                m_chips.append(f"Private Endpoint {pe_linked}개 연결 · RBAC")
            elif m_rgs:
                m_chips.append("Owner: " + m_rgs[0])
            mg.append((key, SERVICE_NAME[t] + (f" ×{len(ids)}" if len(ids) > 1 else ""), sorted(ids), len(ids), m_chips))
    if any(rtype(r) == "microsoft.insights/actiongroups" for r in f.res) or _sec(ev, "diagnosticSettingsExtended"):
        diag_cnt = len(_sec(ev, "diagnosticSettingsExtended"))
        mg.append(("mgmt/monitor", "Monitor", "evidence:diagnosticSettingsExtended", 1,
                   [f"Diagnostic Settings {diag_cnt}건 수집" if diag_cnt else "Observability / Activity Log", "메트릭 · 경고 · 진단 파이프라인"]))
    containers.append({"id": "m:mgmt", "level": "mgmt", "parent": rid_of[prim], "label": "Management Service",
                       "row": 4, "style": "peach", "badges": []})
    for key, label, evid, n, m_chips in mg:
        _card(nodes, "m:" + key, parent="m:mgmt", type=key, service=label.split(" ×")[0], label=label, count=n,
              chips=m_chips[:3], members=evid if isinstance(evid, list) else [], region=prim, row=4,
              evidence=evid if isinstance(evid, list) else [evid], finding=[], mgmt=True,
              reason="관리 서비스는 흐름 축 밖 별도 블록(§S4)")
    # 5) 연결선(증거 관계만)
    node_of = {}
    for n in nodes:
        for m in n.get("members") or []:
            node_of[m] = n["id"]
    for e in f.ingress:
        tgt = node_of.get(lid(e["resourceId"]))
        if tgt:
            edges.append({"from": "x:internet", "to": tgt, "dir": "in", "label": "Https" if "container_app" in e["endpointType"] else "",
                          "style": "solid", "evidence": e["endpointId"]})
    covered = []  # 묶음 선·배지로 대신 표현한 관계(A11 에서 그린 것으로 센다)
    pub_by_vnet = defaultdict(set)
    for r in f.res:
        if rtype(r) == "microsoft.compute/virtualmachines" and f.has_public_ip(r) and node_of.get(r):
            sid = f.subnet_of(r)
            v = lid((f.subnets.get(sid) or {}).get("vnetId"))
            if ("v:" + v) in {c["id"] for c in containers}:
                pub_by_vnet["v:" + v].add(r)
                covered.append(("x:internet", node_of[r]))
            else:
                edges.append({"from": "x:internet", "to": node_of[r], "dir": "in", "label": "Public IP", "style": "solid",
                              "evidence": "public_ip_assigned_to"})
    for v, vms in sorted(pub_by_vnet.items()):  # VM 마다 선을 긋지 않고 VNet 경계로 1선 + 'Public IP VM N대'
        edges.append({"from": "x:internet", "to": v, "dir": "in", "label": f"Public IP VM {len(vms)}대", "style": "solid",
                      "evidence": "public_ip_assigned_to", "label_full": True})
    # AppGW: Internet → AppGW(공용 IP·리스너) → 백엔드 VM(풀 주소=NIC 사설 IP 근거)
    for agw, lst in f.agw_backends.items():
        a = node_of.get(agw)
        if not a:
            continue
        if f.has_public_ip(agw) and not any(e["to"] == a and e["from"] == "x:internet" for e in edges):
            n_l = len(next((g.get("listeners") or [] for g in f.c.get("applicationGatewayConfiguration") or [] if lid(g["id"]) == agw), []))
            edges.append({"from": "x:internet", "to": a, "dir": "in", "label": f"Https · 리스너 {n_l}" if n_l else "Https",
                          "style": "solid", "evidence": "public_ip_assigned_to + listeners"})
        for b in sorted({node_of.get(ow) for _, ow in lst if node_of.get(ow)}):
            pools = sorted({pn for pn, ow in lst if node_of.get(ow) == b})
            edges.append({"from": a, "to": b, "dir": "in", "label": "백엔드 " + str(len(pools)) + "풀", "style": "solid",
                          "evidence": "applicationGatewayConfiguration.backendAddressPools(IP→NIC)"})
    # NAT Gateway → 서브넷 아웃바운드
    for nat, subs in f.nat_subnets.items():
        a = node_of.get(nat)
        for sid in sorted(set(subs)):
            if a and ("s:" + sid) in {c["id"] for c in containers}:
                if diagram == "D1":
                    covered.append(("s:" + sid, a))  # D1: 서브넷 오른쪽 위 NAT 배지로 표시(§2.2 배지 문법)
                else:
                    edges.append({"from": "s:" + sid, "to": a, "dir": "out", "label": "NAT 아웃바운드", "style": "solid",
                                  "evidence": "nat_gateway_subnet_association"})
    for pe, tg in f.pe_target.items():
        a, b = node_of.get(pe), node_of.get(tg)
        if a and b:
            cross = f.region_of(pe) != f.region_of(tg) and f.region_of(tg) != "global"
            edges.append({"from": a, "to": b, "dir": "both", "label": "Private Link·교차리전" if cross else "Private Link",
                          "style": "solid", "evidence": "private_endpoint_target"})
        elif a and not b:
            edges.append({"from": a, "to": "x:out-of-scope", "dir": "both", "label": "범위 밖", "style": "dashed-assumed",
                          "evidence": "private_endpoint_target(대상 미수집)"})
    seen = set()
    edges = [e for e in edges if not ((e["from"], e["to"]) in seen or seen.add((e["from"], e["to"])))]
    # 6) TO-BE 오버레이(§6): AS-IS 개체는 그대로, 과제 개체만 추가/변경
    change = {}
    replaced = list(covered)  # TO-BE 에서 과제가 의도적으로 바꾼 AS-IS 관계(A11 에서 '그린 것'으로 센다)
    if state == "TO-BE":
        hub = None
        for tk in tks:
            b = tk["block"]
            if b == "NET-HUB":
                hub = "v:tobe-hub"
                containers.append({"id": hub, "level": "vnet", "parent": rid_of[prim], "label": "Hub VNet (신규)",
                                   "sub": "CIDR 설계 시 확정", "style": "new", "badges": [], "task": tk["id"]})
                containers.append({"id": "s:tobe-fw", "level": "subnet", "parent": hub, "label": "AzureFirewallSubnet",
                                   "sub": "", "row": 1, "style": "hatched-red", "badges": [], "task": tk["id"]})
                _card(nodes, "t:fw", parent="s:tobe-fw", type="microsoft.network/azurefirewalls", service="Azure Firewall",
                      label="Azure Firewall", count=1, chips=["0.0.0.0/0 → F/W", tk["id"]], members=[], region=prim, row=1,
                      evidence=[f"task:{tk['id']}"], finding=[], task=tk["id"], new=True, reason=f"{tk['id']} {tk['name']}")
        for tk in tks:
            b = tk["block"]
            par = hub or (f"g:{rid_of[prim]}:2")
            if b in ("NET-BASTION",) :
                containers.append({"id": "s:tobe-bastion", "level": "subnet", "parent": hub or rid_of[prim], "label": "AzureBastionSubnet",
                                   "sub": "", "row": 2, "style": "new", "badges": ["nsg"], "task": tk["id"]})
                _card(nodes, "t:bastion", parent="s:tobe-bastion", type="microsoft.network/bastionhosts", service="Bastion",
                      label="Bastion", count=1, chips=[tk["id"]], members=[], region=prim, row=2, evidence=[f"task:{tk['id']}"],
                      finding=[], task=tk["id"], new=True, reason=f"{tk['id']} {tk['name']}")
                for r in tk["resources"]:
                    change.setdefault(r, []).append((tk["id"], "Public IP 제거"))
            elif b == "NET-EDGE":
                gid = f"g:{rid_of[prim]}:1"
                if not any(c["id"] == gid for c in containers):
                    containers.append({"id": gid, "level": "group", "parent": rid_of[prim], "label": "외부 진입", "row": 1,
                                       "style": "normal", "badges": []})
                _card(nodes, "t:afd", parent=gid, type="microsoft.network/frontdoors", service="Front Door",
                      label="Front Door + WAF", count=1, chips=["OWASP Rules", tk["id"]], members=[], region=prim, row=1,
                      evidence=[f"task:{tk['id']}"], finding=[], task=tk["id"], new=True, reason=f"{tk['id']} {tk['name']}")
            elif b == "GW-APIM":
                gid = "s:tobe-apim"
                containers.append({"id": gid, "level": "subnet", "parent": hub or rid_of[prim], "label": "snet-apim",
                                   "sub": "", "row": 2, "style": "new", "badges": ["nsg"], "task": tk["id"]})
                _card(nodes, "t:apim", parent=gid, type="microsoft.apimanagement/service", service="API Management",
                      label="API Management", count=1, chips=["llm-token-limit", tk["id"]], members=[], region=prim, row=2,
                      evidence=[f"task:{tk['id']}"], finding=[], task=tk["id"], new=True, reason=f"{tk['id']} {tk['name']}")
            elif b == "NET-PE":
                for r in tk["resources"]:
                    change.setdefault(r, []).append((tk["id"], "공용 → 사설(PE)"))
            elif b == "NET-DNS":
                if any(c["id"] == "c:common" for c in containers):
                    for n in nodes:
                        if n["id"] == "n:" + sorted([r for r in f.res if rtype(r) == "microsoft.network/privatednszones"] or ["-"])[0]:
                            n.setdefault("tasks", []).append(tk["id"])
            elif b == "OPS-DIAG":
                for n in nodes:
                    if n["id"] == "m:mgmt/log-analytics":
                        n.setdefault("tasks", []).append(tk["id"])
                for r in tk["resources"]:
                    change.setdefault(r, []).append((tk["id"], "진단 → Log Analytics"))
            elif b == "AIS-FDY":
                sp = "v:tobe-aispoke"
                containers.append({"id": sp, "level": "vnet", "parent": rid_of[prim], "label": "AI Spoke VNet (신규)",
                                   "sub": "CIDR 설계 시 확정", "style": "new", "badges": [], "task": tk["id"]})
                containers.append({"id": "s:tobe-ai-app", "level": "subnet", "parent": sp, "label": "snet-ai-app", "sub": "",
                                   "row": 3, "style": "new", "badges": ["nsg"], "task": tk["id"]})
                containers.append({"id": "s:tobe-ai-pe", "level": "subnet", "parent": sp, "label": "snet-ai-pe", "sub": "",
                                   "row": 4, "style": "new", "badges": ["pe"], "task": tk["id"]})
                _card(nodes, "t:foundry", parent="s:tobe-ai-app", type="microsoft.cognitiveservices/accounts#aiservices",
                      service="Foundry", label="Foundry (신규)", count=1, chips=["Entra 인증", tk["id"]], members=[], region=prim,
                      row=3, evidence=[f"task:{tk['id']}"], finding=[], task=tk["id"], new=True, reason=f"{tk['id']} {tk['name']}")
                _card(nodes, "t:aipe", parent="s:tobe-ai-pe", type="microsoft.network/privateendpoints", service="Private Endpoint",
                      label="Private Endpoint (AI)", count=1, chips=[tk["id"]], members=[], region=prim, row=4,
                      evidence=[f"task:{tk['id']}"], finding=[], task=tk["id"], new=True, reason=f"{tk['id']} AI 사설 접근")
        for n in nodes:
            ch = [c for m in n.get("members") or [] for c in change.get(m, [])]
            if ch:
                n["changed"] = sorted({c[1] for c in ch})
                n.setdefault("tasks", [])
                n["tasks"] = sorted(set(n["tasks"]) | {c[0] for c in ch})
        # 신규 개체 연결
        ids = {n["id"] for n in nodes}
        if "t:afd" in ids:
            for e in list(edges):
                if e["from"] == "x:internet" and e["dir"] == "in" and not e["label"].startswith("Public IP"):
                    replaced.append(("x:internet", e["to"]))
                    e["from"] = "t:afd"
            edges.append({"from": "x:internet", "to": "t:afd", "dir": "in", "label": "Https", "style": "solid", "evidence": "task"})
        if "t:bastion" in ids:
            replaced += [(e["from"], e["to"]) for e in edges if e["label"].startswith("Public IP")]
            edges = [e for e in edges if not e["label"].startswith("Public IP")]
            # 대상 VM 이 여럿이면 VM 마다 선을 긋지 않고 VM 들이 속한 VNet 마다 1개(판독성, §3.3)
            vn = []
            for n in nodes:
                if "Public IP 제거" in (n.get("changed") or []):
                    v = next((c["parent"] for c in containers if c["id"] == n["parent"]), n["id"])
                    if v not in vn:
                        vn.append(v)
            for v in vn:
                edges.append({"from": "t:bastion", "to": v, "dir": "both", "label": "RDP/SSH", "style": "solid", "evidence": "task"})
        if "t:apim" in ids:
            for n in nodes:
                if n.get("type", "").startswith("microsoft.cognitiveservices/accounts"):
                    edges.append({"from": "t:apim", "to": n["id"], "dir": "both", "label": "AI API", "style": "solid", "evidence": "task"})
        if "t:foundry" in ids:
            edges.append({"from": "t:aipe", "to": "t:foundry", "dir": "both", "label": "Private Link", "style": "solid", "evidence": "task"})
        if "t:fw" in ids:
            edges.append({"from": "t:fw", "to": "x:internet", "dir": "out", "label": "Outbound", "style": "solid", "evidence": "task"})
    # 7) D2 전용: 아웃바운드(방화벽이 없으면 워크로드 → Internet 기본 경로)
    if diagram == "D2":
        fw = [n for n in nodes if n.get("type") == "microsoft.network/azurefirewalls"]
        work = [n for n in nodes if n.get("row") in (2, 3) and not n.get("mgmt") and n.get("type") not in GATE_TYPES
                and n.get("parent", "").startswith("s:")]
        # 선은 워크로드 '행(lane)' 단위로 하나씩(카드마다 그리면 판독 불가 — §3.3 흐름 축 단순화)
        lanes = sorted({w["row"] for w in work})
        nat_rows = {next((c.get("row") for c in containers if c["id"] == "s:" + sd), None) for v in f.nat_subnets.values() for sd in v}
        for ln in lanes:
            if not fw and ln in nat_rows and all(
                    any(("s:" + sd) == w["parent"] for v in f.nat_subnets.values() for sd in v) for w in work if w["row"] == ln):
                continue  # 이 띠의 서브넷은 모두 NAT 로 나간다(NAT 선이 이미 표현)
            if state == "AS-IS":
                # 방화벽 존재/부재는 UDR·실효 라우팅의 증거가 아니다. 미수집을 기본 Internet 경로로 바꾸지 않는다.
                subnet_ids = {w["parent"][2:] for w in work if w["row"] == ln}
                route_refs = sorted({str(f.subnets.get(sd, {}).get("routeTableId"))
                                     for sd in subnet_ids if f.subnets.get(sd, {}).get("routeTableId")})
                detail = "UDR 연결 참조: " + ", ".join(route_refs) if route_refs else "UDR·실효 경로 수집 근거 미확인"
                notes.append({"anchor": f"lane:{ln}", "text": detail + " / 실제 아웃바운드 경로 확정 불가",
                              "style": "yellow-note", "evidence": "configurations.subnets.routeTableId; effective routes not collected"})
                continue
            if fw:
                edges.append({"from": f"lane:{ln}", "to": fw[0]["id"], "dir": "out", "label": "UDR 0.0.0.0/0", "style": "solid",
                              "evidence": "task" if state == "TO-BE" else "azureFirewalls"})
            else:
                edges.append({"from": f"lane:{ln}", "to": "x:internet", "dir": "out", "label": "기본 아웃바운드", "style": "solid",
                              "evidence": "subnets.routeTableId=null · azureFirewalls=0"})
        if state == "AS-IS":
            pass  # 위의 수집 상태별 주석을 유지한다. UDR 없음·방화벽 강제경로를 추정하지 않는다.
        elif not fw:
            notes.append({"anchor": "x:internet", "text": "Azure Firewall 없음 / 서브넷 UDR 없음 → 기본 아웃바운드", "style": "yellow-note"})
        else:
            notes.append({"anchor": fw[0]["id"], "text": "외부망 Outbound Traffic / User Defined Route / 0.0.0.0/0 → Azure F/W", "style": "yellow-note"})
        if any(n.get("row") == 4 and not n.get("mgmt") for n in nodes) and 3 in lanes:
            edges.append({"from": "lane:3", "to": "lane:4", "dir": "both", "label": "데이터 접근", "style": "dashed-assumed",
                          "evidence": "추정 — 워크로드→데이터 트래픽은 관계 미수집"})
    # A11 기준: 엣지와 독립적으로 '수집 관계' 에서 그려야 할 쌍을 센다(카드 단위로 접은 뒤의 서로 다른 쌍)
    want = set()
    for agw, lst in f.agw_backends.items():
        if node_of.get(agw):
            if f.has_public_ip(agw):
                want.add(("x:internet", node_of[agw]))
            want |= {(node_of[agw], node_of[ow]) for _, ow in lst if node_of.get(ow)}
    for nat, subs in f.nat_subnets.items():
        if node_of.get(nat):
            want |= {("s:" + sd, node_of[nat]) for sd in subs if ("s:" + sd) in {c["id"] for c in containers}}
    for pe, tg in f.pe_target.items():
        if node_of.get(pe):
            want.add((node_of[pe], node_of.get(tg) or "x:out-of-scope"))
    for e in f.ingress:
        if node_of.get(lid(e["resourceId"])):
            want.add(("x:internet", node_of[lid(e["resourceId"])]))
    for r in f.res:
        if rtype(r) == "microsoft.compute/virtualmachines" and f.has_public_ip(r) and node_of.get(r):
            want.add(("x:internet", node_of[r]))
    drawable = sorted(want)
    # 가용 영역 사실: 그림에 올린 워크로드(VM·AppGW 등) 의 zones 값 집계(§2.3 바닥 띠)
    zf = Counter()
    for n in nodes:
        for mbr in n.get("members") or []:
            r = f.res.get(mbr)
            if not r or rtype(mbr) not in ("microsoft.compute/virtualmachines", "microsoft.network/applicationgateways",
                                           "microsoft.compute/virtualmachinescalesets", "microsoft.network/loadbalancers"):
                continue
            zs = [z for z in (r.get("zones") or []) if z]
            if not zs:
                for p_ in f.c.get("applicationGatewayConfiguration") or []:
                    if lid(p_["id"]) == mbr:
                        zs = [z for z in p_.get("zones") or [] if z]
            if zs:
                for z in zs:
                    zf[str(z)] += 1
            else:
                zf["none"] += 1
    zf = dict(zf)
    sub_ids = sorted({r.split("/")[2] for r in f.res if r.startswith("/subscriptions/") and len(r.split("/")) > 2})
    sub_id_short = sub_ids[0][:8] if sub_ids else "azure-sub"
    ten_list = _sec(ev, "tenant")
    ten_name = (ten_list[0].get("displayName") or ten_list[0].get("defaultDomain") or sub_id_short) if ten_list else sub_id_short
    vn_labels = [f"{c['label']}({c.get('sub','').split(' · ')[0]})" for c in containers if c["level"] == "vnet"]
    pe_total = len(f.pe_target)
    pub_dns_names = [(f.res[r].get("name") or leaf(r)) for r in f.res if rtype(r) == PUBLIC_DNS]
    agw_names = [(f.res[r].get("name") or leaf(r)) for r in f.res if rtype(r) == "microsoft.network/applicationgateways"]
    confirmed_lines = [
        f"네트워크 경계: {', '.join(vn_labels[:2]) if vn_labels else '단일 리전'} · 사설 엔드포인트(PE) {pe_total}개 연결",
        f"진입 및 도메인: {', '.join(pub_dns_names[:1] + agw_names[:1]) if (pub_dns_names or agw_names) else '내부 전용 구성'} · 리소스 총 {len(f.res)}개 수집",
    ]
    if state == "AS-IS":
        verify_lines = [f"{x['id']} {x['title']} (대상 {x['count']}건 · {x['principle']})" for x in fnd[:3]]
        if not verify_lines:
            verify_lines = ["수집된 구성 기준 주요 보안·네트워크 미흡 판정 없음"]
    else:
        verify_lines = [f"{t['id']} {t['name']} ({t.get('principle','')})" for t in tks[:3]]
        if not verify_lines:
            verify_lines = ["채택된 TO-BE 아키텍처 개선과제 기준 이행 검증"]
    blueprint_meta = {
        "tenant_name": ten_name,
        "subscription_short": sub_id_short,
        "public_dns": pub_dns_names[0] if pub_dns_names else "",
        "entry_gateway": agw_names[0] if agw_names else "",
        "confirmed_criteria": confirmed_lines,
        "verification_items": verify_lines,
        "status_badges": (["AS-IS BASELINE", "EVIDENCE VERIFIED"] if state == "AS-IS" else ["TARGET DESIGN", "TO-BE ARCHITECTURE"]),
        "status_note": "수집 증거 기반 실측본" if state == "AS-IS" else "개선과제 반영 목표 설계본",
    }
    title = f"{state} {'전체 인프라 상세도' if diagram == 'D1' else '네트워크 흐름도'}"
    lock = {"nodeCount": len(nodes), "edgeCount": len(edges), "regions": [r["name"] for r in regs]}
    return {"diagram": diagram, "state": state, "track": track,
            "canvas": {"ratio": "16:9", "title": title, "legend": ["IaaS", "PaaS", "SaaS"], "blueprint": blueprint_meta},
            "regions": regs, "external": external, "containers": containers, "nodes": nodes, "edges": edges,
            "notes": notes, "zones": [f"Zone{z}" for z in f.zones if z in ("1", "2", "3")],
            "findings": fnd, "tasks": tks if state == "TO-BE" else [], "lockedTruth": lock,
            "resourceIds": sorted(f.res), "drawableRelations": drawable, "zoneFacts": zf,
            "replacedRelations": sorted(set(replaced if state == "TO-BE" else covered))}


# 샘플 고유 명칭(A9). 샘플 2장을 판독해 뽑은 고유명(서비스 일반명 제외). 이 목록은 차단 전용이다.
SAMPLE_BLOCK = ["NOREU", "URECA", "UREC", "SAMSUNG", "AWS", "S3", "BLUE.NET", "GREEN.NET", "NEWS-API", "PEW1",
                "DB-JMP", "HQBASTION", "BDC", "NORTH EUROPE", "MEDALLION", "UNITY CATALOG", "BLOG POST API",
                "PRODUCT CATALOG API"]


def a9_hits(texts):
    out = []
    for t in texts:
        u = (t or "").upper()
        for b in SAMPLE_BLOCK:
            if re.search(r"(?<![A-Z0-9])" + re.escape(b) + r"(?![A-Z0-9])", u):
                out.append(f"{b} in '{t[:60]}'")
    return out
