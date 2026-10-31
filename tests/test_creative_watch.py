"""Creative Check wired into Watch (rein check) and the CLI."""

from __future__ import annotations

import json

import pytest

from conftest import POLICY_EXAMPLE
from creative_helpers import ACCOUNT2, CREATIVE, K_AD1, K_AD3, K_CR1, RULES_PATH, Clock, attributes, registry, rules
from rein.alerts import FileAlertSink
from rein.cli import main
from rein.client import MockAdsClient
from rein.creative import CreativeWatch, creative_view, prohibited_from_policy
from rein.ledger import Ledger
from rein.pipeline import check, ingest, watch_check
from rein.policy import load_policy
from rein.timeutil import parse_time

K_CR5, K_CR6, K_CR7 = "130000000000000405", "130000000000000406", "130000000000000407"


def watched(tmp_path, *, with_policy_terms=False, ledger=None):
    if ledger is None:
        ledger, _ = registry()
    ledger.clock.now = parse_time("2026-10-29T00:00:00Z")
    policy = load_policy(POLICY_EXAMPLE)
    ingest(ledger, MockAdsClient(CREATIVE / "after"), policy.attribution)
    extra = prohibited_from_policy(policy) if with_policy_terms else ()
    sink = FileAlertSink(tmp_path / "alerts.jsonl")
    result = watch_check(ledger, policy, [sink], creative=CreativeWatch(rules(), attributes(), extra))
    return ledger, result, tmp_path / "alerts.jsonl"


def test_watch_checks_new_and_changed_creatives(tmp_path):
    ledger, result, _ = watched(tmp_path)
    by_id = {r["creative_id"]: r for r in result.creatives}
    assert set(by_id) == {K_CR5, K_CR6, K_CR7}
    assert by_id[K_CR5]["band"] == "high" and by_id[K_CR5]["original"]["creative_id"] == K_CR1
    assert by_id[K_CR6]["band"] == "medium" and by_id[K_CR6]["score"] == 25
    assert by_id[K_CR7]["status"] == "no_original" and by_id[K_CR7]["score"] is None
    assert all(r["trigger"] == "watch" for r in result.creatives)
    rows = ledger.creative_checks()
    assert [r["creative_id"] for r in rows] == [r["creative_id"] for r in result.creatives]


def test_high_band_goes_down_the_phase1_alert_path(tmp_path):
    ledger, result, alerts_file = watched(tmp_path)
    high = next(r for r in result.creatives if r["band"] == "high")
    alert = high["alert"]
    assert alert["kind"] == "rein.creative_alert" and alert["severity"] == "high"
    anchor = ledger.get_change(alert["change_id"])
    assert anchor.entity_type == "ad" and anchor.entity_id == K_AD1 and anchor.actor_kind == "meta_ai"
    assert alert in ledger.alerts_for(anchor.id)
    lines = [json.loads(line) for line in alerts_file.read_text(encoding="utf-8").splitlines()]
    assert [a for a in lines if a["kind"] == "rein.creative_alert"] == [alert]
    assert "法的なコンプライアンス判定ではありません" in alert["note"]
    # medium / no_original produce no creative alert.
    assert all("alert" not in r for r in result.creatives if r["band"] != "high")


def test_watch_does_not_recheck_already_evaluated_changes(tmp_path):
    ledger, _, _ = watched(tmp_path)
    again = watch_check(ledger, load_policy(POLICY_EXAMPLE), [], creative=CreativeWatch(rules(), attributes()))
    assert again.changes == [] and again.creatives == []


def test_watch_without_creative_rules_is_phase1_check(tmp_path):
    ledger, _ = registry()
    ingest(ledger, MockAdsClient(CREATIVE / "after"))
    result = watch_check(ledger, load_policy(POLICY_EXAMPLE), [])
    assert result.creatives == [] and ledger.creative_checks() == []


def test_policy_terms_can_be_added_to_watch(tmp_path):
    _, result, _ = watched(tmp_path, with_policy_terms=True)
    cr5 = next(r for r in result.creatives if r["creative_id"] == K_CR5)
    assert "policy:banned-claims" in {d["rule_id"] for d in cr5["detections"]}
    assert cr5["score"] == 100 and cr5["score_breakdown"]["capped"]


def test_lineage_through_watch_history(tmp_path):
    """An original registered without ad ids is still found through the ad's recorded creative.id change."""
    ledger = Ledger(":memory:", clock=Clock("2026-10-28T00:00:00Z"))
    res = ingest(ledger, MockAdsClient(CREATIVE / "base"))
    snap = ledger.load_snapshot(res.snapshot_id)
    ledger.insert_original(registered_at="2026-10-28T00:00:00Z", registered_by={"name": "t", "verified": False},
                           account_id=ACCOUNT2, creative_id=K_CR1, ad_ids=[], snapshot_id=snap.id,
                           fields=creative_view(snap.get("creative", K_CR1)), substantiation=[])
    _, result, _ = watched(tmp_path, ledger=ledger)
    cr5 = next(r for r in result.creatives if r["creative_id"] == K_CR5)
    assert cr5["original_resolution"]["method"] == "ad_lineage"
    assert any("creative.id 変更履歴" in v for v in cr5["original_resolution"]["via"])
    # 403 has no original at all here, so the ad 303 rewrite is "no original".
    assert next(r for r in result.creatives if r["creative_id"] == K_CR6)["status"] == "no_original"


