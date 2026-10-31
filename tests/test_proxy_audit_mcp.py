"""Audit records, the MCP protocol surface (in-memory and stdio) and the approvals CLI."""

from __future__ import annotations

import json
import shutil
import sys

import anyio
import yaml
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from rein.cli import main
from rein.client import MockAdsClient
from rein.ledger import Ledger
from rein.mock_ads_mcp import build_server as build_mock_server
from rein.pipeline import ingest
from rein.proxy import ReinProxy, SessionDownstream, build_proxy_server
from rein.timeutil import parse_time

from proxy_helpers import (ACCOUNT, AS1, BASE, PROXY_POLICY, Clock, MockAdsMcp, approve, call, make_proxy,
                           scenarios)

AGENT_INFO = types.Implementation(name="test-agent", version="9.9")


def test_every_call_is_audited_once_with_declared_client():
    proxy, _, _ = make_proxy()
    for spec in scenarios()["calls"]:
        if "clock" not in spec:
            call(proxy, spec["tool"], spec["arguments"])
    rows = proxy.ledger.audit_log()
    assert len(rows) == len([s for s in scenarios()["calls"] if "clock" not in s])
    for r in rows:
        assert r["declared_client"] == {"name": "scripted-agent", "version": "1.0",
                                        "source": "mcp.initialize.clientInfo", "verified": False}
        assert r["occurred_at"] == "2026-10-28T05:00:00Z"
        assert len(r["arguments_sha256"]) == 64
        assert r["decision"] in {"passthrough", "allow", "deny", "require_approval"}
    read = next(r for r in rows if r["decision"] == "passthrough")
    assert read["forwarded"] == 1 and read["tool"] == "ads_get_ad_entities"
    assert proxy.ledger.feed() == []  # proxy audit never lands in the Watch change ledger


def test_long_arguments_are_summarized_but_hashed_in_full():
    proxy, _, _ = make_proxy()
    call(proxy, "ads_update_entity", {"entity_id": AS1, "fields": {"name": "x" * 1000}})
    [row] = proxy.ledger.audit_log()
    assert len(row["arguments_summary"]) == 240 and row["arguments_summary"].endswith("…")


def test_approval_lifecycle_audit_trail():
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 9000}})
    aid = r.structuredContent["rein"]["approval_id"]
    approve(proxy, aid, operator="運用責任者B")
    approve(proxy, aid, operator="運用責任者B")
    trail = proxy.ledger.audit_log(approval_id=aid)
    assert [(a["event"], a["decision"]) for a in trail] == [
        ("call", "require_approval"), ("approval_executed", "executed"), ("approval_refused", "refused")]
    assert trail[1]["operator"] == "運用責任者B" and trail[1]["entity_id"] == AS1
    assert trail[1]["declared_client"]["name"] == "scripted-agent"  # original requester kept


