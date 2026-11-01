"""Phase 5C: the interpreter refitted to the real Ads MCP argument names (discover-live.json)."""

from __future__ import annotations

import json

import pytest

from http_helpers import DUMMY, closed_port_url, misbehaving
from proxy_helpers import ACCOUNT, AD2, AS1, AS2, CR1, CR2, PROXY_POLICY, call, decision_of, make_proxy
from rein.cli import main
from rein.creative import extract_claims, load_creative_rules, view_from_raw
from rein.proxy import AGENT_METADATA, InProcessDownstream, LOOKUP_ARGUMENTS, creative_raw_from_args

from conftest import ROOT

META = {"advertiser_job": "job-1", "advertiser_request": "raise the budget a little",
        "client_conversation_id": "conv-1", "scheduled_task_run": False}
C1 = "120000000000000101"


def rein_of(result):
    return (result.structuredContent or {}).get("rein") or {}


def rules_of(result):
    return [r["rule_id"] for r in rein_of(result).get("reasons", [])]


def update(entity_id, entity_type, fields=None, **extra):
    args = {"ad_account_id": ACCOUNT, "entity_id": entity_id, "entity_type": entity_type, **extra}
    if fields is not None:
        args["fields"] = fields
    return args


class Recording(InProcessDownstream):
    def __init__(self, mock):
        super().__init__(mock)
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, json.loads(json.dumps(arguments))))
        return await super().call(name, arguments)


def recording_proxy():
    proxy, mock, clock = make_proxy()
    rec = Recording(mock)
    proxy.downstream = rec
    return proxy, mock, rec


# --------------------------------------------------------------------------- agent metadata


def test_agent_metadata_is_accepted_forwarded_and_ignored():
    proxy, mock, rec = recording_proxy()
    plain = decision_of(call(make_proxy()[0], "ads_update_entity", update(AS2, "adset", {"daily_budget": 4400})))
    result = call(proxy, "ads_update_entity", update(AS2, "adset", {"daily_budget": 4400}, **META))
    assert decision_of(result) == plain == "forwarded"
    forwarded = [a for n, a in rec.calls if n == "ads_update_entity"]
    assert forwarded == [update(AS2, "adset", {"daily_budget": 4400}, **META)]  # unchanged, metadata included
    audit = proxy.ledger.rows("proxy_audit")[0]
    assert "client_conversation_id" in audit["arguments_summary"] and audit["reasons"] == "[]"


@pytest.mark.parametrize("extra", [{"hide_ui": True}, {"confirmation_token": "x"}, {"advertiser_jobs": "typo"}])
def test_other_unknown_fields_still_make_a_write_uninterpretable(extra):
    proxy, mock, _ = make_proxy()
    result = call(proxy, "ads_update_entity", update(AS2, "adset", {"daily_budget": 4400}, **META, **extra))
    assert decision_of(result) == "deny" and rules_of(result) == ["rein:uninterpretable"] and mock.write_log == []


def test_metadata_set_matches_the_real_report():
    live = {t["name"]: t for t in json.loads((ROOT / "discover-live.json").read_text(encoding="utf-8"))["tools"]}
    carrying = [n for n, t in live.items() if t["schema"] and AGENT_METADATA <= set(t["schema"]["properties"])]
    assert len(carrying) >= 90


# --------------------------------------------------------------------------- account and entity type


def test_bare_digit_account_is_normalised_and_bad_ones_denied():
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_update_entity", {**update(AS2, "adset", {"daily_budget": 4400}),
                                          "ad_account_id": "100000000000001"})
    assert decision_of(r) == "forwarded"
    for bad in ("acct_1", "act_", 100000000000001, ""):
        r = call(proxy, "ads_update_entity", {**update(AS2, "adset", {"daily_budget": 4400}), "ad_account_id": bad})
        assert decision_of(r) == "deny", bad


def test_account_reaches_the_change_records():
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_update_entity", update(AS1, "adset", {"daily_budget": 9000}))
    approval = proxy.ledger.get_approval(rein_of(r)["approval_id"])
    assert approval["arguments"]["ad_account_id"] == ACCOUNT


@pytest.mark.parametrize("spelling", ["adset", "ad_set", "ADSET", "AD_SET"])
def test_entity_type_spellings(spelling):
    proxy, _, _ = make_proxy()
    assert decision_of(call(proxy, "ads_update_entity", update(AS2, spelling, {"daily_budget": 4400}))) == "forwarded"


@pytest.mark.parametrize("bad", ["ad set", "adsets", "creative", None, 3])
def test_bad_entity_types_are_denied(bad):
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_update_entity", update(AS2, bad, {"daily_budget": 4400}))
    assert decision_of(r) == "deny" and rules_of(r) == ["rein:uninterpretable"]


