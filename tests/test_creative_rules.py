"""Creative Check rules: declaration file, scoring boundaries, extraction limits, context and matching."""

from __future__ import annotations

import pytest
import yaml

from creative_helpers import (ACCOUNT2, K_AD1, K_AD3, K_CR1, K_CR3, RULES_PATH, build_candidate, cases, registry,
                              rule_ids, rules, run_case)
from rein.creative import (Context, band_for, check_creative, creative_view, diff_views, extract_claims, parse_creative_rules,
                           prohibited_from_policy, register_original, resolve_context, resolve_original, score,
                           view_from_raw)
from rein.policy import PolicyError, load_policy
from conftest import POLICY_EXAMPLE


def raw_rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- scoring


@pytest.mark.parametrize("value,band", [(0, "low"), (24, "low"), (25, "medium"), (49, "medium"), (50, "high"),
                                        (100, "high")])
def test_band_thresholds_are_inclusive(value, band):
    assert band_for(value, rules()) == band


def test_weights_and_bands_come_from_the_declaration_file():
    r = rules()
    assert r.weights == {"critical": 50, "major": 20, "minor": 5, "info": 0}
    assert (r.band_medium, r.band_high, r.max_score) == (25, 50, 100)


def test_changing_declared_thresholds_changes_the_band():
    raw = raw_rules()
    raw["scoring"]["bands"] = {"medium": 20, "high": 60}
    r = parse_creative_rules(raw)
    assert band_for(20, r) == "medium" and band_for(55, r) == "medium" and band_for(60, r) == "high"


def test_score_is_capped_and_marks_it():
    dets = [{"id": f"d{i}", "rule_id": "x", "severity": "critical", "points": 50} for i in range(3)]
    total, band, bd = score(dets, rules())
    assert (total, band, bd["raw_total"], bd["capped"]) == (100, "high", 150, True)


def test_score_counts_kinds_and_numbers_not_only_whether_there_is_a_diff():
    # Same single changed field; different detections on top give different scores.
    plain = run_case({"candidate": {"id": "130000000000000490", "from": K_CR1,
                                    "link_data": {"message": "秋の栗デニッシュです。※写真はイメージです。"}}, "ad_id": K_AD1})
    claim = run_case({"candidate": {"id": "130000000000000491", "from": K_CR1,
                                    "link_data": {"message": "秋の栗デニッシュ、2倍の栗。※写真はイメージです。"}}, "ad_id": K_AD1})
    two = run_case({"candidate": {"id": "130000000000000492", "from": K_CR1,
                                  "link_data": {"message": "秋の栗デニッシュ、2倍の栗、満足度90%。※写真はイメージです。"}},
                    "ad_id": K_AD1})
    assert (plain["score"], claim["score"], two["score"]) == (5, 25, 45)


# --------------------------------------------------------------------------- declaration file validation


@pytest.mark.parametrize("mutate,message", [
    (lambda r: r.update(extra=1), "unknown keys"),
    (lambda r: r["claim_patterns"].append({"id": "bad", "kind": "numeric", "pattern": "("}), "invalid regex"),
    (lambda r: r["claim_patterns"].append({"id": "x", "kind": "vibes", "pattern": "a"}), "kind must be"),
    (lambda r: r["claim_patterns"].append(dict(r["claim_patterns"][0])), "duplicate claim pattern"),
    (lambda r: r["prohibited"][0].update(severity="fatal"), "severity must be"),
    (lambda r: r["prohibited"][0].update(terms=[]), "terms must be"),
    (lambda r: r["scoring"].update(bands={"medium": 60, "high": 50}), "medium <= high"),
    (lambda r: r["scoring"]["weights"].pop("info"), "cover exactly"),
    (lambda r: r["brands"]["example_bakery"].update(colour="red"), "only required_phrases"),
    (lambda r: r["substantiation"].update(claim_kinds=["numeric", "nope"]), "claim_kinds must be among"),
    (lambda r: r.update(text_fields=["body"]), "text_fields must be among"),
])
def test_invalid_declarations_are_rejected(mutate, message):
    raw = raw_rules()
    mutate(raw)
    with pytest.raises(PolicyError, match=message):
        parse_creative_rules(raw)


