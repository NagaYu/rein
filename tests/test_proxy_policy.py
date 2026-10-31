"""Pre-execution policy semantics and boundaries."""

from __future__ import annotations

import pytest

from rein.policy import PolicyError, evaluate, parse_policy
from rein.proxy import ReinProxy

from helpers import change
from proxy_helpers import (ACCOUNT, AD2, AS1, AS2, CR2, PROXY_POLICY, call, decision_of, make_proxy, policy_with)

WED_1400 = "2026-10-28T05:00:00Z"


def budget(value, entity_id=AS1, clock=WED_1400, policy=None):
    proxy, mock, _ = make_proxy(clock, policy=policy)
    return decision_of(call(proxy, "ads_update_entity", {"entity_id": entity_id, "fields": {"daily_budget": value}})), mock


# ------------------------------------------------------------------ increase ratio (5000 current, max +50%)


@pytest.mark.parametrize("value,expected", [
    (7499, "forwarded"),
    (7500, "forwarded"),          # exactly +50% is allowed
    (7501, "require_approval"),   # just over
    (3000, "forwarded"),          # decreases are not limited by the ratio rule
    ("7500", "forwarded"),        # numeric strings are accepted like the Graph API returns them
])
def test_increase_ratio_boundary(value, expected):
    assert budget(value)[0] == expected


def test_ratio_with_fractional_percent():
    p = policy_with(**{"budget-step-up": {"max_increase_pct": 12.5}})
    assert budget(5625, policy=p)[0] == "forwarded"        # exactly +12.5%
    assert budget(5626, policy=p)[0] == "require_approval"


# ------------------------------------------------------------------ absolute cap


@pytest.mark.parametrize("value,expected", [(7000, "forwarded"), (7001, "deny")])
def test_cap_boundary(value, expected):
    p = policy_with(**{"adset-daily-cap": {"max": 7000}})
    assert budget(value, policy=p)[0] == expected


def test_cap_beats_ratio_and_window():
    # 50000 is over the cap and over +50%, at night: deny wins over require_approval.
    decision, mock = budget(50000, clock="2026-10-28T18:00:00Z")
    assert decision == "deny" and mock.write_log == []


# ------------------------------------------------------------------ change window (Mon-Fri 09:00-19:00 JST)


@pytest.mark.parametrize("clock,expected", [
    ("2026-10-28T00:00:00Z", "forwarded"),          # Wed 09:00:00 JST, start inclusive
    ("2026-10-27T23:59:59Z", "require_approval"),   # Wed 08:59:59 JST
    ("2026-10-28T09:59:59Z", "forwarded"),          # Wed 18:59:59 JST
    ("2026-10-28T10:00:00Z", "require_approval"),   # Wed 19:00:00 JST, end exclusive
    ("2026-10-31T03:00:00Z", "require_approval"),   # Sat 12:00 JST
])
def test_window_boundaries(clock, expected):
    proxy, mock, _ = make_proxy(clock)
    result = call(proxy, "ads_update_entity", {"entity_id": AS2, "fields": {"status": "PAUSED"}})
    assert decision_of(result) == expected
    if expected == "require_approval":
        reason = result.structuredContent["rein"]["reasons"][0]["reason"]
        assert "proxy.requested_at" in reason


def test_activation_goes_through_window_rule():
    proxy, _, _ = make_proxy("2026-10-28T18:00:00Z")
    assert decision_of(call(proxy, "ads_activate_entity", {"entity_id": AS2})) == "require_approval"
    proxy, mock, _ = make_proxy(WED_1400)
    assert decision_of(call(proxy, "ads_activate_entity", {"entity_id": AS2})) == "forwarded"


# ------------------------------------------------------------------ creative swap / text


def _creative(mock, link, message):
    return mock.call("ads_create_creative", {"account_id": ACCOUNT, "name": "c", "link": link, "message": message,
                                             "headline": "さつまいもブレッド", "description": "Example Bakery（架空）",
                                             "call_to_action": "SHOP_NOW"})["id"]


def test_swap_to_creative_with_same_link_and_text_is_allowed():
    proxy, mock, _ = make_proxy()
    same = _creative(mock, "https://shop.example.com/autumn/satsumaimo", "ほくほくのさつまいもを練り込んだ秋のパン。")
    result = call(proxy, "ads_update_entity", {"entity_id": AD2, "fields": {"creative_id": same}})
    assert decision_of(result) == "forwarded"
    assert mock.find("ad", AD2)["creative"] == {"id": same}


