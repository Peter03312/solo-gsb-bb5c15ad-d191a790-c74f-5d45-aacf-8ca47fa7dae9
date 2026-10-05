"""接口与规则版本化行为测试。"""
import json


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


# ------------------------------------------------------------------ 修订链
def revise_payload(**kw):
    base = {
        "reason": "复称发现称重偏差",
        "operator": "李四",
        "weight_kg": 100,
        "materials": [{"category": "PET", "contamination_kg": 1}],
    }
    base.update(kw)
    return base


def test_revision_ok(client):
    aid = client.post("/api/assessments", json=make_payload()).json()["id"]
    r = client.post(f"/api/assessments/{aid}/revisions", json=revise_payload())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["id"] != aid
    assert d["batch_no"] == "B20261005-001"      # 沿用原单批次
    assert d["region"] == "default"              # 沿用原单区域
    assert d["rule_version"] == "v1.0"           # 沿用原单规则快照
    assert d["prev_id"] == aid
    assert d["root_id"] == aid
    assert d["revision_no"] == 2
    assert d["revision_reason"] == "复称发现称重偏差"
    assert d["operator"] == "李四"
    assert d["level"]["code"] == "A"             # 1kg/100kg = 1%
    assert d["total_fee"] == 0
    assert d["superseded"] is False
    # 原单被标记取代，内容与证据保持不变、仍可按编号查询
    old = client.get(f"/api/assessments/{aid}").json()
    assert old["superseded"] is True
    assert old["revision_no"] == 1
    assert old["total_fee"] == 3.6
    assert old["conclusion"] == "降级接收"
    assert any("规则版本" in e for e in old["evidence"])


def test_revision_uses_snapshot_not_active_rule(client):
    aid = client.post("/api/assessments", json=make_payload()).json()["id"]
    # 切换区域启用规则后再修订：复算仍固定为原单快照 v1.0
    client.post("/api/rules/default/activate", json={"version": "v1.1"})
    r = client.post(f"/api/assessments/{aid}/revisions", json=revise_payload(
        materials=[{"category": "PET", "contamination_kg": 3}]))
    assert r.status_code == 201
    d = r.json()
    assert d["rule_version"] == "v1.0"
    assert d["total_fee"] == 3.6                 # v1.0 费率 1.2；v1.1 应为 4.5
    assert d["conclusion"] == "降级接收"


def test_revision_requires_reason_and_operator(client):
    aid = client.post("/api/assessments", json=make_payload()).json()["id"]
    for kw in ({"reason": ""}, {"reason": "  "}, {"operator": ""},
               {"operator": "  "}):
        r = client.post(f"/api/assessments/{aid}/revisions",
                        json=revise_payload(**kw))
        assert r.status_code == 422, kw
    # 失败请求不产生单据和审计记录
    assert len(client.get("/api/assessments").json()["items"]) == 1
    actions = [i["action"] for i in client.get("/api/audit").json()["items"]]
    assert "assessment.revised" not in actions


def test_revision_invalid_fields_no_side_effects(client):
    aid = client.post("/api/assessments", json=make_payload()).json()["id"]
    before = client.get("/api/audit").json()["items"]
    r = client.post(f"/api/assessments/{aid}/revisions",
                    json=revise_payload(weight_kg=-1))
    assert r.status_code == 422
    assert "weight_kg" in fields(r)
    r = client.post(f"/api/assessments/{aid}/revisions", json=revise_payload(
        materials=[{"category": "锂电池", "contamination_kg": 1}]))
    assert r.status_code == 422
    assert "materials[0].category" in fields(r)
    # 不产生单据和审计记录，原单仍是链尾
    items = client.get("/api/assessments").json()["items"]
    assert len(items) == 1 and items[0]["superseded"] is False
    assert len(client.get("/api/audit").json()["items"]) == len(before)


def test_revision_unknown_id_404(client):
    r = client.post("/api/assessments/NOPE/revisions", json=revise_payload())
    assert r.status_code == 404
    r = client.get("/api/assessments/NOPE/revisions")
    assert r.status_code == 404