def test_patterns_live_in_the_file_not_in_code():
    raw = raw_rules()
    raw["claim_patterns"] = []
    r = parse_creative_rules(raw)
    result = run_case(next(c for c in cases() if c["id"] == "3a"))
    assert result["claims"]
    view = view_from_raw(build_candidate(next(c for c in cases() if c["id"] == "3a")["candidate"]))
    assert extract_claims(view, r) == []


# --------------------------------------------------------------------------- extraction limits / normalization


def test_rewording_evades_extraction():
    """Only declared patterns are found: '一番' alone is not a declared superlative."""
    case = {"candidate": {"id": "130000000000000493", "from": K_CR1,
                          "link_data": {"name": "地域で一番おいしい栗のデニッシュ"}}, "ad_id": K_AD1}
    result = run_case(case)
    assert [c for c in result["claims"] if c["kind"] == "superlative"] == []
    assert result["score"] == 5  # only the headline diff


def test_notation_only_difference_is_labelled_but_still_counted():
    r = rules()
    original = {"headline": "栗のデニッシュ No1"}
    candidate = {"headline": "栗のデニッシュ　Ｎｏ１"}
    (d,) = diff_views(original, candidate, r)
    assert d["kind"] == "notation_only" and d["severity"] == "minor"


def test_claim_spans_come_from_normalized_text():
    view = {"message": "満足度　９２．５％ アップ、送料無料"}
    claims = extract_claims(view, rules())
    assert ("numeric", "92.5% アップ") in [(c["kind"], c["text"]) for c in claims]
    assert ("price", "無料") in [(c["kind"], c["text"]) for c in claims]


def test_image_hash_is_part_of_the_view():
    view = view_from_raw(build_candidate({"id": "1", "from": K_CR1, "link_data": {}}))
    assert view["image_hash"] == "fixtureimg0401" and view["link"] == "https://shop.example.com/autumn/marron"


# --------------------------------------------------------------------------- prohibited lists


def test_phase1_prohibited_terms_are_reused():
    extra = prohibited_from_policy(load_policy(POLICY_EXAMPLE))
    assert [p.id for p in extra] == ["policy:banned-claims"] and "No.1" in extra[0].terms
    case = next(c for c in cases() if c["id"] == "7a")
    plain = run_case(case)
    with_policy = run_case(case, extra_prohibited=extra)
    assert "policy:banned-claims" not in rule_ids(plain)
    assert "policy:banned-claims" in rule_ids(with_policy)


def test_category_list_can_be_added_in_the_file():
    raw = raw_rules()
    raw["prohibited"].append({"id": "food-medical", "categories": ["food"], "terms": ["がんに効く"]})
    r = parse_creative_rules(raw)
    view = {"message": "がんに効くパン"}
    food = check_creative(view, creative_id="x", account_id=None, rules=r, original=None, resolution={"reason": "-"},
                          context=Context(("food",)))
    other = check_creative(view, creative_id="x", account_id=None, rules=r, original=None, resolution={"reason": "-"},
                           context=Context(("health_beauty",)))
    assert "food-medical" in rule_ids(food) and "food-medical" not in rule_ids(other)


def test_uncategorized_creative_gets_only_all_category_lists_and_says_so():
    view = {"message": "必ず痩せる 絶対"}
    result = check_creative(view, creative_id="x", account_id=None, rules=rules(), original=None,
                            resolution={"reason": "-"}, context=Context())
    assert "banned-absolute" in rule_ids(result) and "health-efficacy-guarantee" not in rule_ids(result)
    assert any("カテゴリ未設定" in n for n in result["notes"])


# --------------------------------------------------------------------------- context / registry / matching


