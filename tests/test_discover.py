"""Phase 5A: `rein proxy discover` — tools/list only, deterministic, no credentials in the report."""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import types

from http_helpers import DUMMY, http_mock
from proxy_helpers import BASE, PROXY_POLICY, ROOT
from rein.cli import main
from rein.discover import ListOnlyClientSession, build_report, list_all_tools, load_tools_document, report_json
from rein.downstream_http import DiscoveryViolation, HttpDownstream
from rein.mock_ads_mcp import tool_definitions
from rein.policy import load_policy
from rein.proxy import ASSUMED_ARGUMENTS, KNOWN_WRITE_TOOLS

ASSUMED = ROOT / "fixtures" / "discovery" / "assumed_tools_list.json"   # real names (Phase 5C), types unknown
PRE_5C = ROOT / "fixtures" / "discovery" / "assumed_tools_list.pre5c.json"  # Phase 5A guesses, kept for regression
LIVE = ROOT / "discover-live.json"                                           # the real discovery report
IN_SCOPE_READS = {"ads_get_ad_accounts", "ads_get_ad_entities", "ads_get_creatives", "ads_account_get_activity_logs",
                  "ads_get_ad_images", "ads_get_ad_videos", "ads_get_custom_audience", "ads_get_ad_account_pages",
                  "ads_get_ig_accounts"}


def report_for(path):
    tools, server = load_tools_document(str(path))
    return build_report(tools, load_policy(PROXY_POLICY), downstream={"transport": "file", "endpoint": "x"},
                        server=server)


def assumed_report():
    return report_for(ASSUMED)


def by_name(report):
    return {t["name"]: t for t in report["tools"]}


def gap_codes(entry):
    return [g["code"] for g in entry["gaps"]]


def gaps(entry):
    return {g["code"]: g.get("fields") for g in entry["gaps"]}


# --------------------------------------------------------------------------- the real report (discover-live.json)


def test_live_report_is_what_phase_5b_recorded():
    live = json.loads(LIVE.read_text(encoding="utf-8"))
    assert live["server"] == {"name": "Meta Ads MCP Server", "version": "1.0.0"}
    assert live["summary"]["tools"] == 98
    assert live["summary"]["proxy_outcomes"] == {"passthrough": 3, "evaluated": 6, "denied": 89}
    assert live["not_offered"] == {"read_allowlist": [], "interpreted_writes": []}


def test_refitted_rein_against_the_real_tool_list():
    report = report_for(LIVE)
    s = report["summary"]
    assert s["tools"] == 98
    assert s["proxy_outcomes"] == {"passthrough": 9, "evaluated": 6, "denied": 83}
    assert s["gaps"] == {"read_hint_not_allowlisted": 47, "schema_fields_not_in_rein": 6, "write_without_mapping": 36}
    t = by_name(report)
    # Every lookup, naming and required-set gap on the in-scope tools is closed.
    for name in IN_SCOPE_READS:
        assert t[name]["proxy_outcome"] == "passthrough" and t[name]["gaps"] == [], name
    for name in KNOWN_WRITE_TOOLS:
        assert set(gap_codes(t[name])) == {"schema_fields_not_in_rein"}, name
        assert t[name]["agent_metadata"] == ["advertiser_job", "advertiser_request", "client_conversation_id",
                                             "scheduled_task_run"], name
    assert gaps(t["ads_update_entity"])["schema_fields_not_in_rein"] == [
        "advantage_audience_off_reason", "confirmation_token", "discard_draft_changes", "hide_ui"]


def test_refit_matches_the_real_required_lists():
    live = by_name(json.loads(LIVE.read_text(encoding="utf-8")))
    for name, (accepted, required) in ASSUMED_ARGUMENTS.items():
        real = live[name]["schema"]
        assert required == set(real["required"]), name
        assert accepted <= set(real["properties"]), name  # Rein accepts nothing the real schema lacks


# --------------------------------------------------------------------------- fixtures


def test_fixtures_are_labelled():
    doc = json.loads(ASSUMED.read_text(encoding="utf-8"))
    assert doc["_note"].startswith("REAL NAMES, UNKNOWN TYPES")
    assert {t["name"] for t in doc["tools"]} == IN_SCOPE_READS | KNOWN_WRITE_TOOLS
    assert json.loads(PRE_5C.read_text(encoding="utf-8"))["_note"].startswith("ASSUMED SHAPES")


