"""Phase 3 Creative Check scenarios 1-9 (fixtures/creative/cases.json)."""

from __future__ import annotations

import pytest

from creative_helpers import ACCOUNT2, K_CR1, K_CR2, cases, registry, rule_ids, run_case
from rein.creative import EXTRACTION_LIMIT, NOT_LEGAL, register_original

CASES = cases()


def by_id(case_id: str) -> dict:
    return next(c for c in CASES if c["id"] == case_id)


def test_every_scenario_1_to_9_has_a_case():
    assert {c["scenario"] for c in CASES} == set(range(1, 10))


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case_matches_expectation(case):
    exp = case["expected"]
    result = run_case(case)
    assert result["status"] == exp["status"]
    assert result["score"] == exp["score"]
    assert result["band"] == exp["band"]
    if "detections" in exp:
        assert rule_ids(result) == sorted(exp["detections"])
    if "types" in exp:
        assert sorted(d["type"] for d in result["detections"]) == sorted(exp["types"])
    for absent in exp.get("absent", []):
        assert absent not in rule_ids(result)
    if "resolution" in exp:
        assert result["original_resolution"]["method"] == exp["resolution"]
    if "raw_total" in exp:
        assert result["score_breakdown"]["raw_total"] == exp["raw_total"]
        assert result["score_breakdown"]["capped"] is True
    if exp.get("link_changed"):
        assert result["link_change"] is not None
    for claim in exp.get("claims", []):
        assert claim in [c["text"] for c in result["claims"]]
    # Every result says what it is not.
    assert NOT_LEGAL in result["notes"] and EXTRACTION_LIMIT in result["notes"]


def test_score_breakdown_adds_up_to_the_score():
    for case in CASES:
        result = run_case(case)
        if result["score"] is None:
            continue
        bd = result["score_breakdown"]
        assert sum(i["points"] for i in bd["items"]) == bd["raw_total"]
        assert min(bd["raw_total"], bd["max"]) == result["score"]
        assert [i["detection"] for i in bd["items"]] == [d["id"] for d in result["detections"]]
        assert sum(v["points"] for v in bd["by_severity"].values()) == bd["raw_total"]


def test_1_identical_has_no_diff_and_lowest_band():
    result = run_case(by_id("1"))
    assert result["diff"] == [] and result["detections"] == [] and result["claims"] == []
    assert result["original"]["creative_id"] == K_CR1
    assert result["original_resolution"]["method"] == "ad_lineage"


def test_2_prohibited_term_is_critical():
    result = run_case(by_id("2"))
    hit = next(d for d in result["detections"] if d["type"] == "prohibited_term")
    assert hit["severity"] == "critical" and hit["term"] == "絶対" and hit["field"] == "message"


def test_3_same_claim_is_flagged_without_note_and_lowered_with_one():
    without = run_case(by_id("3a"))
    with_note = run_case(by_id("3b"))
    need = next(d for d in without["detections"] if d["type"] == "needs_substantiation")
    ok = next(d for d in with_note["detections"] if d["type"] == "substantiated")
    assert need["claim"] == ok["claim"] == "200%アップ"
    assert "根拠未記録" in need["message"] and need["points"] == 20
    assert ok["points"] == 0 and "EV-0001" in ok["message"]
    claim = next(c for c in without["claims"] if c["kind"] == "numeric")
    assert claim["new"] is True and claim["substantiation"] is None


def test_3_adding_a_note_by_re_registering_the_original_lowers_the_detection():
    ledger, snap = registry()
    before = run_case(by_id("3a"), ledger, snap)
    register_original(ledger, snap, creative_id=K_CR1, by="広告主担当A（架空）",
                      substantiation=[{"claim": "昨年比 売上２００％アップ", "source": "資料 EV-0002（架空）"}])
    after = run_case(by_id("3a"), ledger, snap)
    assert before["score"] == 25 and before["band"] == "medium"
    assert after["score"] == 5 and after["band"] == "low"
    assert after["original"]["id"] != before["original"]["id"]  # the latest registration wins


def test_4_link_change_is_the_top_difference():
    result = run_case(by_id("4"))
    assert result["link_change"] == {"original": "https://shop.example.com/autumn/marron",
                                     "candidate": "https://shop.example.net/autumn/marron", "kind": "content"}
    first = result["detections"][0]
    assert first["type"] == "link_changed" and first["severity"] == "critical" and "最重要" in first["message"]


def test_5_brand_rules():
    missing = run_case(by_id("5a"))
    banned = run_case(by_id("5b"))
    assert next(d for d in missing["detections"] if d["type"] == "brand_required_missing")["phrase"] == "※写真はイメージです"
    assert next(d for d in banned["detections"] if d["type"] == "brand_banned_word")["word"] == "訳あり"
    assert missing["context"]["brands"] == ["example_bakery"] and missing["context"]["sources"]["brand"] == "adset"


def test_6_category_rule_applies_only_to_its_category():
    health = run_case(by_id("6a"))
    food = run_case(by_id("6b"))
    assert health["context"]["categories"] == ["health_beauty"]
    assert food["context"]["categories"] == ["food"]
    assert "health-efficacy-guarantee" in rule_ids(health)
    assert "health-efficacy-guarantee" not in rule_ids(food)
    # The efficacy claim itself is still extracted and needs substantiation in both.
    assert "substantiation:efficacy" in rule_ids(health) and "substantiation:efficacy" in rule_ids(food)


def test_6_explicit_category_overrides_attributes():
    result = run_case(by_id("6b"), category="health_beauty")
    assert "health-efficacy-guarantee" in rule_ids(result)
    assert result["context"]["sources"]["category"] == "explicit"


def test_7_full_width_text_is_normalized_before_matching():
    case = by_id("7a")
    assert "no.1" not in case["candidate"]["link_data"]["name"].casefold()  # not findable without NFKC
    result = run_case(case)
    claim = next(c for c in result["claims"] if c["kind"] == "superlative")
    assert claim["text"] == "no.1" and claim["field"] == "headline"
    term = next(d for d in run_case(by_id("7b"))["detections"] if d["type"] == "prohibited_term")
    assert term["term"] == "guaranteed"


def test_8_no_original_is_classified_and_not_scored():
    result = run_case(by_id("8"))
    assert result["status"] == "no_original" and result["original"] is None
    assert result["score"] is None and result["band"] is None and result["score_breakdown"] is None
    assert result["notes"][0].startswith("原本なし")
    assert result["original_resolution"]["reason"] == "対応する承認済み原本が登録されていない"
    # Claims are still shown as material, without a score.
    assert [c["kind"] for c in result["claims"]] == ["numeric"]


def test_8_recorded_in_ledger_without_score():
    ledger, snap = registry()
    result = run_case(by_id("8"), ledger, snap)
    row = ledger.creative_checks()[-1]
    assert row["id"] == result["check_id"] and row["status"] == "no_original"
    assert row["score"] is None and row["band"] is None and row["account_id"] == ACCOUNT2


def test_9_bands_at_thresholds():
    assert run_case(by_id("9a"))["band"] == "medium"   # 25 exactly
    assert run_case(by_id("9b"))["band"] == "low"      # 20
    assert run_case(by_id("9c"))["band"] == "high"     # 50 exactly
    assert run_case(by_id("9d"))["band"] == "medium"   # 45


def test_original_with_memo_is_the_one_for_ad_302():
    result = run_case(by_id("3b"))
    assert result["original"]["creative_id"] == K_CR2
