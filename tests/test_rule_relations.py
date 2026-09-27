"""POST /v1/recall/rule-relations 的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app
from app.gs1 import FNC1, parse_scan
from app.matcher import match_rules
from app.models import Rule

client = TestClient(app)


def make_gtin(body13: str) -> str:
    """由 13 位本体计算模 10 校验位,生成合法 14 位 GTIN。"""
    assert len(body13) == 13 and body13.isdigit()
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body13)))
    return body13 + str((10 - total % 10) % 10)


GTIN = make_gtin("0950600112345")
GTIN2 = make_gtin("0950600112346")


def post(rules):
    resp = client.post("/v1/recall/rule-relations", json={"rules": rules})
    assert resp.status_code == 200, resp.text
    return resp.json()


def rule(rid, gtin=GTIN, **kw):
    return {"id": rid, "gtin": gtin, **kw}


def assert_witness(scan, left_spec, right_spec):
    """凭证串必须合法,且同时命中左右两条规则。"""
    parsed = parse_scan(scan)
    rules = [
        Rule.model_validate({**left_spec, "id": "L"}),
        Rule.model_validate({**right_spec, "id": "R"}),
    ]
    assert match_rules(parsed, rules) == ["L", "R"]


# ---------- 不可能命中任何合法扫描 ----------

def test_unsatisfiable_rules_listed_in_input_order():
    rules = [
        rule("after", date_from="2100-01-01"),
        rule("ok"),
        rule("before", date_to="1999-12-31"),
        rule("after-too", date_from="2100-06-01", date_to="2101-06-01"),
    ]
    data = post(rules)
    assert data["unsatisfiable_rule_ids"] == ["after", "before", "after-too"]


def test_date_range_touching_2000_feasible():
    # 区间端点在合法全集内(闭区间),仍然可命中
    specs = [
        rule("edge1", date_from="1999-12-31", date_to="2000-01-01"),
        rule("edge2", date_from="1999-12-31", date_to="2000-01-01"),
    ]
    data = post(specs)
    assert data["unsatisfiable_rule_ids"] == []
    (pair,) = data["pairs"]
    assert pair["relation"] == "equivalent"
    assert "17000101" in pair["witness_scan"]
    assert_witness(pair["witness_scan"], specs[0], specs[1])


def test_unsatisfiable_rules_excluded_from_pairs():
    data = post([
        rule("dead1", gtin=GTIN, date_from="2100-01-01"),
        rule("dead2", gtin=GTIN, date_to="1999-01-01"),
    ])
    assert data["pairs"] == []


# ---------- 关系判定 ----------

def test_equivalent_rules():
    specs = [
        rule("A", batch="LOT1", date_from="2025-01-01", date_to="2025-12-31"),
        rule("B", batch="LOT1", date_from="2025-01-01", date_to="2025-12-31"),
    ]
    data = post(specs)
    (pair,) = data["pairs"]
    assert pair["relation"] == "equivalent"
    assert (pair["left_index"], pair["right_index"]) == (0, 1)
    assert (pair["left_id"], pair["right_id"]) == ("A", "B")
    assert_witness(pair["witness_scan"], specs[0], specs[1])


def test_left_contains_right_via_batch():
    specs = [rule("broad"), rule("narrow", batch="LOT1")]
    data = post(specs)
    (pair,) = data["pairs"]
    assert pair["relation"] == "left_contains_right"
    assert_witness(pair["witness_scan"], specs[0], specs[1])


def test_right_contains_left():
    specs = [rule("narrow", serial="SN9"), rule("broad")]
    data = post(specs)
    (pair,) = data["pairs"]
    assert pair["relation"] == "right_contains_left"
    assert_witness(pair["witness_scan"], specs[0], specs[1])


def test_left_contains_right_via_date_subset():
    specs = [
        rule("L", date_from="2025-01-01", date_to="2025-12-31"),
        rule("R", date_from="2025-06-01", date_to="2025-06-30"),
    ]
    data = post(specs)
    (pair,) = data["pairs"]
    assert pair["relation"] == "left_contains_right"
    # 日期取交集内最早一天
    assert "17250601" in pair["witness_scan"]
    assert_witness(pair["witness_scan"], specs[0], specs[1])


def test_partial_overlap_dates():
    specs = [
        rule("L", date_from="2025-01-01", date_to="2025-06-30"),
        rule("R", date_from="2025-06-01", date_to="2025-12-31"),
    ]
    data = post(specs)
    (pair,) = data["pairs"]
    assert pair["relation"] == "partial_overlap"
    assert "17250601" in pair["witness_scan"]
    assert_witness(pair["witness_scan"], specs[0], specs[1])


def test_partial_overlap_batch_and_serial_different_constraints():
    # 批号、序列号各约束一个维度:仍可同时满足,但集合互不包含
    specs = [rule("L", batch="LOT1"), rule("R", serial="SN9")]
    data = post(specs)
    (pair,) = data["pairs"]
    assert pair["relation"] == "partial_overlap"
    witness = pair["witness_scan"]
    assert_witness(witness, specs[0], specs[1])
    # 批号(变长)后仍有序列号 → 必须有 FNC1
    assert "10LOT1" + FNC1 + "21SN9" in witness


def test_disjoint_gtin_no_pair():
    data = post([rule("A", gtin=GTIN), rule("B", gtin=GTIN2)])
    assert data["pairs"] == []


def test_disjoint_batch_no_pair():
    data = post([rule("A", batch="LOT1"), rule("B", batch="LOT2")])
    assert data["pairs"] == []


def test_disjoint_serial_no_pair():
    data = post([rule("A", serial="SN1"), rule("B", serial="SN2")])
    assert data["pairs"] == []


def test_disjoint_date_no_pair():
    data = post([
        rule("A", date_from="2025-01-01", date_to="2025-06-30"),
        rule("B", date_from="2025-07-01", date_to="2025-12-31"),
    ])
    assert data["pairs"] == []


def test_closed_date_interval_endpoints_intersect():
    # 闭区间端点相接算相交,交集最早一天为 2025-06-30
    data = post([
        rule("A", date_from="2025-01-01", date_to="2025-06-30"),
        rule("B", date_from="2025-06-30", date_to="2025-12-31"),
    ])
    (pair,) = data["pairs"]
    assert pair["relation"] == "partial_overlap"
    assert "17250630" in pair["witness_scan"]


# ---------- 凭证扫描串 ----------

def test_witness_omits_unneeded_variable_fields():
    specs = [rule("A"), rule("B", date_from="2025-01-01", date_to="2025-12-31")]
    data = post(specs)
    witness = data["pairs"][0]["witness_scan"]
    assert witness == "01" + GTIN + "17250101"
    assert_witness(witness, specs[0], specs[1])


def test_witness_gtin_only_for_unconstrained_rules():
    witness = post([rule("A"), rule("B")])["pairs"][0]["witness_scan"]
    assert witness == "01" + GTIN


def test_witness_batch_only_no_trailing_fnc1():
    specs = [rule("A", batch="LOT1"), rule("B")]
    witness = post(specs)["pairs"][0]["witness_scan"]
    assert witness == "01" + GTIN + "10LOT1"
    assert not witness.endswith(FNC1)
    assert_witness(witness, specs[0], specs[1])


def test_witness_serial_only_no_fnc1_needed():
    specs = [rule("A"), rule("B", serial="SN9")]
    witness = post(specs)["pairs"][0]["witness_scan"]
    assert witness == "01" + GTIN + "21SN9"
    assert_witness(witness, specs[0], specs[1])


def test_witness_all_fields_order_and_fnc1():
    specs = [
        rule("A", batch="LOT1", date_from="2025-01-01", date_to="2025-12-31"),
        rule("B", serial="SN9", date_from="2025-06-01"),
    ]
    data = post(specs)
    pair = data["pairs"][0]
    assert pair["relation"] == "partial_overlap"
    witness = pair["witness_scan"]
    assert witness == "01" + GTIN + "17250601" + "10LOT1" + FNC1 + "21SN9"
    assert_witness(witness, specs[0], specs[1])


def test_witness_for_single_sided_date():
    # 一侧单边区间、一侧不约束日期:无约束一侧集合更宽,凭证取单边端点
    specs = [rule("A"), rule("B", date_from="2026-05-01")]
    data = post(specs)
    pair = data["pairs"][0]
    assert pair["relation"] == "left_contains_right"
    assert "17260501" in pair["witness_scan"]
    assert_witness(pair["witness_scan"], specs[0], specs[1])


# ---------- 排序 ----------

def test_pairs_sorted_by_indices():
    rules = [
        rule("R0", batch="LOT1"),
        rule("R1", batch="LOT1"),
        rule("R2", batch="LOT1"),
        rule("R3", gtin=GTIN2),  # 与其他 GTIN 不相交
    ]
    data = post(rules)
    indices = [(p["left_index"], p["right_index"]) for p in data["pairs"]]
    assert indices == [(0, 1), (0, 2), (1, 2)]


# ---------- 请求体验证(422) ----------

def _expect_422(payload):
    resp = client.post("/v1/recall/rule-relations", json=payload)
    assert resp.status_code == 422, resp.text


def test_validation_rule_count_bounds():
    _expect_422({"rules": [rule("R1")]})
    _expect_422({"rules": []})
    _expect_422({"rules": [rule(f"R{i}") for i in range(101)]})


def test_validation_rule_ids_unique():
    _expect_422({"rules": [rule("R1"), rule("R1")]})


def test_validation_rule_fields_reused():
    bad = GTIN[:-1] + str((int(GTIN[-1]) + 1) % 10)
    _expect_422({"rules": [rule("R1", gtin=bad), rule("R2")]})
    _expect_422({"rules": [rule("R1", date_from="2026-01-01", date_to="2025-01-01"), rule("R2")]})
    _expect_422({"rules": [rule("R1"), rule("R2", extra=1)]})
