"""接口与规则版本化行为测试。"""


def make_payload(**kw):
    base = {
        "batch_no": "B20261005-001",
        "region": "default",
        "weight_kg": 100,
        "materials": [{"category": "PET", "contamination_kg": 3}],
        "operator": "tester",
    }
    base.update(kw)
    return base


def fields(resp):
    return {e["field"] for e in resp.json()["errors"]}


# ------------------------------------------------------------------ 正常判定
def test_create_assessment_ok(client):
    r = client.post("/api/assessments", json=make_payload())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["rule_version"] == "v1.0"
    assert d["level"]["code"] == "B"            # 3% 污染率
    assert d["conclusion"] == "降级接收"
    assert d["total_fee"] == 3.6                # 3kg * 1.2 * 1.0
    assert d["currency"] == "CNY"
    assert len(d["fee_lines"]) == 1
    assert any("规则版本" in e for e in d["evidence"])


def test_clean_batch_no_fee(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "PET", "contamination_kg": 1}]))
    assert r.status_code == 201
    d = r.json()
    assert d["level"]["code"] == "A"
    assert d["conclusion"] == "合格接收"
    assert d["total_fee"] == 0


def test_reject_batch(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "其他", "contamination_kg": 50}]))
    assert r.status_code == 201
    assert r.json()["conclusion"] == "拒收"


# ------------------------------------------------------------------ 字段校验
def test_negative_weight_rejected(client):
    r = client.post("/api/assessments", json=make_payload(weight_kg=-5))
    assert r.status_code == 422
    assert "weight_kg" in fields(r)


def test_zero_weight_rejected(client):
    r = client.post("/api/assessments", json=make_payload(weight_kg=0))
    assert r.status_code == 422
    assert "weight_kg" in fields(r)


def test_negative_contamination_rejected(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "PET", "contamination_kg": -1}]))
    assert r.status_code == 422
    assert "materials[0].contamination_kg" in fields(r)


def test_single_contamination_exceeds_weight(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "PET", "contamination_kg": 150}]))
    assert r.status_code == 422
    assert "materials[0].contamination_kg" in fields(r)


def test_total_contamination_exceeds_weight(client):
    r = client.post("/api/assessments", json=make_payload(materials=[
        {"category": "PET", "contamination_kg": 60},
        {"category": "纸类", "contamination_kg": 60},
    ]))
    assert r.status_code == 422
    assert "materials" in fields(r)


def test_unknown_category_rejected(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "锂电池", "contamination_kg": 1}]))
    assert r.status_code == 422
    assert "materials[0].category" in fields(r)
    assert "未知物料类别" in r.json()["errors"][0]["message"]


def test_unknown_region_rejected(client):
    r = client.post("/api/assessments", json=make_payload(region="不存在区"))
    assert r.status_code == 422
    assert "region" in fields(r)


def test_empty_materials_rejected(client):
    r = client.post("/api/assessments", json=make_payload(materials=[]))
    assert r.status_code == 422


# -------------------------------------------------------------- 规则版本固定
def test_history_pinned_after_rule_switch(client):
    # 切换前出具：v1.0，PET 3kg/100kg → B 级，扣费 3.6
    r1 = client.post("/api/assessments", json=make_payload())
    assert r1.status_code == 201
    aid = r1.json()["id"]
    assert r1.json()["rule_version"] == "v1.0"
    assert r1.json()["total_fee"] == 3.6

    # 切换到 v1.1（费率更高、阈值更严）
    r = client.post("/api/rules/default/activate", json={"version": "v1.1"})
    assert r.status_code == 200

    # 新判定使用 v1.1：3kg * 1.5 = 4.5
    r2 = client.post("/api/assessments", json=make_payload(batch_no="B002"))
    assert r2.json()["rule_version"] == "v1.1"
    assert r2.json()["total_fee"] == 4.5

    # 历史判定单保持出具时的版本与结论，不被改写
    old = client.get(f"/api/assessments/{aid}")
    assert old.status_code == 200
    assert old.json()["rule_version"] == "v1.0"
    assert old.json()["total_fee"] == 3.6
    assert old.json()["conclusion"] == "降级接收"


def test_explicit_rule_version(client):
    r = client.post("/api/assessments",
                    json=make_payload(rule_version="v1.1"))
    assert r.status_code == 201
    assert r.json()["rule_version"] == "v1.1"


def test_activate_unknown_version_404(client):
    r = client.post("/api/rules/default/activate", json={"version": "v9.9"})
    assert r.status_code == 404


def test_create_and_activate_new_rule(client):
    new_rule = {
        "region": "华南",
        "version": "v1.0",
        "activate": True,
        "categories": {"PET": {"fee_per_kg": 1.0, "desc": "PET"}},
        "levels": [
            {"code": "A", "name": "合格", "max_ratio": 0.05,
             "conclusion": "合格接收", "fee_multiplier": 0},
            {"code": "B", "name": "超标", "max_ratio": 1.0,
             "conclusion": "拒收", "fee_multiplier": 1.0},
        ],
    }
    r = client.post("/api/rules", json=new_rule)
    assert r.status_code == 201, r.text
    r = client.post("/api/assessments", json=make_payload(
        region="华南", materials=[{"category": "PET", "contamination_kg": 10}]))
    assert r.status_code == 201
    assert r.json()["conclusion"] == "拒收"


def test_duplicate_rule_version_conflict(client):
    rule = {
        "region": "default", "version": "v1.0",
        "categories": {"PET": {"fee_per_kg": 1.0}},
        "levels": [{"code": "A", "name": "合格", "max_ratio": 1.0,
                    "conclusion": "合格接收", "fee_multiplier": 0}],
    }
    r = client.post("/api/rules", json=rule)
    assert r.status_code == 409


# ------------------------------------------------------------------ 查询导出
def test_query_and_export(client):
    client.post("/api/assessments", json=make_payload())
    client.post("/api/assessments", json=make_payload(
        batch_no="B-HD-01", region="华东",
        materials=[{"category": "厨余", "contamination_kg": 10}]))

    r = client.get("/api/assessments", params={"region": "华东"})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1
    assert r.json()["items"][0]["batch_no"] == "B-HD-01"

    r = client.get("/api/assessments", params={"batch_no": "B20261005"})
    assert len(r.json()["items"]) == 1

    r = client.get("/api/export/assessments.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "B20261005-001" in r.text and "B-HD-01" in r.text


def test_audit_log_and_export(client):
    client.post("/api/assessments", json=make_payload())
    client.post("/api/rules/default/activate",
                json={"version": "v1.1", "actor": "admin"})

    r = client.get("/api/audit")
    assert r.status_code == 200
    actions = [i["action"] for i in r.json()["items"]]
    assert "assessment.created" in actions
    assert "rule.activated" in actions

    r = client.get("/api/audit", params={"entity": "rule"})
    assert all(i["entity"] == "rule" for i in r.json()["items"])

    r = client.get("/api/export/audit.csv")
    assert r.status_code == 200
    assert "rule.activated" in r.text


def test_assessment_not_found(client):
    r = client.get("/api/assessments/NOPE")
    assert r.status_code == 404