def test_revision_only_head_and_chain(client):
    aid = client.post("/api/assessments", json=make_payload()).json()["id"]
    r2 = client.post(f"/api/assessments/{aid}/revisions", json=revise_payload())
    assert r2.status_code == 201
    # 原单已非链尾，再次基于它修订 → 409 冲突
    r3 = client.post(f"/api/assessments/{aid}/revisions", json=revise_payload())
    assert r3.status_code == 409
    assert "已被更新的修订取代" in r3.json()["errors"][0]["message"]
    # 基于新链尾可以继续修订
    r4 = client.post(f"/api/assessments/{r2.json()['id']}/revisions",
                     json=revise_payload(weight_kg=90))
    assert r4.status_code == 201
    assert r4.json()["revision_no"] == 3
    assert r4.json()["root_id"] == aid
    assert r4.json()["prev_id"] == r2.json()["id"]
    # 链尾单被取代后同样返回 409
    assert client.post(f"/api/assessments/{r2.json()['id']}/revisions",
                       json=revise_payload()).status_code == 409


def test_revision_chain_query(client):
    aid = client.post("/api/assessments", json=make_payload()).json()["id"]
    r2 = client.post(f"/api/assessments/{aid}/revisions",
                     json=revise_payload()).json()
    r3 = client.post(f"/api/assessments/{r2['id']}/revisions",
                     json=revise_payload(weight_kg=80)).json()
    # 按原单号查询当前有效结论与完整修订链
    r = client.get(f"/api/assessments/{aid}/revisions")
    assert r.status_code == 200
    chain = r.json()
    assert chain["root_id"] == aid
    assert [c["revision_no"] for c in chain["revisions"]] == [1, 2, 3]
    assert [c["id"] for c in chain["revisions"]] == [aid, r2["id"], r3["id"]]
    assert chain["current"]["id"] == r3["id"]
    assert chain["current"]["weight_kg"] == 80
    assert chain["current"]["superseded"] is False
    # 按链中任一单号查询得到同一条链
    r = client.get(f"/api/assessments/{r2['id']}/revisions")
    assert r.json()["current"]["id"] == r3["id"]
    assert len(r.json()["revisions"]) == 3


def test_revision_audit(client):
    aid = client.post("/api/assessments", json=make_payload()).json()["id"]
    client.post(f"/api/assessments/{aid}/revisions",
                json=revise_payload(reason="杂质复测", operator="王五"))
    items = client.get("/api/audit", params={"entity": "assessment"}).json()["items"]
    rev = [i for i in items if i["action"] == "assessment.revised"]
    assert len(rev) == 1
    assert rev[0]["actor"] == "王五"
    detail = json.loads(rev[0]["detail"])
    assert detail["reason"] == "杂质复测"
    assert detail["prev_id"] == aid
    assert detail["revision_no"] == 2


def test_revision_concurrent_conflict(client):
    import threading
    aid = client.post("/api/assessments", json=make_payload()).json()["id"]
    results = []

    def rev():
        r = client.post(f"/api/assessments/{aid}/revisions", json=revise_payload())
        results.append(r.status_code)

    threads = [threading.Thread(target=rev) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409]
    # 修订链只前进一格，链尾唯一
    chain = client.get(f"/api/assessments/{aid}/revisions").json()
    assert len(chain["revisions"]) == 2
    assert chain["current"]["revision_no"] == 2


def test_revision_list_and_csv_compatible(client):
    aid = client.post("/api/assessments", json=make_payload()).json()["id"]
    client.post(f"/api/assessments/{aid}/revisions", json=revise_payload())
    items = client.get("/api/assessments").json()["items"]
    assert len(items) == 2
    by_id = {i["id"]: i for i in items}
    assert by_id[aid]["superseded"] is True
    assert by_id[aid]["revision_no"] == 1
    assert by_id[aid]["root_id"] == aid
    assert by_id[aid]["prev_id"] is None
    head = [i for i in items if not i["superseded"]][0]
    assert head["revision_no"] == 2 and head["prev_id"] == aid
    # 既有字段与含义保持不变
    for col in ("id", "batch_no", "region", "weight_kg", "conclusion",
                    "total_fee", "currency", "rule_version", "operator",
                    "created_at"):
        assert col in items[0]
    r = client.get("/api/export/assessments.csv")
    assert r.status_code == 200
    header = r.text.splitlines()[0]
    for col in ("id", "batch_no", "conclusion", "total_fee",
                "root_id", "prev_id", "revision_no", "superseded"):
        assert col in header