def test_audit_and_watch_ledger_share_entity_id_and_time_format(tmp_path):
    """Phase 3 needs to join proxy audit rows with Watch changes."""
    proxy, mock, _ = make_proxy()
    call(proxy, "ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 5500}})
    after = mock.export_fixture(tmp_path / "after", "2026-10-28T06:00:00Z")
    ingest(proxy.ledger, MockAdsClient(BASE))
    ingest(proxy.ledger, MockAdsClient(after))
    [change] = proxy.ledger.feed()
    [audit] = proxy.ledger.audit_log()
    assert (change.entity_type, change.entity_id, change.before, change.after) == ("adset", AS1, 5000, 5500)
    assert audit["entity_type"] == change.entity_type and audit["entity_id"] == change.entity_id
    assert parse_time(audit["occurred_at"]) <= change.detected_at


# ------------------------------------------------------------------ MCP protocol, in memory


def test_proxy_over_mcp_records_client_info():
    async def scenario():
        mock = MockAdsMcp(BASE)
        async with create_connected_server_and_client_session(build_mock_server(mock)) as downstream_session:
            proxy = ReinProxy(make_proxy()[0].policy, Ledger(":memory:"), SessionDownstream(downstream_session),
                              clock=Clock("2026-10-28T05:00:00Z"))
            async with create_connected_server_and_client_session(build_proxy_server(proxy),
                                                                  client_info=AGENT_INFO) as agent:
                tools = await agent.list_tools()
                assert "ads_update_entity" in {t.name for t in tools.tools}
                ok = await agent.call_tool("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 5500}})
                assert not ok.isError and ok.structuredContent["success"] is True
                denied = await agent.call_tool("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 50000}})
                assert denied.isError and denied.structuredContent["rein"]["decision"] == "deny"
                broken = await agent.call_tool("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": "lots"}})
                assert broken.isError and broken.structuredContent["rein"]["reasons"][0]["rule_id"] == "rein:uninterpretable"
                unknown = await agent.call_tool("ads_delete_entity", {"entity_id": AS1})
                assert unknown.isError and unknown.structuredContent["rein"]["decision"] == "deny"
            assert mock.find("adset", AS1)["daily_budget"] == "5500" and len(mock.write_log) == 1
            rows = proxy.ledger.audit_log()
            assert [r["decision"] for r in rows] == ["allow", "deny", "deny", "deny"]
            assert rows[0]["declared_client"]["name"] == "test-agent" and rows[0]["declared_client"]["verified"] is False
    anyio.run(scenario)


# ------------------------------------------------------------------ stdio end to end + approvals CLI


def test_stdio_proxy_and_approvals_cli(tmp_path, capsys):
    db, state = tmp_path / "rein.db", tmp_path / "mock_state.json"
    # Real wall clock runs inside the subprocess, so drop the business-hours rule for determinism.
    raw = yaml.safe_load(PROXY_POLICY.read_text(encoding="utf-8"))
    raw["rules"] = [r for r in raw["rules"] if r["type"] != "change_window"]
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "rein.cli", "--db", str(db), "proxy", "--policy", str(policy), "--mock-fixture", str(BASE),
        "--mock-state", str(state)])

    async def agent():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, client_info=AGENT_INFO) as session:
                await session.initialize()
                ok = await session.call_tool("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 5500}})
                assert not ok.isError
                pending = await session.call_tool("ads_update_entity", {"entity_id": AS1, "fields": {"daily_budget": 9000}})
                return pending.structuredContent["rein"]

    rein = anyio.run(agent)
    assert rein["decision"] == "require_approval"
    assert MockAdsMcp(BASE, state).find("adset", AS1)["daily_budget"] == "5500"

    assert main(["--db", str(db), "approvals", "list"]) == 0
    out = capsys.readouterr().out
    assert rein["approval_id"] in out and "pending" in out and "test-agent（申告）" in out

    approve_args = ["--db", str(db), "approvals", "approve", rein["approval_id"], "--policy", str(policy),
                    "--mock-fixture", str(BASE), "--mock-state", str(state), "--by", "tester"]
    assert main(approve_args) == 0
    assert "executed" in capsys.readouterr().out
    assert main(approve_args) == 2  # second press: not executed again
    assert "再実行しない" in capsys.readouterr().out
    final = MockAdsMcp(BASE, state)
    assert final.find("adset", AS1)["daily_budget"] == "9000"
    assert [w["arguments"]["fields"]["daily_budget"] for w in final.write_log] == [5500, 9000]

    assert main(["--db", str(db), "audit"]) == 0
    out = capsys.readouterr().out
    assert "approval_executed" in out and "operator=tester" in out


def test_reject_cli(tmp_path, capsys):
    from rein.timeutil import now_utc
    db = tmp_path / "rein.db"
    # The CLI runs on the wall clock, so queue the approval on the wall clock too.
    proxy, _, _ = make_proxy(Clock(now_utc()), ledger=Ledger(db))
    r = call(proxy, "ads_create_campaign", {"account_id": ACCOUNT, "name": "x", "objective": "OUTCOME_SALES"})
    aid = r.structuredContent["rein"]["approval_id"]
    assert main(["--db", str(db), "approvals", "reject", aid, "--by", "boss", "--note", "不要"]) == 0
    assert "rejected" in capsys.readouterr().out
    assert main(["--db", str(db), "approvals", "reject", aid]) == 2
    capsys.readouterr()
    assert main(["--db", str(db), "approvals", "list", "--status", "rejected", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["id"] == aid and row["decided_by"] == "boss" and row["status_reason"] == "不要"