def test_phase1_check_results_are_unchanged_by_creative_watch(tmp_path):
    ledger_a, result_a, _ = watched(tmp_path)
    ledger_b, _ = registry()
    ledger_b.clock.now = parse_time("2026-10-29T00:00:00Z")
    ingest(ledger_b, MockAdsClient(CREATIVE / "after"), load_policy(POLICY_EXAMPLE).attribution)
    plain = check(ledger_b, load_policy(POLICY_EXAMPLE), [])
    assert [(r.change.id, r.evaluation.severity) for r in result_a.changes] == \
           [(r.change.id, r.evaluation.severity) for r in plain]


# --------------------------------------------------------------------------- CLI


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_register_check_and_watch(tmp_path, capsys):
    db = str(tmp_path / "rein.db")
    assert run(capsys, "--db", db, "snapshot", "--fixture", str(CREATIVE / "base"))[0] == 0
    code, out, _ = run(capsys, "--db", db, "creative", "register-original", "--ad", K_AD1, "--by", "担当A",
                       "--memo", "売上200%アップ=資料 EV-0001（架空）")
    assert code == 0 and "自己申告" in out and "根拠メモ 1 件" in out
    assert run(capsys, "--db", db, "creative", "register-original", "--creative", "130000000000000403",
               "--by", "担当A")[0] == 0
    code, out, _ = run(capsys, "--db", db, "creative", "originals")
    assert code == 0 and "売上200%アップ" in out

    candidate = tmp_path / "candidate.json"
    from creative_helpers import build_candidate
    candidate.write_text(json.dumps(build_candidate({"id": "130000000000000480", "from": K_CR1,
                                                     "link_data": {"link": "https://shop.example.net/x"}})),
                         encoding="utf-8")
    code, out, _ = run(capsys, "--db", db, "creative", "check", "--candidate-file", str(candidate), "--ad", K_AD1,
                       "--rules", str(RULES_PATH), "--attributes", str(CREATIVE / "attributes.json"))
    assert code == 0 and "帯: high" in out and "リンク先が原本と異なる" in out and "法的なコンプライアンス判定" in out

    code, out, _ = run(capsys, "--db", db, "creative", "check", "--creative", K_CR1, "--rules", str(RULES_PATH),
                       "--json", "--no-record")
    data = json.loads(out)
    assert code == 0 and data["score"] == 0 and data["original_resolution"]["method"] == "same_creative"
    assert "check_id" not in data

    assert run(capsys, "--db", db, "snapshot", "--fixture", str(CREATIVE / "after"), "--policy", str(POLICY_EXAMPLE))[0] == 0
    code, out, _ = run(capsys, "--db", db, "check", "--policy", str(POLICY_EXAMPLE), "--no-webhook",
                       "--alerts-file", str(tmp_path / "a.jsonl"), "--creative-rules", str(RULES_PATH),
                       "--attributes", str(CREATIVE / "attributes.json"))
    assert code == 0
    assert f"creative {K_CR5}: score=100 band=high  → alert" in out
    assert f"creative {K_CR6}: score=25 band=medium" in out
    assert f"creative {K_CR7}: スコアなし（原本なし）" in out
    with Ledger(db) as ledger:
        assert [r["trigger"] for r in ledger.creative_checks()] == ["cli", "watch", "watch", "watch"]


def test_cli_creative_errors(tmp_path, capsys):
    db = str(tmp_path / "rein.db")
    code, _, err = run(capsys, "--db", db, "creative", "register-original", "--creative", "1", "--by", "x")
    assert code == 1 and "no snapshot" in err
    run(capsys, "--db", db, "snapshot", "--fixture", str(CREATIVE / "base"))
    code, _, err = run(capsys, "--db", db, "creative", "register-original", "--creative", K_CR1, "--memo", "no-equals")
    assert code == 1 and "CLAIM=SOURCE" in err
    code, _, err = run(capsys, "--db", db, "creative", "check", "--creative", "999", "--rules", str(RULES_PATH))
    assert code == 1 and "not found" in err


@pytest.mark.parametrize("ad", [K_AD3])
def test_cli_check_with_category_override(tmp_path, capsys, ad):
    db = str(tmp_path / "rein.db")
    run(capsys, "--db", db, "snapshot", "--fixture", str(CREATIVE / "base"))
    run(capsys, "--db", db, "creative", "register-original", "--ad", ad, "--by", "x")
    from creative_helpers import build_candidate
    path = tmp_path / "c.json"
    path.write_text(json.dumps(build_candidate({"id": "130000000000000481", "from": "130000000000000403",
                                                "link_data": {"message": "必ず痩せる。※個人の感想です。"}})), encoding="utf-8")
    code, out, _ = run(capsys, "--db", db, "creative", "check", "--candidate-file", str(path), "--ad", ad,
                       "--rules", str(RULES_PATH), "--category", "health_beauty", "--json")
    data = json.loads(out)
    assert code == 0 and data["context"]["sources"]["category"] == "explicit"
    assert "health-efficacy-guarantee" in {d["rule_id"] for d in data["detections"]}
