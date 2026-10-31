"""The five planted scenarios: every expected finding is detected, and nothing else is flagged."""

from __future__ import annotations

import json

import pytest

from conftest import SCENARIO_NAMES, SCENARIOS
from helpers import run_scenario


def expected(name):
    return json.loads((SCENARIOS / name / "scenario.json").read_text(encoding="utf-8"))["expected"]


def test_there_are_five_scenarios():
    assert len(SCENARIO_NAMES) == 5


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_scenario_detection_matches_expectation(name):
    ledger, results = run_scenario(name)
    by_key = {(r.change.entity_type, r.change.entity_id, r.change.field): r for r in results}
    for exp in expected(name):
        r = by_key[(exp["entity_type"], exp["entity_id"], exp["field"])]
        assert r.change.severity == exp["severity"], (name, exp, r.change.findings)
        assert sorted({f["rule_id"] for f in r.change.findings}) == sorted(exp["rules"])
        assert r.change.actor_kind == exp["actor_kind"]
    # Nothing outside the expectation is flagged.
    expected_flagged = {(e["entity_type"], e["entity_id"], e["field"]) for e in expected(name) if e["severity"]}
    flagged = {k for k, r in by_key.items() if r.change.severity}
    assert flagged == expected_flagged


def test_normal_change_produces_no_alert():
    _, results = run_scenario("5_normal_change")
    assert len(results) == 1 and results[0].alert is None


def test_unknown_attribution_is_explained():
    ledger, _ = run_scenario("2_url_swap")
    rec = next(c for c in ledger.feed() if c.field == "creative_resolved.link")
    assert rec.actor_kind == "unknown"
    assert rec.attribution["basis"] == "no_matching_activity"


def test_matched_attribution_keeps_activity_reference():
    ledger, _ = run_scenario("1_budget_spike")
    rec = ledger.feed()[0]
    assert rec.actor_kind == "external_agent"
    act = rec.attribution["activities"][0]
    assert act["event_type"] == "update_ad_set_budget"
    assert act["application_id"] == "900000000000002"
    assert rec.attribution["mapping"] == ["application:900000000000002"]


def test_without_attribution_config_everything_is_unknown():
    from rein.policy import load_policy, parse_policy
    from conftest import POLICY_EXAMPLE
    import yaml

    raw = yaml.safe_load(POLICY_EXAMPLE.read_text(encoding="utf-8"))
    raw.pop("attribution")
    ledger, _ = run_scenario("1_budget_spike", policy=parse_policy(raw))
    rec = ledger.feed()[0]
    assert rec.actor_kind == "unknown"
    assert rec.attribution["basis"] == "activity_matched_actor_undeclared"