def test_real_shape_fixture_report():
    report = assumed_report()
    assert report["summary"]["proxy_outcomes"] == {"passthrough": 9, "evaluated": 6, "denied": 0}
    assert report["summary"]["gaps"] == {"schema_fields_not_in_rein": 6}


def test_pre_refit_fixture_regression():
    """The Phase 5A guesses, re-run with the refitted Rein: the 6 allowlisted reads now pass through, and the
    guessed shapes now disagree with Rein in the places the real schemas proved them wrong."""
    report = report_for(PRE_5C)
    assert report["summary"]["proxy_outcomes"] == {"passthrough": 9, "evaluated": 6, "denied": 14}
    t = by_name(report)
    assert gaps(t["ads_create_campaign"])["schema_requires_unknown_to_rein"] == ["account_id", "name"]
    assert gaps(t["ads_create_ad"])["schema_requires_unknown_to_rein"] == ["account_id", "adset_id", "creative_id", "name"]
    assert gap_codes(t["ads_get_ad_entities"]) == ["lookup_args_not_in_schema", "lookup_omits_required_args"]


def test_classification_rules_per_tool():
    t = by_name(report_for(LIVE))
    assert t["ads_get_ad_entities"]["classification_rule"] == "listed in policy proxy.read_tools"
    assert t["ads_update_entity"]["proxy_outcome"] == "evaluated" and t["ads_update_entity"]["interpreter_mapping"]
    # Read-looking tools off the allowlist are still denied; the annotation is only a hint.
    denied_reads = [n for n, e in t.items() if "read_hint_not_allowlisted" in gap_codes(e)]
    assert len(denied_reads) == 47 and all(t[n]["proxy_outcome"] == "denied" for n in denied_reads)
    assert gap_codes(t["ads_boost_ig_post"]) == ["write_without_mapping"]
    assert not t["ads_boost_ig_post"]["interpreter_mapping"]


def test_required_mismatches_and_missing_schema():
    policy = load_policy(PROXY_POLICY)
    tools = [
        {"name": "ads_create_ad", "inputSchema": {"type": "object", "properties": {
            "ad_account_id": {}, "ad_set_id": {}, "ad_name": {}, "status": {}},
            "required": ["ad_account_id", "ad_set_id", "status"]}},
        {"name": "ads_activate_entity"},
        {"name": "ads_get_creatives", "annotations": {"readOnlyHint": False}, "inputSchema": {
            "type": "object", "properties": {"ad_account_id": {}}, "required": []}},
    ]
    t = by_name(build_report(tools, policy, downstream={"transport": "file", "endpoint": "x"}))
    ad = gaps(t["ads_create_ad"])
    assert ad["schema_requires_unknown_to_rein"] == ["status"]
    assert ad["rein_requires_not_required_by_schema"] == ["ad_name"]
    assert ad["rein_fields_not_in_schema"] == ["adlabels", "creative"]
    assert gap_codes(t["ads_activate_entity"]) == ["schema_missing"]
    assert set(gap_codes(t["ads_get_creatives"])) == {"allowlisted_but_hints_write", "lookup_args_not_in_schema"}


def test_not_offered_and_duplicates():
    policy = load_policy(PROXY_POLICY)
    report = build_report([{"name": "ads_get_ad_entities"}, {"name": "ads_get_ad_entities"}], policy,
                          downstream={"transport": "file", "endpoint": "x"})
    assert report["summary"]["duplicate_tool_names"] == ["ads_get_ad_entities"]
    assert report["not_offered"]["interpreted_writes"] == sorted(KNOWN_WRITE_TOOLS)
    assert "ads_get_creatives" in report["not_offered"]["read_allowlist"]


def test_report_is_deterministic_and_order_independent():
    tools, server = load_tools_document(str(LIVE))
    policy = load_policy(PROXY_POLICY)
    a = report_json(build_report(tools, policy, downstream={"transport": "file", "endpoint": "x"}, server=server))
    b = report_json(build_report(list(reversed(tools)), policy, downstream={"transport": "file", "endpoint": "x"},
                                 server=server))
    assert a == b and json.loads(a)["kind"] == "rein.discovery_report"