def test_context_priority_creative_over_adset():
    ledger, snap = registry()
    attrs = {"creative": {K_CR3: {"category": "finance"}}, "adset": {"130000000000000202": {"category": "health_beauty",
                                                                                             "brand": "example_wellness"}}}
    ctx = resolve_context(K_CR3, snap, attrs)
    assert ctx.categories == ("finance",) and ctx.sources["category"] == "creative"
    assert ctx.brands == ("example_wellness",) and ctx.sources["brand"] == "adset"
    assert resolve_context(K_CR3, snap, {}).sources == {"category": "unset", "brand": "unset"}


def test_registered_by_is_declared_not_verified():
    ledger, snap = registry()
    o = ledger.list_originals()[0]
    assert o["registered_by"] == {"name": "広告主担当A（架空）", "source": "fixture", "verified": False}
    assert o["registered_at"] == "2026-10-28T02:00:00Z" and o["ad_ids"] == [K_AD1]


def test_register_by_ad_uses_its_current_creative():
    ledger, snap = registry(originals=False)
    oid = register_original(ledger, snap, ad_id=K_AD3, by="tester")
    assert ledger.get_original(oid)["creative_id"] == K_CR3


@pytest.mark.parametrize("memo", [[{"claim": "x"}], [{"claim": " ", "source": "y"}], ["text"]])
def test_substantiation_notes_need_claim_and_source(memo):
    ledger, snap = registry(originals=False)
    with pytest.raises(KeyError, match="substantiation"):
        register_original(ledger, snap, creative_id=K_CR1, by="tester", substantiation=memo)


def test_unknown_creative_cannot_be_registered():
    ledger, snap = registry(originals=False)
    with pytest.raises(KeyError):
        register_original(ledger, snap, creative_id="999", by="tester")


def test_two_candidate_originals_are_not_guessed():
    ledger, snap = registry()
    # Ad 301 also gets registered against original 402: its lineage is now ambiguous.
    from rein.creative import place_on_ad
    from rein.snapshot import normalize_entity
    o402 = ledger.latest_original_for(ACCOUNT2, "130000000000000402")
    ledger.conn.execute("UPDATE creative_originals SET ad_ids = ? WHERE id = ?", (f'["{K_AD1}"]', o402["id"]))
    raw = build_candidate({"id": "130000000000000494", "from": K_CR1, "link_data": {}})
    s2 = place_on_ad(snap, K_AD1, raw["id"], normalize_entity("creative", raw))
    original, res = resolve_original(ledger, ACCOUNT2, raw["id"], s2)
    assert original is None and "特定できない" in res["reason"] and len(res["candidates"]) == 2


def test_explicit_original_wins_and_unknown_id_is_an_error():
    ledger, snap = registry()
    o3 = ledger.latest_original_for(ACCOUNT2, K_CR3)
    result = run_case({"candidate": {"id": "130000000000000495", "from": K_CR1, "link_data": {}}, "ad_id": K_AD1},
                      ledger, snap, explicit_original_id=o3["id"])
    assert result["original_resolution"]["method"] == "explicit"
    assert result["link_change"]["original"] == "https://wellness.example.com/drink"
    with pytest.raises(KeyError):
        resolve_original(ledger, ACCOUNT2, "x", snap, explicit_original_id=999)


def test_check_does_not_change_the_original():
    ledger, snap = registry()
    before = ledger.list_originals()
    run_case(next(c for c in cases() if c["id"] == "2"), ledger, snap)
    assert ledger.list_originals() == before
    assert creative_view(snap.get("creative", K_CR1)) == before[0]["fields"]


def test_same_term_in_two_lists_counts_once():
    extra = prohibited_from_policy(load_policy(POLICY_EXAMPLE))
    result = run_case(next(c for c in cases() if c["id"] == "2"), extra_prohibited=extra)
    hits = [d for d in result["detections"] if d["type"] == "prohibited_term"]
    assert len(hits) == 1 and hits[0]["rule_id"] == "banned-absolute" and hits[0]["also_in"] == ["policy:banned-claims"]
    assert result["score"] == 55
