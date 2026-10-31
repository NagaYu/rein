"""Run the five planted scenarios end to end and print ledger, findings, alerts and revert plans.

Everything runs against fixtures and the mock client; nothing touches the network.
Run: .venv/bin/python scripts/demo.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rein.alerts import FileAlertSink, WebhookAlertSink  # noqa: E402
from rein.cli import format_change  # noqa: E402
from rein.client import MockAdsClient  # noqa: E402
from rein.diff import diff_snapshots  # noqa: E402
from rein.ledger import Ledger  # noqa: E402
from rein.pipeline import check, ingest  # noqa: E402
from rein.policy import load_policy  # noqa: E402
from rein.revert import execute_plan, plan_for_changes  # noqa: E402
from rein.snapshot import take_snapshot  # noqa: E402

SCENARIOS = ROOT / "fixtures" / "scenarios"
POLICY = ROOT / "configs" / "policy.example.yaml"
TZ = ZoneInfo("Asia/Tokyo")


def rule(title: str, char: str = "=") -> None:
    print(f"\n{char * 78}\n{title}\n{char * 78}")


def run(name: str, workdir: Path) -> dict:
    info = json.loads((SCENARIOS / name / "scenario.json").read_text(encoding="utf-8"))
    rule(f"シナリオ {name}: {info['title']}")
    policy = load_policy(POLICY)
    alerts_path = workdir / f"{name}.alerts.jsonl"
    ledger = Ledger(":memory:")
    ingest(ledger, MockAdsClient(SCENARIOS / name / "before"), policy.attribution)
    ingest(ledger, MockAdsClient(SCENARIOS / name / "after"), policy.attribution)
    results = check(ledger, policy, [FileAlertSink(alerts_path), WebhookAlertSink(policy.alerts.webhook_url_env)])

    print("\n--- 台帳（rein feed）")
    for change in ledger.feed():
        print(format_change(change, TZ))

    flagged = ledger.feed(flagged_only=True)
    print("\n--- アラート（ファイル追記。webhook は環境変数未設定のため skipped）")
    if alerts_path.exists():
        for line in alerts_path.read_text(encoding="utf-8").splitlines():
            a = json.loads(line)
            print(f"[{a['severity']}] change #{a['change_id']}: {a['summary']}")
            for reason in a["reasons"]:
                print(f"    理由: {reason}")
            print(f"    主体: {a['actor_kind']}（{a['attribution_basis']}）  巻き戻し: {a['revert']['command']}")
    else:
        print("（アラートなし）")

    violations = [c for c in flagged if c.severity == "violation"]
    targets = violations or flagged
    restored = None
    if targets:
        plan = plan_for_changes(targets, ledger)
        print("\n--- 巻き戻し計画（dry-run 表示）" + ("" if violations else "  ※警告のみ。必要時に人が判断して実行"))
        print(plan.render())
        mock = MockAdsClient(SCENARIOS / name / "after")
        step_results = execute_plan(plan, mock)
        print("\n--- モッククライアントで実行")
        for r in step_results:
            print(f"  {r.order}. {r.status}  {r.detail}")
        remaining = diff_snapshots(take_snapshot(MockAdsClient(SCENARIOS / name / "before")), take_snapshot(mock))
        leftovers = [c for c in remaining if c.change_type != "entity_created"]
        restored = not leftovers
        created = [f"{c.entity_type} {c.entity_id}" for c in remaining if c.change_type == "entity_created"]
        print(f"\n  復元確認（変更前スナップショットとの差分）: {'OK' if restored else 'NG'}"
              + (f"  ※新規作成物は自動削除しない: {', '.join(created)}" if created else ""))

    return {
        "name": name,
        "title": info["title"],
        "changes": len(results),
        "violations": sum(1 for c in flagged if c.severity == "violation"),
        "warnings": sum(1 for c in flagged if c.severity == "warning"),
        "actors": sorted({c.actor_kind for c in flagged}) or ["-"],
        "restored": restored,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="rein-demo-") as tmp:
        summary = [run(p.name, Path(tmp)) for p in sorted(SCENARIOS.iterdir()) if p.is_dir()]
    rule("まとめ")
    print(f"{'scenario':<20} {'changes':>7} {'viol':>5} {'warn':>5}  {'actor(flagged)':<24} restored")
    for s in summary:
        restored = "-" if s["restored"] is None else ("OK" if s["restored"] else "NG")
        print(f"{s['name']:<20} {s['changes']:>7} {s['violations']:>5} {s['warnings']:>5}  {','.join(s['actors']):<24} {restored}")
    print("\nRein は証跡と統制の道具であり、広告成果や法的責任を保証するものではありません。")


if __name__ == "__main__":
    main()
