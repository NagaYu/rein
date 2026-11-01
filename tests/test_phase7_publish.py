"""Phase 7: publish flags on ads_activate_entity, the not-an-entity lookup, created_draft, and the mock draft lifecycle."""

from __future__ import annotations

import json
import sqlite3

import pytest

from http_helpers import DUMMY, http_mock
from proxy_helpers import ACCOUNT, AS2, PROXY_POLICY, approve, call, decision_of, make_proxy
from rein.cli import main
from rein.downstream_http import HttpDownstream
from rein.ledger import Ledger
from rein.mock_ads_mcp import MockAdsMcp
from rein.proxy import DownstreamResult, InProcessDownstream, is_draft
from rein.timeutil import now_utc

from conftest import FIXTURES

C1 = "120000000000000101"
CAMPAIGN = {"ad_account_id": ACCOUNT, "campaign_name": "Draft test", "objective": "OUTCOME_TRAFFIC",
            "buying_type": "AUCTION"}


def activate(entity_id, entity_type="campaign", **flags):
    return {"ad_account_id": ACCOUNT, "entity_id": entity_id, "entity_type": entity_type, **flags}


def rein_of(result):
    return (result.structuredContent or {}).get("rein") or {}


def rules_of(result):
    return [r["rule_id"] for r in rein_of(result).get("reasons", [])]


# --------------------------------------------------------------------------- publish flags


@pytest.mark.parametrize("flag,value", [("publish_as_active", "true"), ("publish_as_active", 1),
                                        ("publish_as_active", None), ("publish_as_active", {}),
                                        ("allow_partial_publish", "yes"), ("allow_partial_publish", 0)])
def test_publish_flags_must_be_literal_booleans(flag, value):
    proxy, mock, _ = make_proxy()
    r = call(proxy, "ads_activate_entity", activate(C1, **{flag: value}))
    assert decision_of(r) == "deny" and rules_of(r) == ["rein:uninterpretable"] and mock.write_log == []


def test_publish_always_needs_approval_even_inside_business_hours():
    proxy, mock, _ = make_proxy("2026-10-28T05:00:00Z")  # Wednesday 14:00 JST, inside the window
    plain = call(proxy, "ads_activate_entity", activate(C1))
    assert decision_of(plain) == "forwarded"  # a plain activation is unchanged
    published = call(make_proxy("2026-10-28T05:00:00Z")[0], "ads_activate_entity", activate(C1, publish_as_active=True))
    assert decision_of(published) == "require_approval" and rules_of(published) == ["publish-needs-approval"]
    assert "ドラフトを公開" in rein_of(published)["reasons"][0]["reason"]


def test_publish_outside_hours_keeps_both_reasons():
    proxy, _, _ = make_proxy("2026-10-28T18:00:00Z")  # Thursday 03:00 JST
    r = call(proxy, "ads_activate_entity", activate(C1, publish_as_active=True))
    assert decision_of(r) == "require_approval" and set(rules_of(r)) == {"publish-needs-approval", "business-hours"}


@pytest.mark.parametrize("flags", [{"publish_as_active": False}, {"allow_partial_publish": True},
                                   {"publish_as_active": False, "allow_partial_publish": True}])
def test_without_publish_true_the_call_is_a_plain_activation(flags):
    proxy, _, _ = make_proxy("2026-10-28T05:00:00Z")
    assert decision_of(call(proxy, "ads_activate_entity", activate(C1, **flags))) == "forwarded"


def test_a_deny_is_never_softened_by_publish():
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_activate_entity", {**activate(C1, publish_as_active=True), "ad_account_id": "acct_1"})
    assert decision_of(r) == "deny"


def test_unknown_target_without_a_server_statement_is_still_denied():
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_activate_entity", activate("999999", publish_as_active=True))
    assert decision_of(r) == "deny" and rules_of(r) == ["rein:unresolvable"]


# --------------------------------------------------------------------------- lookups in the real answer shape


class RealShape(InProcessDownstream):
    """Answers entity lookups the way the real Ads MCP did in Phase 5; everything else goes to the mock."""

    def __init__(self, mock, rows, info=None):
        super().__init__(mock)
        self.rows, self.info = rows, info

    async def call(self, name, arguments):
        if name == "ads_get_ad_entities":
            out = {"ad_entities": json.dumps(self.rows), "client_conversation_id": "x"}
            if self.info:
                out["additional_info"] = self.info
            return DownstreamResult(True, out, json.dumps(out))
        return await super().call(name, arguments)


DRAFT = "990000000000570355"  # synthetic in the published copy
NOT_ENTITY = f"The following object_ids are not campaign entities in the requested ad account: {DRAFT}."