def test_entity_type_is_required_now():
    proxy, _, _ = make_proxy()
    args = update(AS2, "adset", {"daily_budget": 4400})
    del args["entity_type"]
    assert decision_of(call(proxy, "ads_update_entity", args)) == "deny"


# --------------------------------------------------------------------------- lookups


def test_lookup_argument_sets():
    proxy, _, rec = recording_proxy()
    call(proxy, "ads_update_entity", update(AD2, "ad", {"creative_id": CR1}))
    lookups = [(n, a) for n, a in rec.calls if n in LOOKUP_ARGUMENTS]
    entity = [a for n, a in lookups if n == "ads_get_ad_entities"]
    creatives = [a for n, a in lookups if n == "ads_get_creatives"]
    assert entity and creatives
    for a in entity:
        assert set(a) == {"ad_account_id", "object_ids", "fields"} and "level" not in a
        assert a["ad_account_id"] == ACCOUNT and a["object_ids"] == [AD2]
    assert entity[0]["fields"] == ["id", "name", "status", "adset_id", "creative"]
    for a in creatives:
        assert set(a) == {"ad_account_id", "creative_ids", "fields"}
    assert sorted(a["creative_ids"][0] for a in creatives) == sorted([CR1, CR2])


def test_lookup_sends_the_account_as_the_agent_wrote_it():
    proxy, _, rec = recording_proxy()
    call(proxy, "ads_update_entity", {**update(AS2, "adset", {"daily_budget": 4400}), "ad_account_id": "100000000000001"})
    lookup = next(a for n, a in rec.calls if n == "ads_get_ad_entities")
    assert lookup["ad_account_id"] == "100000000000001"


# --------------------------------------------------------------------------- ads_update_entity without fields


@pytest.mark.parametrize("fields", [None, {}])
def test_fieldless_update_needs_approval(fields):
    proxy, mock, _ = make_proxy()
    r = call(proxy, "ads_update_entity", update(AS2, "adset", fields))
    assert decision_of(r) == "require_approval" and rules_of(r) == ["rein:no-field-changes"]
    assert mock.write_log == []


def test_fieldless_update_of_a_missing_entity_is_still_denied():
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_update_entity", update("999999", "adset"))
    assert decision_of(r) == "deny" and rules_of(r) == ["rein:unresolvable"]


# --------------------------------------------------------------------------- creates


def campaign(**kw):
    return {"ad_account_id": ACCOUNT, "campaign_name": "冬の新作", "objective": "OUTCOME_AWARENESS",
            "buying_type": "AUCTION", **kw}


def test_campaign_budget_rules_read_the_real_budget_names():
    proxy, _, _ = make_proxy()
    over = call(proxy, "ads_create_campaign", campaign(campaign_daily_budget=60001))
    assert decision_of(over) == "deny" and "campaign-daily-cap" in rules_of(over)
    at_cap = call(proxy, "ads_create_campaign", campaign(campaign_lifetime_budget=60000))
    assert decision_of(at_cap) == "require_approval" and rules_of(at_cap) == ["new-campaign-needs-approval"]
    old = call(proxy, "ads_create_campaign", campaign(daily_budget=60001))  # the pre-5C name is now unknown
    assert decision_of(old) == "deny" and rules_of(old) == ["rein:uninterpretable"]


def ad_set(**kw):
    return {"ad_account_id": ACCOUNT, "campaign_id": C1, "ad_set_name": "s", "optimization_goal": "REACH",
            "billing_event": "IMPRESSIONS", "targeting": {"geo_locations": {"countries": ["JP"]}}, **kw}


def test_ad_set_benign_optional_and_unknown_optionals():
    proxy, _, _ = make_proxy()
    assert decision_of(call(proxy, "ads_create_ad_set", ad_set(adlabels=[{"name": "q4"}]))) == "forwarded"
    for extra in ({"bid_strategy": "LOWEST_COST_WITH_BID_CAP"}, {"promoted_object": {}}, {"daily_spend_cap": 1}):
        r = call(proxy, "ads_create_ad_set", ad_set(**extra))
        assert decision_of(r) == "deny" and rules_of(r) == ["rein:uninterpretable"], extra
    missing = ad_set()
    del missing["targeting"]
    assert decision_of(call(proxy, "ads_create_ad_set", missing)) == "deny"


def ad(**kw):
    return {"ad_account_id": ACCOUNT, "ad_set_id": AS1, "ad_name": "a", **kw}


def banned_creative(mock):
    return mock.call("ads_create_creative", {"ad_account_id": ACCOUNT, "name": "x",
                                             "link_url": "https://shop.example.com/autumn/marron",
                                             "message": "絶対おいしい"})["id"]


