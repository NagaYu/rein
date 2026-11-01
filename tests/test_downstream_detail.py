"""Phase 5D: the downstream's own words (detail) are captured, bounded, scrubbed, shown, and never decide."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import anyio
import pytest

from http_helpers import DUMMY, StripAuthorization, http_mock, misbehaving
from proxy_helpers import ACCOUNT, AS1, AS2, PROXY_POLICY, ROOT, approve, call, decision_of, make_proxy, scenarios
from rein.cli import main
from rein.downstream_http import DETAIL_LIMIT, HttpDownstream, sanitize_detail
from rein.ledger import Ledger
from rein.proxy import DownstreamResult, error_content
from rein.timeutil import now_utc

UNKNOWN = "act_999999999"


def http_proxy(url, clock="2026-10-28T05:00:00Z", ledger=None, **kw):
    downstream = HttpDownstream(url, DUMMY, timeout=kw.pop("timeout", 10), **kw)
    proxy, _, clock = make_proxy(clock, downstream=downstream, ledger=ledger)
    return proxy


def downstream_reasons(row):
    return [r for r in json.loads(row["reasons"]) if r["rule_id"] == "rein:downstream-failure"]


def everything(proxy) -> str:
    return json.dumps([proxy.ledger.rows(t) for t in ("proxy_audit", "approvals")], ensure_ascii=False)


# --------------------------------------------------------------------------- sanitize_detail


def test_detail_is_single_line_bounded_and_scrubbed():
    raw = f"first line\n\n  second\tline Authorization: Bearer abc.def-123 and {DUMMY} " + "x" * 2000
    detail = sanitize_detail(raw, DUMMY)
    assert len(detail) == DETAIL_LIMIT and detail.endswith("…") and "\n" not in detail and "\t" not in detail
    assert DUMMY not in detail and "abc.def-123" not in detail and "Bearer [redacted]" in detail
    assert sanitize_detail("   \n ") is None and sanitize_detail(None) is None
    assert sanitize_detail({"error": {"message": "m"}}) == '{"error": {"message": "m"}}'


# --------------------------------------------------------------------------- reads and lookups


def test_jsonrpc_error_on_a_forwarded_read_carries_the_server_message():
    with http_mock(faults=("jsonrpc-errors",)) as (url, _):
        proxy = http_proxy(url)
        result = call(proxy, "ads_get_ad_entities", {"ad_account_id": UNKNOWN})
    assert result.isError and result.content[0].text == "downstream error: protocol_error"  # unchanged text
    downstream = result.structuredContent["rein_downstream"]
    assert downstream["failure"] == "protocol_error"
    assert downstream["detail"] == f"MCP error -32602: (mock) ad account {UNKNOWN} not found or not accessible with this token"
    row = proxy.ledger.rows("proxy_audit")[0]
    assert row["decision"] == "passthrough" and row["downstream_ok"] == 0
    assert row["result_summary"] == "downstream error: protocol_error"
    [reason] = downstream_reasons(row)
    assert reason["detail"] == downstream["detail"] and reason["decision"] == "passthrough"


def test_lookup_failure_detail_reaches_the_approval_payload_without_changing_the_verdict():
    args = {"ad_account_id": UNKNOWN, "entity_id": AS2, "entity_type": "adset", "fields": {"daily_budget": 4400}}
    with http_mock(faults=("jsonrpc-errors",)) as (url, mock):
        result = call(http_proxy(url), "ads_update_entity", args)
    rein = result.structuredContent["rein"]
    assert rein["decision"] == "require_approval" and mock.write_log == []
    rules = [r["rule_id"] for r in rein["reasons"]]
    reference = call(make_proxy()[0], "ads_update_entity", args).structuredContent["rein"]
    assert reference["decision"] == "require_approval"
    # Same rules as the in-process path (no HTTP detail), plus only the diagnostic failure entry.
    assert set(rules) - {"rein:downstream-failure"} == {r["rule_id"] for r in reference["reasons"]}
    assert "rein:downstream-failure" in rules
    assert "not found or not accessible" in rein["downstream_detail"]
    assert rein["reasons"][1]["reason"] == "下流の読み取りに失敗: protocol_error"  # class text as before


def test_tool_level_lookup_error_gets_a_detail_too():
    args = {"ad_account_id": UNKNOWN, "entity_id": AS2, "entity_type": "adset", "fields": {"daily_budget": 4400}}
    with http_mock() as (url, _):
        rein = call(http_proxy(url), "ads_update_entity", args).structuredContent["rein"]
    assert rein["decision"] == "require_approval"
    assert "not found or not accessible" in rein["downstream_detail"]


@pytest.mark.parametrize("mode,label,expected", [
    ("500", "http_status (500)", 'HTTP 500 Internal Server Error; body: {"error":"boom"}'),
    ("401", "unauthorized (401)", 'HTTP 401 Unauthorized; www-authenticate: Bearer; body: {"error":"unauthorized"}'),
    ("garbage-json", "malformed", None),
])
def test_http_failures_carry_status_and_body(mode, label, expected):
    with misbehaving(mode) as url:
        result = call(http_proxy(url, timeout=5), "ads_get_ad_entities", {"ad_account_id": ACCOUNT})
    downstream = result.structuredContent["rein_downstream"]
    assert downstream["failure"] == label
    if expected:
        assert downstream["detail"] == expected
    else:
        assert downstream["detail"]  # some parser message; content depends on the SDK


def test_missing_header_401_from_the_mock():
    with http_mock() as (url, _):
        result = call(http_proxy(url, transport=StripAuthorization()), "ads_get_ad_accounts", {})
    assert result.structuredContent["rein_downstream"]["detail"].startswith("HTTP 401 Unauthorized")


# --------------------------------------------------------------------------- redaction and truncation


def test_token_echoed_by_the_server_never_survives():
    with http_mock(faults=("echo-token",)) as (url, _):
        proxy = http_proxy(url)
        read = call(proxy, "ads_get_ad_entities", {"ad_account_id": ACCOUNT})
        write = call(proxy, "ads_update_entity", {"ad_account_id": ACCOUNT, "entity_id": AS2, "entity_type": "adset",
                                                  "fields": {"daily_budget": 4400}})
    detail = read.structuredContent["rein_downstream"]["detail"]
    assert detail == "MCP error -32001: (mock) invalid OAuth access token: Bearer [redacted]"
    assert write.structuredContent["rein"]["decision"] == "require_approval"
    produced = json.dumps([read.model_dump(mode="json"), write.model_dump(mode="json")]) + everything(proxy)
    assert DUMMY not in produced


def test_long_multiline_error_body_is_truncated_and_scrubbed():
    with misbehaving("500-long") as url:
        proxy = http_proxy(url, timeout=5)
        result = call(proxy, "ads_get_ad_accounts", {})
    detail = result.structuredContent["rein_downstream"]["detail"]
    assert len(detail) <= DETAIL_LIMIT and "\n" not in detail and DUMMY not in detail
    assert detail.startswith("HTTP 500 Internal Server Error; body: line 0: Authorization: Bearer [redacted]")
    assert DUMMY not in everything(proxy)


def test_tool_result_text_echoing_the_token_is_scrubbed():
    with http_mock() as (url, mock):
        mock.state["account"]["name"] = f"name with {DUMMY}"
        res = anyio.run(HttpDownstream(url, DUMMY, timeout=10).call, "ads_get_ad_accounts", {})
    assert DUMMY not in res.text and DUMMY not in json.dumps(res.structured) and DUMMY not in res.detail


# --------------------------------------------------------------------------- proxy_call.py


def run_proxy_call(tmp_path, url, *argv):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"), "REIN_ADS_MCP_TOKEN": DUMMY}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "proxy_call.py"), "--db", str(tmp_path / "r.db"),
                           "--policy", str(PROXY_POLICY), "--downstream", "http", "--downstream-url", url, *argv],
                          capture_output=True, text=True, env=env, timeout=60)


def test_proxy_call_prints_downstream_detail(tmp_path):
    with http_mock(faults=("jsonrpc-errors",)) as (url, _):
        proc = run_proxy_call(tmp_path, url, "--tool", "ads_get_ad_entities", "--args",
                              json.dumps({"ad_account_id": UNKNOWN}))
    out = json.loads(proc.stdout)
    assert proc.returncode == 1 and out["decision"] == "forwarded" and out["downstream_ok"] is False
    assert out["downstream_detail"].startswith("MCP error -32602: (mock) ad account act_999999999 not found")
    assert DUMMY not in proc.stdout + proc.stderr


def test_proxy_call_prints_detail_for_a_queued_write(tmp_path):
    with http_mock(faults=("jsonrpc-errors",)) as (url, _):
        proc = run_proxy_call(tmp_path, url, "--allow-write", "--tool", "ads_update_entity", "--args", json.dumps(
            {"ad_account_id": UNKNOWN, "entity_id": AS2, "entity_type": "adset", "fields": {"daily_budget": 4400}}))
    out = json.loads(proc.stdout)
    assert out["decision"] == "require_approval" and "not found or not accessible" in out["downstream_detail"]


def test_proxy_call_echo_token_is_redacted(tmp_path):
    with http_mock(faults=("echo-token",)) as (url, _):
        proc = run_proxy_call(tmp_path, url, "--tool", "ads_get_ad_accounts")
    assert "Bearer [redacted]" in proc.stdout and DUMMY not in proc.stdout + proc.stderr
    with Ledger(tmp_path / "r.db") as ledger:
        assert DUMMY not in json.dumps(ledger.rows("proxy_audit"))


# --------------------------------------------------------------------------- execution path (approve)


def queue(proxy, tool, args):
    rein = call(proxy, tool, args).structuredContent["rein"]
    assert rein["decision"] == "require_approval"
    return rein["approval_id"]


BUMP = {"ad_account_id": ACCOUNT, "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 9000}}
CAMPAIGN = {"ad_account_id": ACCOUNT, "campaign_name": "冬の新作", "objective": "OUTCOME_AWARENESS",
            "buying_type": "AUCTION"}


@pytest.mark.parametrize("fault,needle", [("write-iserror", "Error: (mock) (#100) invalid parameter"),
                                          ("write-error-body", '"message": "(mock) (#2635)')])
def test_error_content_on_execution_is_execution_failed(fault, needle):
    with http_mock(faults=(fault,)) as (url, mock):
        proxy = http_proxy(url)
        aid = queue(proxy, "ads_update_entity", BUMP)
        outcome = approve(proxy, aid)
        again = approve(proxy, aid)
    assert (outcome.status, outcome.executed) == ("execution_failed", False) and needle in outcome.detail
    assert (again.status, again.executed) == ("execution_failed", False)  # consumed: no retry
    assert mock.write_log == [] and mock.find("adset", AS1)["daily_budget"] == "5000"
    record = proxy.ledger.get_approval(aid)
    assert record["status"] == "execution_failed" and needle in record["result"]["detail"]
    assert record["result"]["ok"] is False and "応答がエラーを示した" in record["status_reason"]
    events = [(r["event"], r["decision"], r["downstream_ok"]) for r in proxy.ledger.rows("proxy_audit")]
    assert events == [("call", "require_approval", None), ("approval_execution_failed", "execution_failed", 0),
                      ("approval_refused", "refused", None)]
    assert needle in proxy.ledger.rows("proxy_audit")[1]["result_summary"]


def test_successful_creation_shows_the_created_id():
    with http_mock() as (url, mock):
        proxy = http_proxy(url)
        aid = queue(proxy, "ads_create_campaign", CAMPAIGN)
        outcome = approve(proxy, aid)
    created = mock.write_log[0]["result"]["id"]
    assert (outcome.status, outcome.executed) == ("executed", True)
    assert outcome.detail == json.dumps({"id": created, "status": "PAUSED"})
    row = proxy.ledger.rows("proxy_audit")[1]
    assert row["event"] == "approval_executed" and created in row["result_summary"] and row["entity_id"] == created


@pytest.mark.parametrize("fault,status,code", [("write-iserror", "execution_failed", 2),
                                               ("write-error-body", "execution_failed", 2), (None, "executed", 0)])
def test_cli_approve_prints_the_downstream_detail(tmp_path, monkeypatch, capsys, fault, status, code):
    db = tmp_path / "rein.db"
    with http_mock(faults=(fault,) if fault else ()) as (url, mock):
        proxy, _, _ = make_proxy(now_utc(), ledger=Ledger(db))  # the CLI approves on the real clock
        aid = queue(proxy, "ads_create_campaign", CAMPAIGN)
        monkeypatch.setenv("REIN_ADS_MCP_TOKEN", DUMMY)
        rc = main(["--db", str(db), "approvals", "approve", aid, "--policy", str(PROXY_POLICY),
                   "--downstream", "http", "--downstream-url", url, "--by", "boss"])
    out = capsys.readouterr().out
    first, second = out.splitlines()[:2]
    assert rc == code and first.startswith(f"{aid}: {status}") and second.startswith("  downstream: ")
    if fault:
        assert mock.write_log == []
    else:
        assert mock.write_log[0]["result"]["id"] in second


def test_transport_failure_on_execution_stays_failed():
    with http_mock() as (url, mock):
        proxy = http_proxy(url)
        aid = queue(proxy, "ads_create_campaign", CAMPAIGN)
    proxy.downstream = HttpDownstream("http://127.0.0.1:9/mcp", DUMMY, timeout=5)
    outcome = approve(proxy, aid)  # creation needs no re-check lookup, so the forward itself fails
    assert outcome.status == "failed" and outcome.detail.startswith("Connect")


@pytest.mark.parametrize("res,expected", [
    (DownstreamResult(True, {"id": "1", "status": "PAUSED"}, '{"id": "1"}'), None),
    (DownstreamResult(True, {"success": True, "id": "1"}, ""), None),
    (DownstreamResult(True, {"error": None, "id": "1"}, ""), None),
    (DownstreamResult(True, None, "error rate is 0.1% today"), None),
    (DownstreamResult(True, None, "Created campaign 123"), None),
    (DownstreamResult(True, None, ""), None),
    (DownstreamResult(False, None, "anything"), "the result is flagged isError"),
    (DownstreamResult(True, {"error": {"message": "x"}}, ""), "the result content carries an `error` field"),
    (DownstreamResult(True, None, '{"errors": [{"message": "x"}]}'), "the result content carries an `errors` field"),
    (DownstreamResult(True, None, '{"success": false}'), "the result content says success: false"),
    (DownstreamResult(True, None, "Error: invalid parameter"), "the result text is an error message"),
    (DownstreamResult(True, None, "failed - quota"), "the result text is an error message"),
])
def test_error_content_detection_is_narrow(res, expected):
    assert error_content(res) == expected


# --------------------------------------------------------------------------- verdicts unchanged, migration


def test_scenario_verdicts_are_unchanged_with_error_modes_on():
    data = scenarios()
    for fault in ("jsonrpc-errors", "echo-token"):
        for spec in data["calls"]:
            clock = spec.get("clock", data["default_clock"])
            with http_mock(faults=(fault,)) as (url, _):
                got = call(http_proxy(url, clock), spec["tool"], spec["arguments"])
            reference, _, _ = make_proxy(clock)
            want = call(reference, spec["tool"], spec["arguments"])
            if fault == "jsonrpc-errors":
                assert decision_of(got) == decision_of(want), (fault, spec["id"])
            else:
                # Every downstream call fails. A call that was refused or queued is never let through; a call that
                # was forwarded is forwarded (and fails) or, if it needed a current value, queued.
                if decision_of(want) in ("deny", "require_approval"):
                    assert decision_of(got) in ("deny", "require_approval"), spec["id"]
                else:
                    assert decision_of(got) in ("forwarded-error", "require_approval"), spec["id"]


def test_old_ledger_schema_is_migrated_without_touching_rows(tmp_path):
    db = tmp_path / "old.db"
    with Ledger(db) as ledger:
        proxy, _, _ = make_proxy(ledger=ledger)
        aid = queue(proxy, "ads_update_entity", BUMP)
        old_sql = ledger.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'approvals'").fetchone()[0]
    conn = sqlite3.connect(db)
    legacy = old_sql.replace(",\n                                           'execution_failed'", "")
    assert "execution_failed" not in legacy
    conn.executescript(f"ALTER TABLE approvals RENAME TO a_old; {legacy}; INSERT INTO approvals SELECT * FROM a_old; "
                       f"DROP TABLE a_old;")
    conn.close()
    with Ledger(db) as ledger:
        sql = ledger.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'approvals'").fetchone()[0]
        assert "execution_failed" in sql
        row = ledger.get_approval(aid)
        assert row["status"] == "pending" and row["arguments"] == BUMP
        assert ledger.transition_approval(aid, "pending", "execution_failed")