def test_not_an_entity_target_queues_with_the_servers_words():
    proxy, mock, _ = make_proxy("2026-10-28T05:00:00Z")
    proxy.downstream = RealShape(mock, [], NOT_ENTITY)
    for flags in ({}, {"publish_as_active": True}):
        r = call(proxy, "ads_activate_entity", activate(DRAFT, **flags))
        assert decision_of(r) == "require_approval", flags
        reason = next(x for x in rein_of(r)["reasons"] if x["rule_id"] == "rein:target-not-entity")
        assert reason["detail"] == NOT_ENTITY
    assert rules_of(r) == ["rein:target-not-entity", "publish-needs-approval"]
    audit = proxy.ledger.rows("proxy_audit")[-1]
    assert NOT_ENTITY in audit["reasons"] and mock.write_log == []


def test_not_an_entity_update_is_still_denied():
    proxy, mock, _ = make_proxy()
    proxy.downstream = RealShape(mock, [], NOT_ENTITY)
    r = call(proxy, "ads_update_entity", {"ad_account_id": ACCOUNT, "entity_id": DRAFT, "entity_type": "campaign",
                                          "fields": {"daily_budget": 1000}})
    assert decision_of(r) == "deny" and rules_of(r) == ["rein:unresolvable"]


def test_rows_in_the_real_ad_entities_string_are_found():
    proxy, mock, _ = make_proxy("2026-10-28T05:00:00Z")
    proxy.downstream = RealShape(mock, [{"id": AS2, "status": "PAUSED", "daily_budget": "4000"}])
    r = call(proxy, "ads_update_entity", {"ad_account_id": ACCOUNT, "entity_id": AS2, "entity_type": "adset",
                                          "fields": {"daily_budget": 4400}})
    assert decision_of(r) == "forwarded"  # current value 4000 was read from the real-shape answer (+10%)


# --------------------------------------------------------------------------- created_draft


@pytest.mark.parametrize("res,expected", [
    (DownstreamResult(True, {"campaign_id": "1", "status": "DRAFT"}, ""), True),
    (DownstreamResult(True, None, '{"campaign_id": "1", "status": "DRAFT", "spec": "{}"}'), True),
    (DownstreamResult(True, {"id": "1", "status": "PAUSED"}, ""), False),
    (DownstreamResult(True, {"id": "1", "status": "ACTIVE"}, ""), False),
    (DownstreamResult(True, {"id": "1", "status": "draft"}, ""), False),
    (DownstreamResult(True, {"spec": {"status": "DRAFT"}}, ""), False),
    (DownstreamResult(True, None, "DRAFT"), False),
    (DownstreamResult(True, None, '{"campaign_id": "1", "status": "DRA'), False),
])
def test_draft_detection_is_conservative(res, expected):
    assert is_draft(res) is expected


def test_paused_creation_is_untagged_and_draft_creation_is_tagged():
    for draft_mode, kind in ((False, None), (True, "created_draft")):
        proxy, _, _ = make_proxy()
        proxy.downstream = InProcessDownstream(MockAdsMcp(FIXTURES / "base", draft_lifecycle=draft_mode))
        aid = rein_of(call(proxy, "ads_create_campaign", CAMPAIGN))["approval_id"]
        outcome = approve(proxy, aid)
        assert (outcome.status, outcome.result_kind) == ("executed", kind)
        assert proxy.ledger.get_approval(aid)["result_kind"] == kind
        assert proxy.ledger.rows("proxy_audit")[-1]["result_kind"] == kind


def test_real_size_draft_answer_is_tagged_although_its_detail_is_truncated():
    big = {"campaign_id": DRAFT, "status": "DRAFT", "spec": "x" * 2000}
    res = DownstreamResult(True, big, json.dumps(big))
    assert is_draft(res)


# --------------------------------------------------------------------------- the mock draft lifecycle over HTTP


def http_proxy(url, ledger=None):
    proxy, _, _ = make_proxy("2026-10-28T05:00:00Z", downstream=HttpDownstream(url, DUMMY, timeout=10), ledger=ledger)
    return proxy


def entities(proxy, **kw):
    r = call(proxy, "ads_get_ad_entities", {"ad_account_id": ACCOUNT, **kw})
    return r.structuredContent


