"""Fail-closed behaviour: unknown tools, uninterpretable calls, missing current values."""

from __future__ import annotations

import pytest

from rein.policy import parse_policy
from rein.proxy import DownstreamResult, InProcessDownstream

from proxy_helpers import ACCOUNT, AD2, AS1, BASE, CR2, MockAdsMcp, call, decision_of, make_proxy

# ------------------------------------------------------------------ unknown tools


@pytest.mark.parametrize("tool", ["ads_delete_entity", "ads_get_new_report", "ads_boost_ig_post", "ADS_GET_AD_ENTITIES", ""])
def test_unknown_tools_are_denied(tool):
    proxy, mock, _ = make_proxy()
    result = call(proxy, tool, {"entity_id": AS1})
    assert decision_of(result) == "deny"
    assert result.structuredContent["rein"]["reasons"][0]["rule_id"] == "rein:unknown-tool"
    assert mock.write_log == []


def test_read_tool_not_declared_is_treated_as_write():
    policy = parse_policy({"rules": []})  # no proxy.read_tools at all
    proxy, _, _ = make_proxy(policy=policy)
    assert decision_of(call(proxy, "ads_get_ad_entities", {"entity_type": "adset"})) == "deny"


# ------------------------------------------------------------------ uninterpretable


@pytest.mark.parametrize("tool,args", [
    ("ads_update_entity", None),
    ("ads_update_entity", ["not", "an", "object"]),
    ("ads_update_entity", {"fields": {"daily_budget": 6000}}),
    ("ads_update_entity", {"entity_id": "abc", "fields": {"daily_budget": 6000}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": "daily_budget=6000"}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {"targeting": {"age_max": 65}}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {"status": "RUNNING"}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": -5}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 0}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": True}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 5500.5}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 6000}, "force": True}),
    ("ads_update_entity", {"entity_id": AS1, "entity_type": "creative", "fields": {"name": "x"}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {"creative_id": CR2}}),  # ad sets have no creative
    ("ads_create_campaign", {"account_id": ACCOUNT, "name": "x"}),
    ("ads_create_campaign", {"account_id": "100000000000001", "name": "x", "objective": "y"}),
    ("ads_create_creative", {"account_id": ACCOUNT, "name": "c", "link": "javascript:alert(1)", "message": "m"}),
    ("ads_create_ad_set", {"account_id": ACCOUNT, "campaign_id": "120000000000000101", "name": "s",
                           "optimization_goal": "REACH", "billing_event": "IMPRESSIONS", "targeting": "everyone"}),
])
def test_uninterpretable_calls_are_denied(tool, args):
    proxy, mock, _ = make_proxy()
    result = call(proxy, tool, args)
    assert decision_of(result) == "deny"
    [reason] = result.structuredContent["rein"]["reasons"]
    assert reason["rule_id"] == "rein:uninterpretable" and reason["reason"].startswith("解釈不能")
    assert mock.write_log == []


@pytest.mark.parametrize("tool,args", [
    ("ads_update_entity", {"entity_id": "999999", "fields": {"daily_budget": 6000}}),
    ("ads_update_entity", {"entity_id": AD2, "fields": {"creative_id": "999999"}}),
    ("ads_activate_entity", {"entity_id": "999999"}),
    ("ads_create_ad_set", {"account_id": ACCOUNT, "campaign_id": "999999", "name": "s",
                           "optimization_goal": "REACH", "billing_event": "IMPRESSIONS"}),
    ("ads_create_ad", {"account_id": ACCOUNT, "adset_id": AS1, "name": "a", "creative_id": "999999"}),
])
def test_unresolvable_targets_are_denied(tool, args):
    proxy, mock, _ = make_proxy()
    result = call(proxy, tool, args)
    assert decision_of(result) == "deny"
    assert result.structuredContent["rein"]["reasons"][0]["rule_id"] == "rein:unresolvable"
    assert mock.write_log == []


# ------------------------------------------------------------------ current value unavailable


class ReadsBroken(InProcessDownstream):
    """Downstream whose read tools fail (error result or exception); writes would still work."""

    def __init__(self, mock, mode="error"):
        super().__init__(mock)
        self.mode = mode

    async def call(self, name, arguments):
        if name.startswith("ads_get_"):
            if self.mode == "raise":
                raise ConnectionError("downstream gone")
            if self.mode == "garbage":
                return DownstreamResult(True, {"unexpected": True}, "{}")
            return DownstreamResult(False, None, "error: temporarily unavailable")
        return await super().call(name, arguments)


@pytest.mark.parametrize("mode", ["error", "raise", "garbage"])
def test_budget_change_without_current_value_needs_approval(mode):
    mock = MockAdsMcp(BASE)
    proxy, _, _ = make_proxy(downstream=ReadsBroken(mock, mode))
    result = call(proxy, "ads_update_entity", {"entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 5500}})
    assert decision_of(result) == "require_approval"
    assert "rein:current-unavailable" in {r["rule_id"] for r in result.structuredContent["rein"]["reasons"]}
    assert mock.write_log == []


def test_over_cap_is_still_denied_without_current_value():
    mock = MockAdsMcp(BASE)
    proxy, _, _ = make_proxy(downstream=ReadsBroken(mock))
    result = call(proxy, "ads_update_entity", {"entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 50000}})
    assert decision_of(result) == "deny"


@pytest.mark.parametrize("tool,args", [
    ("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 5500}}),
    ("ads_update_entity", {"entity_id": AD2, "entity_type": "ad", "fields": {"creative_id": CR2}}),
    ("ads_update_entity", {"entity_id": AS1, "fields": {"name": "rename"}}),
    ("ads_activate_entity", {"entity_id": AS1}),
    ("ads_create_ad", {"account_id": ACCOUNT, "adset_id": AS1, "name": "a", "creative_id": CR2}),
])
def test_never_allowed_when_downstream_reads_fail(tool, args):
    mock = MockAdsMcp(BASE)
    proxy, _, _ = make_proxy(downstream=ReadsBroken(mock))
    assert decision_of(call(proxy, tool, args)) in ("require_approval", "deny")
    assert mock.write_log == []


def test_downstream_write_error_is_returned_and_audited():
    class WritesBroken(InProcessDownstream):
        async def call(self, name, arguments):
            if name == "ads_update_entity":
                raise ConnectionError("lost")
            return await super().call(name, arguments)

    proxy, _, _ = make_proxy(downstream=WritesBroken(MockAdsMcp(BASE)))
    result = call(proxy, "ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 5500}})
    assert result.isError and decision_of(result) == "forwarded-error"
    [audit] = proxy.ledger.audit_log()
    assert audit["decision"] == "allow" and audit["forwarded"] == 1 and audit["downstream_ok"] == 0
