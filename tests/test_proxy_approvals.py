"""Approval lifecycle: expiry boundary, re-check before execution, idempotency."""

from __future__ import annotations

import anyio
import pytest

from rein.proxy import ApprovalError, InProcessDownstream, ReinProxy

from proxy_helpers import AS1, BASE, Clock, MockAdsMcp, approve, call, make_proxy, policy_with
from test_proxy_failclosed import ReadsBroken

BUMP = {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 9000}}  # +80% -> require_approval


def queued(clock="2026-10-28T05:00:00Z", **kw):
    proxy, mock, clock = make_proxy(clock, **kw)
    result = call(proxy, "ads_update_entity", BUMP)
    return proxy, mock, clock, result.structuredContent["rein"]["approval_id"]


@pytest.mark.parametrize("seconds_after,status,writes", [
    (24 * 3600 - 1, "executed", 1),   # one second before the deadline
    (24 * 3600, "expired", 0),        # exactly at expires_at: no longer valid
    (24 * 3600 + 1, "expired", 0),
])
def test_expiry_boundary(seconds_after, status, writes):
    proxy, mock, clock, aid = queued()
    clock.advance(seconds=seconds_after)
    outcome = approve(proxy, aid)
    assert outcome.status == status and outcome.executed is (writes == 1)
    assert len(mock.write_log) == writes


def test_expiry_is_recorded_once():
    proxy, _, clock, aid = queued()
    clock.advance(hours=25)
    proxy.expire_due()
    proxy.expire_due()
    events = [a["event"] for a in proxy.ledger.audit_log(approval_id=aid)]
    assert events == ["call", "approval_expired"]


def test_drift_aborts_execution():
    proxy, mock, _, aid = queued()
    mock.call("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 15000}})  # someone else changed it
    outcome = approve(proxy, aid)
    assert outcome.status == "aborted" and not outcome.executed
    assert mock.find("adset", AS1)["daily_budget"] == "15000"
    row = proxy.ledger.get_approval(aid)
    assert row["status"] == "aborted" and "5000" in row["status_reason"] and "15000" in row["status_reason"]
    assert approve(proxy, aid).executed is False  # and it stays aborted


def test_recheck_deny_aborts():
    proxy, mock, clock, aid = queued()
    stricter = ReinProxy(policy_with(**{"adset-daily-cap": {"max": 8000}}), proxy.ledger, proxy.downstream, clock=clock)
    outcome = approve(stricter, aid)
    assert outcome.status == "aborted" and "上限" in outcome.message
    assert len(mock.write_log) == 0


def test_recheck_without_current_value_aborts():
    proxy, mock, clock, aid = queued()
    broken = ReinProxy(proxy.policy, proxy.ledger, ReadsBroken(mock), clock=clock)
    outcome = approve(broken, aid)
    assert outcome.status == "aborted" and len(mock.write_log) == 0


def test_failed_execution_is_not_retried():
    proxy, mock, clock, aid = queued()

    class WritesBroken(InProcessDownstream):
        async def call(self, name, arguments):
            if name == "ads_update_entity":
                raise ConnectionError("lost")
            return await super().call(name, arguments)

    flaky = ReinProxy(proxy.policy, proxy.ledger, WritesBroken(mock), clock=clock)
    assert approve(flaky, aid).status == "failed"
    second = approve(proxy, aid)
    assert second.status == "failed" and second.executed is False
    assert mock.write_log == []


def test_rejected_never_runs_and_cannot_be_rejected_twice():
    proxy, mock, _, aid = queued()
    assert proxy.reject(aid, "boss").status == "rejected"
    assert proxy.reject(aid, "boss").status == "rejected"
    assert approve(proxy, aid).executed is False
    events = [a["event"] for a in proxy.ledger.audit_log(approval_id=aid)]
    assert events == ["call", "approval_rejected", "approval_refused"]
    assert mock.write_log == []


def test_reject_after_expiry_reports_expired():
    proxy, _, clock, aid = queued()
    clock.advance(hours=24)
    assert proxy.reject(aid, "boss").status == "expired"


def test_unknown_approval_id():
    proxy, _, _ = make_proxy()
    with pytest.raises(ApprovalError):
        approve(proxy, "apr_doesnotexist")


def test_concurrent_double_approve_executes_once():
    proxy, mock, _, aid = queued()
    outcomes = []

    async def both():
        async def one():
            outcomes.append(await proxy.approve(aid, "a"))
        async with anyio.create_task_group() as tg:
            tg.start_soon(one)
            tg.start_soon(one)

    anyio.run(both)
    assert sorted(o.executed for o in outcomes) == [False, True]
    assert len(mock.write_log) == 1


def test_approved_campaign_creation_runs_once_and_records_new_id():
    proxy, mock, _ = make_proxy()
    r = call(proxy, "ads_create_campaign", {"ad_account_id": "act_100000000000001", "buying_type": "AUCTION", "campaign_name": "冬", "objective": "OUTCOME_AWARENESS"})
    aid = r.structuredContent["rein"]["approval_id"]
    outcome = approve(proxy, aid)
    assert outcome.executed
    new_id = outcome.result["structured"]["id"]
    assert mock.find("campaign", new_id)["status"] == "PAUSED"
    executed = [a for a in proxy.ledger.audit_log(approval_id=aid) if a["event"] == "approval_executed"]
    assert executed[0]["entity_id"] == new_id
    assert approve(proxy, aid).executed is False
    assert len(mock.write_log) == 1