def test_full_draft_loop_create_approve_publish_verify():
    mock = MockAdsMcp(FIXTURES / "base", draft_lifecycle=True)
    from rein.mock_ads_mcp_http import serve_in_thread
    with serve_in_thread(mock) as url:
        proxy = http_proxy(url)
        # 1. create -> queued -> approved -> executed [created_draft]
        aid = rein_of(call(proxy, "ads_create_campaign", CAMPAIGN))["approval_id"]
        created = approve(proxy, aid)
        assert (created.status, created.result_kind) == ("executed", "created_draft")
        draft_id = json.loads(proxy.ledger.get_approval(aid)["result"]["text"])["campaign_id"]
        # 2. the draft is not an entity, and the server says so
        before = entities(proxy, object_ids=[draft_id])
        assert before["data"] == [] and draft_id in before["additional_info"]
        assert draft_id not in json.dumps(entities(proxy)["data"])
        # 3. activate without publish: queued, then the server refuses -> execution_failed, consumed
        plain = call(proxy, "ads_activate_entity", activate(draft_id))
        assert rules_of(plain) == ["rein:target-not-entity"]
        failed = approve(proxy, rein_of(plain)["approval_id"])
        assert failed.status == "execution_failed" and "has not been published" in failed.detail
        assert approve(proxy, rein_of(plain)["approval_id"]).executed is False
        # 4. activate with publish_as_active: true -> queued -> approved -> executed
        publish = call(proxy, "ads_activate_entity", activate(draft_id, publish_as_active=True))
        assert decision_of(publish) == "require_approval"
        assert rules_of(publish) == ["rein:target-not-entity", "publish-needs-approval"]
        published = approve(proxy, rein_of(publish)["approval_id"])
        assert (published.status, published.executed, published.result_kind) == ("executed", True, None)
        assert '"published": true' in published.detail
        again = approve(proxy, rein_of(publish)["approval_id"])
        assert (again.status, again.executed) == ("executed", False)  # exactly once
        # 5. verification: by object_ids, account-level, activity log
        after = entities(proxy, object_ids=[draft_id])
        assert [(r["id"], r["status"]) for r in after["data"]] == [(draft_id, "PAUSED")]
        assert "additional_info" not in after
        assert draft_id in [r["id"] for r in entities(proxy, level="campaign")["data"]]
        logs = call(proxy, "ads_account_get_activity_logs", {"ad_account_id": ACCOUNT}).structuredContent["data"]
        assert [e["object_id"] for e in logs if e["object_id"] == draft_id] == [draft_id]
    events = [(r["event"], r["decision"], r["result_kind"]) for r in proxy.ledger.rows("proxy_audit")
              if r["event"] != "call"]
    assert events == [("approval_executed", "executed", "created_draft"),
                      ("approval_execution_failed", "execution_failed", None),
                      ("approval_refused", "refused", None),
                      ("approval_executed", "executed", None),
                      ("approval_refused", "refused", None)]
    assert [w["tool"] for w in mock.write_log] == ["ads_create_campaign", "ads_activate_entity"]


# --------------------------------------------------------------------------- CLI display and migration


def test_tag_is_shown_in_approvals_list_and_audit(tmp_path, monkeypatch, capsys):
    db = tmp_path / "rein.db"
    mock = MockAdsMcp(FIXTURES / "base", draft_lifecycle=True)
    from rein.mock_ads_mcp_http import serve_in_thread
    with serve_in_thread(mock) as url:
        proxy, _, _ = make_proxy(now_utc(), ledger=Ledger(db))
        aid = rein_of(call(proxy, "ads_create_campaign", CAMPAIGN))["approval_id"]
        monkeypatch.setenv("REIN_ADS_MCP_TOKEN", DUMMY)
        assert main(["--db", str(db), "approvals", "approve", aid, "--policy", str(PROXY_POLICY),
                     "--downstream", "http", "--downstream-url", url, "--by", "boss"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{aid}: executed [created_draft]") and '"status": "DRAFT"' in out
    assert main(["--db", str(db), "approvals", "list"]) == 0
    assert "executed [created_draft]" in capsys.readouterr().out
    assert main(["--db", str(db), "audit"]) == 0
    audit = capsys.readouterr().out
    assert "executed [created_draft]" in audit and 'result: {"campaign_id"' in audit
    assert DUMMY not in out + audit


def test_old_ledgers_gain_result_kind(tmp_path):
    db = tmp_path / "old.db"
    with Ledger(db) as ledger:
        proxy, _, _ = make_proxy(ledger=ledger)
        call(proxy, "ads_create_campaign", CAMPAIGN)
    conn = sqlite3.connect(db)
    for table in ("approvals", "proxy_audit"):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN result_kind")
    conn.commit()
    conn.close()
    with Ledger(db) as ledger:
        for table in ("approvals", "proxy_audit"):
            assert "result_kind" in {r["name"] for r in ledger.conn.execute(f"PRAGMA table_info({table})")}
        assert len(ledger.rows("approvals")) == 1 and ledger.rows("approvals")[0]["result_kind"] is None
