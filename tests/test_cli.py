"""Smoke test: snapshot -> feed -> check -> revert (dry-run) through the CLI entry point."""

from __future__ import annotations

import json

from rein.cli import main

from conftest import POLICY_EXAMPLE, SCENARIOS


def test_cli_end_to_end(tmp_path, capsys):
    db = str(tmp_path / "rein.db")
    alerts = tmp_path / "alerts.jsonl"
    scen = SCENARIOS / "1_budget_spike"

    assert main(["--db", db, "snapshot", "--fixture", str(scen / "before"), "--policy", str(POLICY_EXAMPLE)]) == 0
    assert "baseline" in capsys.readouterr().out
    assert main(["--db", db, "snapshot", "--fixture", str(scen / "after"), "--policy", str(POLICY_EXAMPLE)]) == 0
    assert "1 change(s) recorded" in capsys.readouterr().out

    assert main(["--db", db, "feed", "--tz", "Asia/Tokyo"]) == 0
    out = capsys.readouterr().out
    assert "daily_budget: 5000 → 50000" in out and "未評価" in out and "external_agent" in out

    assert main(["--db", db, "check", "--policy", str(POLICY_EXAMPLE), "--alerts-file", str(alerts)]) == 0
    out = capsys.readouterr().out
    assert "[VIOLATION]" in out and "1 violation(s)" in out
    assert "webhook: skipped" in out
    assert len(alerts.read_text(encoding="utf-8").splitlines()) == 1

    assert main(["--db", db, "feed", "--flagged", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["severity"] == "violation"

    assert main(["--db", db, "revert", "--change", str(row["id"])]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and '"daily_budget": 5000' in out

    # Re-running check does not re-alert already evaluated changes.
    assert main(["--db", db, "check", "--policy", str(POLICY_EXAMPLE), "--alerts-file", str(alerts)]) == 0
    assert "evaluated 0 change(s)" in capsys.readouterr().out


def test_cli_revert_execute_on_mock(tmp_path, capsys):
    db = str(tmp_path / "rein.db")
    scen = SCENARIOS / "2_url_swap"
    for side in ("before", "after"):
        main(["--db", db, "snapshot", "--fixture", str(scen / side)])
    main(["--db", db, "check", "--policy", str(POLICY_EXAMPLE), "--alerts-file", str(tmp_path / "a.jsonl"), "--no-webhook"])
    capsys.readouterr()
    assert main(["--db", db, "revert", "--flagged", "--execute-on-mock", str(scen / "after")]) == 0
    out = capsys.readouterr().out
    assert "applied" in out and "120000000000000402" in out


def test_cli_real_snapshot_without_token_fails_cleanly(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "x.db"), "snapshot", "--account", "act_1"]) == 1
    assert "REIN_META_ACCESS_TOKEN" in capsys.readouterr().err


def test_cli_has_no_real_apply_option():
    from rein.cli import build_parser
    help_text = build_parser().format_help()
    revert = build_parser()._subparsers._group_actions[0].choices["revert"].format_help()
    assert "apply-real" not in help_text and "apply-real" not in revert
    assert "--execute-on-mock" in revert
