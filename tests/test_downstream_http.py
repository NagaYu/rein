"""Phase 5A: the proxy over an MCP streamable-HTTP downstream (local mocks only, dummy token only)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import anyio
import pytest

from http_helpers import DUMMY, ENV, StripAuthorization, closed_port_url, http_mock, misbehaving
from proxy_helpers import AS1, AS2, ACCOUNT, BASE, PROXY_POLICY, ROOT, Clock, approve, call, decision_of, \
    make_proxy, scenarios
from rein.cli import main
from rein.downstream_http import (DownstreamConfigError, DownstreamFailure, HttpDownstream, check_url, classify,
                                  http_downstream_from_env, public_url, scrub_logs, token_from_env)
from rein.ledger import Ledger
from rein.timeutil import now_utc


def http_proxy(url, clock="2026-10-28T05:00:00Z", **kw):
    downstream = HttpDownstream(url, DUMMY, timeout=kw.pop("timeout", 10), **kw)
    proxy, _, clock = make_proxy(clock, downstream=downstream)
    return proxy, clock


def everything_written(proxy) -> str:
    return json.dumps([proxy.ledger.rows(t) for t in ("proxy_audit", "approvals")], ensure_ascii=False)


# --------------------------------------------------------------------------- configuration gate


@pytest.mark.parametrize("env", [{}, {"REIN_ADS_MCP_TOKEN": ""}, {"REIN_ADS_MCP_TOKEN": "   "}])
def test_missing_token_refuses_to_start(env):
    with pytest.raises(DownstreamConfigError, match="REIN_ADS_MCP_TOKEN is not set"):
        token_from_env(env)
    with pytest.raises(DownstreamConfigError):
        http_downstream_from_env("https://ads-mcp.example.com/mcp", env=env)


@pytest.mark.parametrize("url,ok", [
    ("https://ads-mcp.example.com/mcp", True),
    ("http://127.0.0.1:8765/mcp", True),
    ("http://localhost:8765/mcp", True),
    ("http://ads-mcp.example.com/mcp", False),      # plain http off loopback
    ("https://user:pw@ads-mcp.example.com/mcp", False),
    ("ftp://ads-mcp.example.com/mcp", False),
    ("", False),
])
def test_url_rules(url, ok):
    if ok:
        assert check_url(url) == url
    else:
        with pytest.raises(DownstreamConfigError):
            check_url(url)


def test_empty_or_multiline_token_is_refused_by_the_downstream_itself():
    for bad in ("", " ", "a\nb"):
        with pytest.raises(DownstreamConfigError):
            HttpDownstream("http://127.0.0.1:1/mcp", bad)


def test_repr_and_public_url_hide_the_token():
    d = HttpDownstream("https://ads-mcp.example.com/mcp?x=1", DUMMY)
    assert DUMMY not in repr(d) and DUMMY not in str(d.__dict__)
    assert public_url(d.url) == "https://ads-mcp.example.com/mcp"


def test_cli_refuses_http_downstream_without_token(monkeypatch, capsys):
    monkeypatch.delenv("REIN_ADS_MCP_TOKEN", raising=False)
    code = main(["proxy", "--policy", str(PROXY_POLICY), "--downstream", "http",
                 "--downstream-url", "https://ads-mcp.example.com/mcp"])
    err = capsys.readouterr().err
    assert code == 1 and "REIN_ADS_MCP_TOKEN is not set" in err


@pytest.mark.parametrize("argv,message", [
    (["--downstream", "http"], "needs --downstream-url"),
    (["--downstream", "http", "--downstream-url", "https://x.example.com/mcp", "--mock-fixture", "f"], "mock-stdio only"),
    (["--downstream-url", "https://x.example.com/mcp", "--mock-fixture", "f"], "http only"),
    ([], "--mock-fixture is required"),
])
def test_cli_downstream_argument_errors(monkeypatch, capsys, argv, message):
    monkeypatch.setenv("REIN_ADS_MCP_TOKEN", DUMMY)
    code = main(["proxy", "--policy", str(PROXY_POLICY), *argv])
    err = capsys.readouterr().err
    assert code == 1 and message in err and DUMMY not in err


def test_cli_proxy_subprocess_refuses_and_echoes_nothing(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "REIN_ADS_MCP_TOKEN": ""}
    proc = subprocess.run([sys.executable, "-m", "rein.cli", "--db", str(tmp_path / "r.db"), "proxy", "--policy",
                           str(PROXY_POLICY), "--downstream", "http", "--downstream-url", "https://x.example.com/mcp"],
                          capture_output=True, text=True, env={**env, "PYTHONPATH": str(ROOT / "src")}, timeout=60)
    assert proc.returncode == 1 and "fail-closed" in proc.stderr and proc.stdout == ""


# --------------------------------------------------------------------------- same decisions as stdio / in-process


def test_scenario_calls_decide_the_same_over_http():
    data = scenarios()
    for spec in data["calls"]:
        clock = spec.get("clock", data["default_clock"])
        with http_mock() as (url, mock):
            proxy, _ = http_proxy(url, clock)
            result = call(proxy, spec["tool"], spec["arguments"])
            changed = bool(mock.write_log)
        reference, ref_mock, _ = make_proxy(clock)
        expected = call(reference, spec["tool"], spec["arguments"])
        got = decision_of(result)
        want = decision_of(expected)
        assert got == want, spec["id"]
        assert changed == bool(ref_mock.write_log) == spec["downstream_changed"], spec["id"]
        if result.structuredContent and "rein" in result.structuredContent:
            assert result.structuredContent["rein"]["reasons"] == expected.structuredContent["rein"]["reasons"]


def test_approval_executes_once_over_http():
    with http_mock() as (url, mock):
        proxy, _ = http_proxy(url)
        pending = call(proxy, "ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 9000}})
        aid = pending.structuredContent["rein"]["approval_id"]
        first, second = approve(proxy, aid), approve(proxy, aid)
        assert (first.status, first.executed) == ("executed", True)
        assert (second.status, second.executed) == ("executed", False)
        assert len(mock.write_log) == 1 and mock.find("adset", AS1)["daily_budget"] == "9000"
        events = [r["event"] for r in proxy.ledger.rows("proxy_audit")]
        assert events == ["call", "approval_executed", "approval_refused"]


def test_each_operation_is_its_own_session_and_sessions_are_closed():
    with http_mock() as (url, mock):
        proxy, _ = http_proxy(url)
        for _ in range(3):
            call(proxy, "ads_get_ad_entities", {"ad_account_id": "act_100000000000001", "level": "adset", "object_ids": [AS2]})
        result = call(proxy, "ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS2, "entity_type": "adset", "fields": {"daily_budget": 4400}})
        assert decision_of(result) == "forwarded" and len(mock.write_log) == 1


# --------------------------------------------------------------------------- failures are fail-closed


def failing_write_and_read(url, **kw):
    proxy, _ = http_proxy(url, **kw)
    write = call(proxy, "ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS2, "entity_type": "adset", "fields": {"daily_budget": 4400}})
    read = call(proxy, "ads_get_ad_entities", {"ad_account_id": "act_100000000000001", "level": "adset", "object_ids": [AS2]})
    return proxy, write, read


def assert_fail_closed(proxy, write, read, label):
    rein = write.structuredContent["rein"]
    assert rein["decision"] == "require_approval" and rein["executed"] is False
    rules = [r["rule_id"] for r in rein["reasons"]]
    assert "rein:current-unavailable" in rules and "rein:downstream-failure" in rules
    assert any(label in r["reason"] for r in rein["reasons"] if r["rule_id"] == "rein:downstream-failure")
    assert read.isError and read.content[0].text == f"downstream error: {label}"
    rows = proxy.ledger.rows("proxy_audit")
    assert rows[1]["forwarded"] == 1 and rows[1]["downstream_ok"] == 0 and label in rows[1]["result_summary"]
    assert DUMMY not in everything_written(proxy)


def test_missing_header_gets_401_and_nothing_runs():
    with http_mock() as (url, mock):
        proxy, write, read = failing_write_and_read(url, transport=StripAuthorization())
        assert_fail_closed(proxy, write, read, "unauthorized (401)")
        assert mock.write_log == []


def test_unreachable():
    proxy, write, read = failing_write_and_read(closed_port_url())
    assert_fail_closed(proxy, write, read, "unreachable")


def test_timeout():
    with misbehaving("slow") as url:
        proxy, write, read = failing_write_and_read(url, timeout=0.5)
    assert_fail_closed(proxy, write, read, "timeout")


@pytest.mark.parametrize("mode,label", [("garbage-json", "malformed"), ("html", "malformed"),
                                        ("500", "http_status (500)"), ("403", "forbidden (403)")])
def test_malformed_and_http_errors(mode, label):
    with misbehaving(mode) as url:
        proxy, write, read = failing_write_and_read(url, timeout=5)
    assert_fail_closed(proxy, write, read, label)


def test_allowed_create_with_unreachable_downstream_is_not_executed():
    proxy, _ = http_proxy(closed_port_url())
    args = {"ad_account_id": ACCOUNT, "name": "plain", "link_url": "https://shop.example.com/autumn/marron",
            "message": "秋のパンです。"}
    result = call(proxy, "ads_create_creative", args)
    row = proxy.ledger.rows("proxy_audit")[0]
    assert result.isError and row["decision"] == "allow" and row["forwarded"] == 1 and row["downstream_ok"] == 0
    assert row["result_summary"] == "downstream error: unreachable" and row["entity_id"] is None


def test_approval_aborts_when_the_downstream_fails_at_recheck():
    with http_mock() as (url, mock):
        proxy, clock = http_proxy(url)
        pending = call(proxy, "ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 9000}})
        aid = pending.structuredContent["rein"]["approval_id"]
        proxy.downstream = HttpDownstream(url, DUMMY, timeout=10, transport=StripAuthorization())
        outcome = approve(proxy, aid)
        assert outcome.status == "aborted" and not outcome.executed and mock.write_log == []


def test_classify_unknown_errors_as_error():
    assert classify(RuntimeError("x")).failure_class == "error"
    assert classify(ExceptionGroup("g", [ValueError("x")])).failure_class == "error"
    assert classify(DownstreamFailure("timeout")).failure_class == "timeout"


# --------------------------------------------------------------------------- redaction


def test_token_never_reaches_logs_audit_or_output(caplog, capfd):
    caplog.set_level(logging.DEBUG)
    with http_mock() as (url, mock):
        proxy, _ = http_proxy(url)
        for tool, args in (("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS2, "entity_type": "adset", "fields": {"daily_budget": 4400}}),
                           ("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 50000}}),
                           ("ads_get_ad_entities", {"ad_account_id": "act_100000000000001", "level": "adset", "object_ids": [AS1]})):
            call(proxy, tool, args)
        blind = HttpDownstream(url, DUMMY, timeout=10, transport=StripAuthorization())
        with pytest.raises(DownstreamFailure) as info:
            anyio.run(blind.list_tools)
    out = capfd.readouterr()
    produced = "\n".join([caplog.text, out.out, out.err, everything_written(proxy), str(info.value),
                          repr(info.value), json.dumps(mock.write_log)])
    assert caplog.records, "debug logging should have produced records"
    assert DUMMY not in produced


def test_log_scrubber_removes_the_token_if_a_library_logs_it(caplog):
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("some.library")
    with scrub_logs(DUMMY):
        log.debug("headers: Authorization: Bearer %s", DUMMY)
        try:
            raise ValueError(f"bad header {DUMMY}")
        except ValueError:
            log.exception("request failed")
    log.debug("after: %s", "no secret")
    assert DUMMY not in caplog.text and "[redacted]" in caplog.text and "exception details removed" in caplog.text


def test_cli_approve_over_http(tmp_path, monkeypatch, capsys):
    db = tmp_path / "rein.db"
    with http_mock() as (url, mock):
        proxy, _, _ = make_proxy(now_utc(), ledger=Ledger(db))  # the CLI approves on the real clock
        pending = call(proxy, "ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 9000}})
        aid = pending.structuredContent["rein"]["approval_id"]
        monkeypatch.setenv("REIN_ADS_MCP_TOKEN", DUMMY)
        code = main(["--db", str(db), "approvals", "approve", aid, "--policy", str(PROXY_POLICY),
                     "--downstream", "http", "--downstream-url", url, "--by", "boss"])
        out = capsys.readouterr()
        assert code == 0 and "executed" in out.out and len(mock.write_log) == 1
        assert DUMMY not in out.out + out.err
