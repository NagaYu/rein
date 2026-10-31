from __future__ import annotations

import inspect
import json

import httpx
import pytest

import rein.client as client_mod
from rein.alerts import FileAlertSink, WebhookAlertSink, dispatch
from rein.client import MetaGraphReadClient, MockAdsClient, WriteBlockedError
from rein.diff import diff_snapshots
from rein.revert import RealWriteDisabledError, execute_plan, plan_for_changes
from rein.snapshot import take_snapshot

from conftest import SCENARIO_NAMES, SCENARIOS
from helpers import run_scenario

REVERTIBLE = ["1_budget_spike", "2_url_swap", "3_prohibited_term", "4_off_hours"]


def flagged(ledger):
    return ledger.feed(flagged_only=True)


@pytest.mark.parametrize("name", REVERTIBLE)
def test_executing_plan_on_mock_restores_before_state(name):
    ledger, _ = run_scenario(name)
    plan = plan_for_changes(flagged(ledger), ledger)
    mock = MockAdsClient(SCENARIOS / name / "after")
    results = execute_plan(plan, mock)
    assert {r.status for r in results} <= {"applied", "manual_required"}
    assert any(r.status == "applied" for r in results)

    before = take_snapshot(MockAdsClient(SCENARIOS / name / "before"))
    reverted = take_snapshot(mock)
    remaining = diff_snapshots(before, reverted)
    # Only newly created entities may remain: Rein never deletes automatically.
    assert all(c.change_type == "entity_created" for c in remaining), remaining


def test_budget_plan_contents():
    ledger, _ = run_scenario("1_budget_spike")
    plan = plan_for_changes(flagged(ledger), ledger)
    [step] = plan.steps
    assert (step.action, step.phase, step.entity_type, step.entity_id) == ("update", "budget", "adset", "120000000000000201")
    assert step.params == {"daily_budget": 5000}
    assert step.expect == {"daily_budget": 50000}


def test_url_swap_plan_points_ad_back_to_old_creative():
    ledger, _ = run_scenario("2_url_swap")
    plan = plan_for_changes(flagged(ledger), ledger)
    [step] = [s for s in plan.steps if s.action == "update"]
    assert step.params == {"creative": {"creative_id": "120000000000000402"}}
    assert step.expect == {"creative.id": "120000000000000405"}


def test_creative_swap_changes_merge_into_one_step():
    ledger, _ = run_scenario("3_prohibited_term")
    ad_changes = [c for c in ledger.feed() if c.entity_type == "ad"]
    assert {c.field for c in ad_changes} == {"creative.id", "creative_resolved.message"}
    plan = plan_for_changes(ad_changes, ledger)
    assert len(plan.steps) == 1 and len(plan.steps[0].change_ids) == len(ad_changes)


def test_created_creative_is_manual():
    ledger, _ = run_scenario("3_prohibited_term")
    created = [c for c in flagged(ledger) if c.change_type == "entity_created"]
    plan = plan_for_changes(created, ledger)
    assert [s.action for s in plan.steps] == ["manual"]
    assert "手動" in plan.render()


def test_deleted_entity_is_manual(tmp_path):
    from rein.ledger import Ledger
    from rein.pipeline import ingest
    import shutil

    after = tmp_path / "after"
    shutil.copytree(SCENARIOS / "5_normal_change" / "after", after)
    ads = json.loads((after / "ads.json").read_text(encoding="utf-8"))
    ads["data"] = [a for a in ads["data"] if a["id"] != "120000000000000304"]
    (after / "ads.json").write_text(json.dumps(ads), encoding="utf-8")
    ledger = Ledger(":memory:")
    ingest(ledger, MockAdsClient(SCENARIOS / "5_normal_change" / "before"))
    ingest(ledger, MockAdsClient(after))
    deleted = [c for c in ledger.feed() if c.change_type == "entity_deleted"]
    assert len(deleted) == 1
    plan = plan_for_changes(deleted, ledger)
    assert plan.steps[0].action == "manual" and "復元できない" in plan.steps[0].note


def _edit(dirpath, edge, entity_id, **fields):
    path = dirpath / f"{edge}.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    for row in body["data"]:
        if row["id"] == entity_id:
            row.update(fields)
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def test_plan_ordering_pause_budget_creative_activate(tmp_path):
    import shutil
    from rein.ledger import Ledger
    from rein.pipeline import ingest

    before, after = tmp_path / "before", tmp_path / "after"
    shutil.copytree(SCENARIOS / "5_normal_change" / "before", before)
    shutil.copytree(SCENARIOS / "5_normal_change" / "after", after)
    _edit(before, "adsets", "120000000000000203", status="PAUSED")
    _edit(after, "adsets", "120000000000000203", status="ACTIVE")       # revert -> pause (first)
    _edit(after, "adsets", "120000000000000202", status="PAUSED")       # revert -> activate (last)
    _edit(after, "ads", "120000000000000302", creative={"id": "120000000000000401"})  # creative swap
    ledger = Ledger(":memory:")
    ingest(ledger, MockAdsClient(before))
    ingest(ledger, MockAdsClient(after))
    plan = plan_for_changes(ledger.feed(), ledger)
    assert [(s.phase, s.entity_id) for s in plan.steps] == [
        ("pause", "120000000000000203"),
        ("budget", "120000000000000201"),
        ("creative", "120000000000000302"),
        ("activate", "120000000000000202"),
    ]
    assert [s.order for s in plan.steps] == [1, 2, 3, 4]