def test_swap_with_same_link_but_banned_text_is_denied_by_terms_only():
    proxy, mock, _ = make_proxy()
    bad = _creative(mock, "https://shop.example.com/autumn/satsumaimo", "業界ＮＯ．１のさつまいもパン")
    result = call(proxy, "ads_update_entity", {"entity_id": AD2, "fields": {"creative_id": bad}})
    assert decision_of(result) == "deny"
    assert {r["rule_id"] for r in result.structuredContent["rein"]["reasons"]} == {"banned-claims"}


def test_create_ad_with_banned_creative_is_denied():
    proxy, mock, _ = make_proxy()
    bad = _creative(mock, "https://shop.example.com/x", "絶対おいしい")
    result = call(proxy, "ads_create_ad", {"account_id": ACCOUNT, "adset_id": AS1, "name": "a", "creative_id": bad})
    assert decision_of(result) == "deny"


def test_create_clean_creative_and_ad_set_are_allowed():
    proxy, mock, _ = make_proxy()
    r = call(proxy, "ads_create_creative", {"account_id": ACCOUNT, "name": "ok", "link": "https://shop.example.com/x",
                                            "message": "焼きたてのパン"})
    assert decision_of(r) == "forwarded"
    r = call(proxy, "ads_create_ad_set", {"account_id": ACCOUNT, "campaign_id": "120000000000000101", "name": "s",
                                          "optimization_goal": "REACH", "billing_event": "IMPRESSIONS",
                                          "daily_budget": 20000})
    assert decision_of(r) == "forwarded"
    r = call(proxy, "ads_create_ad_set", {"account_id": ACCOUNT, "campaign_id": "120000000000000101", "name": "s2",
                                          "optimization_goal": "REACH", "billing_event": "IMPRESSIONS",
                                          "daily_budget": 20001})
    assert decision_of(r) == "deny"


def test_campaign_create_over_cap_is_denied_not_queued():
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_create_campaign", {"account_id": ACCOUNT, "name": "x", "objective": "OUTCOME_SALES",
                                            "daily_budget": 60001})
    assert decision_of(r) == "deny"
    assert proxy.ledger.list_approvals() == []


# ------------------------------------------------------------------ declarations


def test_on_proxy_override_and_validation():
    p = policy_with(**{"budget-step-up": {"on_proxy": "deny"}})
    assert budget(9000, policy=p)[0] == "deny"
    with pytest.raises(PolicyError):
        policy_with(**{"budget-step-up": {"on_proxy": "allow"}})  # "allow" overrides are not a thing


def test_proxy_only_rules_do_not_affect_watch():
    from rein.policy import load_policy
    p = load_policy(PROXY_POLICY)
    watch = evaluate(change(before=5000, after=9000), p)            # +80%, under cap
    assert watch.findings == []
    proxy_scope = evaluate(change(before=5000, after=9000), p, scope="proxy")
    assert [f.rule_id for f in proxy_scope.findings] == ["budget-step-up"]
    assert proxy_scope.findings[0].on_proxy == "require_approval"


def test_ttl_is_configurable():
    import yaml
    raw = yaml.safe_load(PROXY_POLICY.read_text(encoding="utf-8"))
    raw["proxy"]["approval_ttl_hours"] = 2
    proxy, _, _ = make_proxy(policy=parse_policy(raw))
    r = call(proxy, "ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 9000}})
    assert r.structuredContent["rein"]["expires_at"] == "2026-10-28T07:00:00Z"


@pytest.mark.parametrize("bad", [
    {"proxy": {"approval_ttl_hours": 0}},
    {"proxy": {"approval_ttl_hours": -1}},
    {"proxy": {"unknown": 1}},
    {"rules": [{"id": "r", "type": "budget_increase_ratio", "entity_types": ["ad"], "fields": ["daily_budget"], "max_increase_pct": 1}]},
    {"rules": [{"id": "r", "type": "budget_increase_ratio", "entity_types": ["adset"], "fields": ["daily_budget"], "max_increase_pct": -1}]},
    {"rules": [{"id": "r", "type": "approval_required", "change_types": ["exploded"]}]},
    {"rules": [{"id": "r", "type": "budget_cap", "entity_types": ["adset"], "fields": ["daily_budget"], "max": 1, "scopes": ["somewhere"]}]},
])
def test_invalid_proxy_declarations(bad):
    with pytest.raises(PolicyError):
        parse_policy(bad)


def test_read_tools_must_not_include_write_tools():
    from rein.ledger import Ledger
    from proxy_helpers import InProcessDownstream, MockAdsMcp, BASE
    p = parse_policy({"rules": [], "proxy": {"read_tools": ["ads_get_ad_entities", "ads_update_entity"]}})
    with pytest.raises(PolicyError):
        ReinProxy(p, Ledger(":memory:"), InProcessDownstream(MockAdsMcp(BASE)))