def test_assumed_argument_table_is_what_the_interpreter_uses():
    # The report compares against ASSUMED_ARGUMENTS; the interpreter must reject anything outside it.
    from proxy_helpers import call, decision_of, make_proxy
    proxy, _, _ = make_proxy()
    for tool, (accepted, required) in ASSUMED_ARGUMENTS.items():
        args = {k: "1" for k in required}
        args.update({"ad_account_id": "act_100000000000001", "not_in_table": 1})
        result = call(proxy, tool, args)
        assert decision_of(result) == "deny"
        assert "未対応の引数" in result.structuredContent["rein"]["reasons"][0]["reason"], tool


# --------------------------------------------------------------------------- live discovery against local mocks


def test_cli_discover_stdio_sends_only_initialize_and_tools_list(monkeypatch, capsys, tmp_path):
    sent = []
    original = ListOnlyClientSession.send_request

    async def record(self, request, *args, **kwargs):
        sent.append(request.root.method)
        return await original(self, request, *args, **kwargs)

    monkeypatch.setattr(ListOnlyClientSession, "send_request", record)
    out = tmp_path / "report.json"
    code = main(["proxy", "discover", "--policy", str(PROXY_POLICY), "--mock-fixture", str(BASE), "--json",
                 "--out", str(out)])
    printed = capsys.readouterr().out
    assert code == 0 and sent == ["initialize", "tools/list"]
    report = json.loads(printed)
    assert report == json.loads(out.read_text(encoding="utf-8"))
    assert report["server"] == {"name": "rein-mock-ads-mcp", "version": "0.2.0"}
    assert report["summary"]["proxy_outcomes"] == {"passthrough": 9, "evaluated": 6, "denied": 0}
    assert report["downstream"]["transport"] == "mock-stdio"


def test_cli_discover_http_never_calls_a_tool(monkeypatch, capsys):
    with http_mock() as (url, mock):
        def forbidden(*_a, **_k):
            raise AssertionError("discovery must not reach tools/call")

        monkeypatch.setattr(mock, "call", forbidden)
        monkeypatch.setenv("REIN_ADS_MCP_TOKEN", DUMMY)
        code = main(["proxy", "discover", "--policy", str(PROXY_POLICY), "--downstream", "http",
                     "--downstream-url", url, "--json"])
        out = capsys.readouterr()
    report = json.loads(out.out)
    assert code == 0 and mock.write_log == []
    assert report["downstream"] == {"transport": "http", "endpoint": url}
    assert DUMMY not in out.out + out.err


def test_cli_discover_http_needs_the_token(monkeypatch, capsys):
    monkeypatch.delenv("REIN_ADS_MCP_TOKEN", raising=False)
    code = main(["proxy", "discover", "--policy", str(PROXY_POLICY), "--downstream", "http",
                 "--downstream-url", "https://ads-mcp.example.com/mcp"])
    assert code == 1 and "REIN_ADS_MCP_TOKEN" in capsys.readouterr().err


def test_list_only_session_refuses_tool_calls():
    with http_mock() as (url, mock):
        downstream = HttpDownstream(url, DUMMY, timeout=10, session_class=ListOnlyClientSession)

        async def try_call(session):
            return await session.call_tool("ads_update_entity", {"entity_id": "1", "fields": {"name": "x"}})

        async def try_raw(session):
            request = types.ClientRequest(types.CallToolRequest(params=types.CallToolRequestParams(
                name="ads_update_entity", arguments={"entity_id": "1", "fields": {"name": "x"}})))
            return await session.send_request(request, types.CallToolResult)

        for op in (try_call, try_raw):
            with pytest.raises(DiscoveryViolation):
                anyio.run(downstream._run, op)
        # The downstream object's own call() goes through the same refusing session.
        with pytest.raises(DiscoveryViolation):
            anyio.run(downstream.call, "ads_update_entity", {"entity_id": "1", "fields": {"name": "x"}})
        tools = anyio.run(downstream._run, list_all_tools)
        assert [t.name for t in tools] == [t.name for t in tool_definitions()]
        assert mock.write_log == []


def test_cli_discover_from_file(capsys):
    assert main(["proxy", "discover", "--policy", str(PROXY_POLICY), "--tools-file", str(ASSUMED)]) == 0
    out = capsys.readouterr().out
    assert "tools: 15   passthrough: 9   evaluated: 6   denied: 0" in out and "Read-only discovery" in out


def test_report_scrubs_a_secret_if_one_ever_appeared():
    report = {"downstream": {"endpoint": f"x {DUMMY}"}}
    assert DUMMY not in report_json(report, DUMMY)


def test_bad_tools_document(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"tools": [{"description": "no name"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="tools/list document"):
        load_tools_document(str(bad))
