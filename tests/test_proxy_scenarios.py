"""Scenario set 1-14 from fixtures/proxy/scenarios.json."""

from __future__ import annotations

import pytest

from proxy_helpers import approve, call, decision_of, make_proxy, scenarios

CALLS = {c["id"]: c for c in scenarios()["calls"]}
FLOWS = scenarios()["approval_flows"]
DEFAULT_CLOCK = scenarios()["default_clock"]


def run_call(spec):
    proxy, mock, clock = make_proxy(spec.get("clock", DEFAULT_CLOCK))
    result = call(proxy, spec["tool"], spec["arguments"])
    return proxy, mock, clock, result


@pytest.mark.parametrize("cid", sorted(CALLS))
def test_call_scenario(cid):
    spec = CALLS[cid]
    proxy, mock, _, result = run_call(spec)
    expected = spec["expected"]
    if expected in ("allow", "passthrough"):
        assert not result.isError, result.content
    else:
        assert decision_of(result) == expected, result.content
        rein = result.structuredContent["rein"]
        assert rein["executed"] is False
        assert sorted({r["rule_id"] for r in rein["reasons"]}) == sorted(spec["rules"])
    assert (len(mock.write_log) > 0) is spec["downstream_changed"]
    [audit] = proxy.ledger.audit_log()
    assert audit["decision"] == expected
    assert audit["tool"] == spec["tool"]
    assert sorted({r["rule_id"] for r in audit["reasons"]}) == sorted(spec["rules"])


def test_allow_actually_changes_downstream():
    proxy, mock, _, result = run_call(CALLS[1])
    assert mock.find("adset", "120000000000000202")["daily_budget"] == "4400"
    assert result.structuredContent == {"success": True, "id": "120000000000000202", "entity_type": "adset"}


def test_require_approval_returns_id_and_status():
    proxy, mock, _, result = run_call(CALLS[3])
    rein = result.structuredContent["rein"]
    assert rein["status"] == "pending" and rein["approval_id"].startswith("apr_")
    assert rein["expires_at"] == "2026-10-29T05:00:00Z"
    [row] = proxy.ledger.list_approvals()
    assert row["id"] == rein["approval_id"] and row["status"] == "pending"
    assert row["arguments"] == CALLS[3]["arguments"]  # full arguments kept
    assert row["declared_client"]["verified"] is False
    assert row["precheck"] == {"daily_budget": 5000}


@pytest.mark.parametrize("flow", FLOWS, ids=lambda f: str(f["id"]))
def test_approval_flow(flow):
    proxy, mock, clock, result = run_call(CALLS[flow["call"]])
    approval_id = result.structuredContent["rein"]["approval_id"]
    outcomes = []
    for step in flow["steps"]:
        if step == "approve":
            outcomes.append(approve(proxy, approval_id))
        elif step == "reject":
            outcomes.append(proxy.reject(approval_id, "tester", "増額が大きすぎる"))
        elif step == "wait_ttl":
            clock.advance(hours=24)
    final = proxy.ledger.get_approval(approval_id)
    assert final["status"] == flow["final_status"]
    assert len(mock.write_log) == flow["downstream_writes"]
    executed_audits = [a for a in proxy.ledger.audit_log(approval_id=approval_id) if a["event"] == "approval_executed"]
    assert len(executed_audits) == flow["downstream_writes"]
    if flow["final_status"] == "executed":
        assert mock.find("adset", "120000000000000201")["daily_budget"] == "9000"
        assert final["executed_at"] and final["decided_by"] == "tester"
    if len(outcomes) == 2 and flow["steps"][-1] == "approve":
        assert outcomes[-1].executed is False