def test_conflict_when_current_value_drifted():
    ledger, _ = run_scenario("1_budget_spike")
    plan = plan_for_changes(flagged(ledger), ledger)
    mock = MockAdsClient(SCENARIOS / "1_budget_spike" / "after")
    mock.update("adset", "120000000000000201", {"daily_budget": 12345})  # someone changed it again
    [result] = execute_plan(plan, mock)
    assert result.status == "skipped_conflict"
    assert mock.get_entity("adset", "120000000000000201")["daily_budget"] == 12345


# ------------------------------------------------------------------ no real write path


def test_execute_refuses_real_client():
    ledger, _ = run_scenario("1_budget_spike")
    plan = plan_for_changes(flagged(ledger), ledger)
    real = MetaGraphReadClient("act_1", token="t", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(RealWriteDisabledError):
        execute_plan(plan, real)


def test_real_client_has_no_write_methods():
    public = {n for n, _ in inspect.getmembers(MetaGraphReadClient, inspect.isfunction) if not n.startswith("_")}
    assert public == {"account_id", "observed_at", "close", "list_entities", "list_activities"}
    for name in ("update", "post", "delete", "create", "apply", "write"):
        assert not hasattr(MetaGraphReadClient, name)


def test_real_client_transport_blocks_non_get():
    seen = []
    real = MetaGraphReadClient("act_1", token="t", transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={})))
    for method in ("POST", "DELETE", "PUT", "PATCH"):
        with pytest.raises(WriteBlockedError):
            real._http.request(method, "act_1/ads")
    assert seen == []


def test_no_apply_real_path_in_source():
    import rein
    from pathlib import Path
    src = Path(rein.__file__).parent
    text = "\n".join(p.read_text(encoding="utf-8") for p in src.glob("*.py"))
    assert "apply-real" not in text and "apply_real" not in text
    # The only write implementation is the mock's.
    assert inspect.getsource(client_mod).count("def update(") == 2  # AdsWriter protocol + MockAdsClient


# ------------------------------------------------------------------ alerts


def test_alerts_written_to_file_with_plan_reference(tmp_path):
    path = tmp_path / "alerts.jsonl"
    _, results = run_scenario("1_budget_spike", sinks=[FileAlertSink(path), WebhookAlertSink()])
    [alert] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert alert["severity"] == "violation"
    assert "daily_budget" in alert["summary"] and "50000" in alert["summary"]
    assert alert["rules"] == ["adset-daily-cap"]
    assert alert["reasons"] and "上限" in alert["reasons"][0]
    assert alert["revert"]["command"] == f"rein revert --change {alert['change_id']}"
    assert alert["revert"]["steps"][0]["params"] == {"daily_budget": 5000}
    # Webhook env not set in tests -> skipped, nothing sent.
    assert results[0].deliveries[1] == {"sink": "webhook", "status": "skipped", "reason": "REIN_ALERT_WEBHOOK_URL not set"}


def test_warning_alert_has_no_auto_plan(tmp_path):
    path = tmp_path / "alerts.jsonl"
    run_scenario("4_off_hours", sinks=[FileAlertSink(path)])
    [alert] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert alert["severity"] == "warning" and "steps" not in alert["revert"]


def test_webhook_posts_only_when_env_set(monkeypatch):
    posted = []
    transport = httpx.MockTransport(lambda r: posted.append(r) or httpx.Response(204))
    sink = WebhookAlertSink(transport=transport)
    assert sink.send({"x": 1})["status"] == "skipped"
    monkeypatch.setenv("REIN_ALERT_WEBHOOK_URL", "https://hooks.example.com/rein")
    result = sink.send({"x": 1})
    assert result == {"sink": "webhook", "status": "sent", "host": "hooks.example.com", "http_status": 204}
    assert posted[0].method == "POST" and json.loads(posted[0].content) == {"x": 1}


def test_webhook_rejects_plain_http_and_reports_failures(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")
    sink = WebhookAlertSink(transport=httpx.MockTransport(boom))
    monkeypatch.setenv("REIN_ALERT_WEBHOOK_URL", "http://hooks.example.com/rein")
    assert sink.send({})["status"] == "skipped"
    monkeypatch.setenv("REIN_ALERT_WEBHOOK_URL", "https://hooks.example.com/rein")
    assert sink.send({})["status"] == "failed"


def test_dispatch_isolates_broken_sink(tmp_path):
    class Broken:
        name = "broken"
        def send(self, alert):
            raise RuntimeError("x")
    results = dispatch({"a": 1}, [Broken(), FileAlertSink(tmp_path / "a.jsonl")])
    assert [r["status"] for r in results] == ["failed", "written"]