def test_create_ad_reads_the_creative_reference():
    proxy, mock, _ = make_proxy()
    clean = call(proxy, "ads_create_ad", ad(creative={"creative_id": CR1}))
    assert decision_of(clean) == "forwarded"
    bad = call(proxy, "ads_create_ad", ad(creative={"creative_id": banned_creative(mock)}))
    assert decision_of(bad) == "deny" and "banned-claims" in rules_of(bad)


@pytest.mark.parametrize("creative", [None, {"creative_id": CR1, "name": "inline"},
                                      {"object_story_spec": {"link_data": {"link": "https://x.example.net"}}},
                                      "120000000000000401"])
def test_create_ad_without_a_readable_creative_needs_approval(creative):
    proxy, mock, _ = make_proxy()
    r = call(proxy, "ads_create_ad", ad() if creative is None else ad(creative=creative))
    assert decision_of(r) == "require_approval" and "rein:creative-not-resolved" in rules_of(r)
    assert mock.write_log == []


def test_create_ad_with_unknown_creative_id_is_denied():
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_create_ad", ad(creative={"creative_id": "999999"}))
    assert decision_of(r) == "deny" and rules_of(r) == ["rein:unresolvable"]


def creative(**kw):
    return {"ad_account_id": ACCOUNT, "link_url": "https://shop.example.com/x", **kw}


def test_creative_plural_texts_are_all_checked():
    proxy, _, _ = make_proxy()
    clean = call(proxy, "ads_create_creative", creative(primary_texts=["焼きたて", "朝の一枚"], headlines=["秋", "冬"]))
    assert decision_of(clean) == "forwarded"
    hidden = call(proxy, "ads_create_creative", creative(message="焼きたて", primary_texts=["朝の一枚", "絶対おいしい"]))
    assert decision_of(hidden) == "deny" and "banned-claims" in rules_of(hidden)
    title = call(proxy, "ads_create_creative", creative(headlines=["秋", "ｎｏ．１の栗"]))
    assert decision_of(title) == "deny"


@pytest.mark.parametrize("args", [creative(link_url="javascript:alert(1)"), creative(primary_texts=[]),
                                  creative(primary_texts="one"), creative(headlines=["ok", ""]),
                                  creative(link="https://shop.example.com/x"), creative(call_to_action="SHOP_NOW"),
                                  creative(video_id="1"), creative(object_story_id="1_2")])
def test_creative_shapes_rein_cannot_read_are_denied(args):
    proxy, _, _ = make_proxy()
    r = call(proxy, "ads_create_creative", args)
    assert decision_of(r) == "deny" and rules_of(r) == ["rein:uninterpretable"]


def test_creative_with_account_only_is_allowed():
    # Required is only ad_account_id; the ad that uses it is checked when it is created.
    proxy, _, _ = make_proxy()
    assert decision_of(call(proxy, "ads_create_creative", {"ad_account_id": ACCOUNT})) == "forwarded"


def test_plural_creative_feeds_claim_extraction():
    raw = creative_raw_from_args(creative(message="秋の新作", primary_texts=["満足度90%", "売上200%アップ"],
                                          headlines=["No.1の栗"], call_to_action_type="SHOP_NOW"))
    view = view_from_raw({"id": "1", **raw})
    assert view["message"] == ["秋の新作", "満足度90%", "売上200%アップ"] and view["link"] == "https://shop.example.com/x"
    texts = {c["text"] for c in extract_claims(view, load_creative_rules(ROOT / "configs" / "creative_rules.example.yaml"))}
    assert {"90%", "200%アップ", "no.1"} <= texts


def test_singular_creative_keeps_the_link_data_shape():
    raw = creative_raw_from_args(creative(message="m", headline="h", description="d", call_to_action_type="LEARN_MORE",
                                          image_hash="abc", page_id="990000000000009"))
    assert raw["object_story_spec"] == {"page_id": "990000000000009", "link_data": {
        "message": "m", "name": "h", "description": "d", "link": "https://shop.example.com/x",
        "call_to_action": {"type": "LEARN_MORE"}, "image_hash": "abc"}}


# --------------------------------------------------------------------------- allowlisted reads


@pytest.mark.parametrize("tool,args", [
    ("ads_account_get_activity_logs", {"ad_account_id": ACCOUNT}),
    ("ads_get_ad_images", {"ad_account_id": ACCOUNT}),
    ("ads_get_ad_videos", {"ad_account_id": ACCOUNT}),
    ("ads_get_ad_account_pages", {"ad_account_id": ACCOUNT}),
    ("ads_get_ig_accounts", {"ad_account_id": ACCOUNT}),
    ("ads_get_custom_audience", {"custom_audience_id": "1"}),
])
def test_new_reads_pass_through(tool, args):
    proxy, mock, _ = make_proxy()
    call(proxy, tool, {**args, **META})
    row = proxy.ledger.rows("proxy_audit")[0]
    assert row["decision"] == "passthrough" and row["forwarded"] == 1 and mock.write_log == []


def test_other_real_reads_stay_denied():
    proxy, _, _ = make_proxy()
    for tool in ("ads_insights_performance_trend", "ads_catalog_list_products", "ads_pixel_event_read",
                 "ads_get_datasets", "ads_log_ui_interaction"):
        assert decision_of(call(proxy, tool, {"ad_account_id": ACCOUNT})) == "deny", tool


# --------------------------------------------------------------------------- discover errors print cleanly


@pytest.mark.parametrize("label", ["401", "unreachable"])
def test_discover_failure_is_one_clean_line(monkeypatch, capsys, label):
    monkeypatch.setenv("REIN_ADS_MCP_TOKEN", DUMMY)
    if label == "401":
        with misbehaving("401") as url:
            code = main(["proxy", "discover", "--policy", str(PROXY_POLICY), "--downstream", "http",
                         "--downstream-url", url, "--downstream-timeout", "5"])
        expected = "error: downstream failure: unauthorized (401) (nothing was executed)"
    else:
        code = main(["proxy", "discover", "--policy", str(PROXY_POLICY), "--downstream", "http",
                     "--downstream-url", closed_port_url(), "--downstream-timeout", "5"])
        expected = "error: downstream failure: unreachable (nothing was executed)"
    err = capsys.readouterr().err
    assert code == 1 and err.strip() == expected and "Traceback" not in err and DUMMY not in err


def test_discover_stdio_failure_is_classified(capsys, tmp_path):
    code = main(["proxy", "discover", "--policy", str(PROXY_POLICY), "--mock-fixture", str(tmp_path / "missing")])
    err = capsys.readouterr().err
    assert code == 1 and err.startswith("error: downstream failure:") and "Traceback" not in err


# --------------------------------------------------------------------------- scripts/proxy_call.py


def run_proxy_call(tmp_path, *argv):
    import subprocess
    import sys
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "proxy_call.py"), "--db", str(tmp_path / "r.db"),
                           "--policy", str(PROXY_POLICY), *argv], capture_output=True, text=True, env=env, timeout=60)


def test_proxy_call_sends_one_read_through_the_proxy(tmp_path):
    from rein.ledger import Ledger
    proc = run_proxy_call(tmp_path, "--mock-fixture", str(ROOT / "fixtures" / "base"), "--tool", "ads_get_ad_accounts")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.splitlines()[0]) == {"decision": "forwarded", "downstream_ok": True}
    with Ledger(tmp_path / "r.db") as ledger:
        [row] = ledger.rows("proxy_audit")
    assert row["decision"] == "passthrough" and json.loads(row["declared_client"])["name"] == "rein-proxy-call"


def test_proxy_call_refuses_writes_by_default(tmp_path):
    proc = run_proxy_call(tmp_path, "--mock-fixture", str(ROOT / "fixtures" / "base"), "--tool", "ads_update_entity",
                          "--args", json.dumps(update(AS2, "adset", {"daily_budget": 4400})))
    assert proc.returncode == 2 and "refused" in proc.stderr and not (tmp_path / "r.db").exists()


def test_proxy_call_write_is_still_decided_by_the_proxy(tmp_path):
    proc = run_proxy_call(tmp_path, "--mock-fixture", str(ROOT / "fixtures" / "base"), "--tool", "ads_update_entity",
                          "--allow-write", "--args", json.dumps(update(AS1, "adset", {"daily_budget": 50000})))
    assert proc.returncode == 1 and json.loads(proc.stdout)["decision"] == "deny"


def test_proxy_call_without_token_is_one_clean_line(tmp_path):
    proc = run_proxy_call(tmp_path, "--downstream", "http", "--downstream-url", "https://ads-mcp.example.com/mcp",
                          "--tool", "ads_get_ad_accounts")
    assert proc.returncode == 2 and proc.stdout == ""
    assert proc.stderr.strip().startswith("error: REIN_ADS_MCP_TOKEN is not set") and "Traceback" not in proc.stderr
    assert not (tmp_path / "r.db").exists()


def test_proxy_call_reports_a_proxy_that_stops_without_a_traceback(tmp_path):
    proc = run_proxy_call(tmp_path, "--mock-fixture", str(tmp_path / "missing"), "--tool", "ads_get_ad_accounts")
    # The child processes may print their own errors; this script ends with one line of its own.
    assert proc.returncode == 1 and proc.stderr.strip().splitlines()[-1].startswith("error: the proxy process stopped")
