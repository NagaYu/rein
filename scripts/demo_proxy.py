"""Phase 2 demo: a scripted agent sends the scenario calls through the Rein MCP proxy.

    scripted agent --MCP--> Rein proxy --MCP--> mock Ads MCP (in memory, fixtures/base)

Everything runs locally against the mock; nothing touches Meta or the network.
The clock is simulated so the time-window rule is reproducible.
Run: .venv/bin/python scripts/demo_proxy.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import anyio  # noqa: E402
from mcp import types  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from rein.client import MockAdsClient  # noqa: E402
from rein.diff import diff_snapshots  # noqa: E402
from rein.ledger import Ledger  # noqa: E402
from rein.mock_ads_mcp import MockAdsMcp, build_server as build_mock_server  # noqa: E402
from rein.policy import load_policy  # noqa: E402
from rein.proxy import ReinProxy, SessionDownstream, build_proxy_server  # noqa: E402
from rein.snapshot import take_snapshot  # noqa: E402
from rein.timeutil import parse_time  # noqa: E402

POLICY = ROOT / "configs" / "policy.proxy.example.yaml"
BASE = ROOT / "fixtures" / "base"
SCENARIOS = json.loads((ROOT / "fixtures" / "proxy" / "scenarios.json").read_text(encoding="utf-8"))
AGENT = types.Implementation(name="scripted-demo-agent", version="0.1")


class Clock:
    def __init__(self, start: str):
        self.now = parse_time(start)

    def __call__(self):
        return self.now


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def outcome_of(result: types.CallToolResult) -> tuple[str, dict]:
    rein = (result.structuredContent or {}).get("rein")
    if rein:
        return rein["decision"], rein
    return ("forwarded（下流でエラー）" if result.isError else "forwarded"), result.structuredContent or {}


async def main_async(workdir: Path) -> None:
    policy = load_policy(POLICY)
    ledger = Ledger(workdir / "rein.db")
    mock = MockAdsMcp(BASE)
    clock = Clock(SCENARIOS["default_clock"])
    queued: dict[int, str] = {}

    async with create_connected_server_and_client_session(build_mock_server(mock)) as downstream_session:
        proxy = ReinProxy(policy, ledger, SessionDownstream(downstream_session), clock=clock)
        async with create_connected_server_and_client_session(build_proxy_server(proxy), client_info=AGENT) as agent:
            rule("1. 疑似エージェントがプロキシ経由でコールを発行（シナリオ 1〜10）")
            print(f"申告されたクライアント: {AGENT.name} {AGENT.version}（clientInfo の自己申告。検証済みの身元ではない）")
            for spec in SCENARIOS["calls"]:
                clock.now = parse_time(spec.get("clock", SCENARIOS["default_clock"]))
                writes_before = len(mock.write_log)
                result = await agent.call_tool(spec["tool"], spec["arguments"])
                decision, detail = outcome_of(result)
                executed = len(mock.write_log) > writes_before
                forwarded_ok = spec["expected"] in ("allow", "passthrough") and decision == "forwarded"
                ok = "一致" if forwarded_ok or decision == spec["expected"] else "不一致"
                local = clock.now.astimezone(ZoneInfo("Asia/Tokyo"))
                print(f"\n[{spec['id']:>2}] {spec['title']}  ({local:%a %H:%M} JST)")
                print(f"     {spec['tool']} {json.dumps(spec['arguments'], ensure_ascii=False)}")
                print(f"     判定: {decision}   期待: {spec['expected']}  → {ok}   下流の状態変化: {'あり' if executed else 'なし'}")
                for r in detail.get("reasons", []) if isinstance(detail, dict) else []:
                    print(f"       - {r['rule_id']} [{r['decision']}]: {r['reason']}")
                if decision == "require_approval":
                    queued[spec["id"]] = detail["approval_id"]
                    print(f"       承認 ID: {detail['approval_id']}（期限 {detail['expires_at']}）")

    # Approvals use the same proxy object with an in-process downstream (the CLI does the same over stdio).
    from rein.proxy import InProcessDownstream
    approver = ReinProxy(policy, ledger, InProcessDownstream(mock), clock=clock)
    clock.now = parse_time(SCENARIOS["default_clock"])

    rule("2. 承認フロー（シナリオ 11〜14）  ※CLI では rein approvals approve|reject <id>")
    writes = len(mock.write_log)
    out = await approver.approve(queued[3], "運用責任者B")
    print(f"[11] approve {queued[3]}（予算 +80%） → {out.status}: {out.message}  下流の書き込み +{len(mock.write_log) - writes}")
    writes = len(mock.write_log)
    out = await approver.approve(queued[3], "運用責任者B")
    print(f"[14] 同じ ID をもう一度 approve → {out.status}: {out.message}  下流の書き込み +{len(mock.write_log) - writes}")
    out = approver.reject(queued[5], "運用責任者B", "今期は新規キャンペーンを作らない")
    print(f"[12] reject {queued[5]}（新規キャンペーン） → {out.status}: {out.message}")
    out = await approver.approve(queued[5], "運用責任者B")
    print(f"     却下後に approve → {out.status}: {out.message}")
    clock.now = parse_time(SCENARIOS["calls"][5]["clock"]) + timedelta(hours=float(policy.proxy.approval_ttl_hours))
    out = await approver.approve(queued[6], "運用責任者B")
    print(f"[13] 24 時間後に approve {queued[6]}（深夜のステータス変更） → {out.status}: {out.message}")

    rule("3. 下流（モック）の最終状態：初期状態からの差分")
    exported = mock.export_fixture(workdir / "final", "2026-10-30T00:00:00Z")
    for c in diff_snapshots(take_snapshot(MockAdsClient(BASE)), take_snapshot(MockAdsClient(exported))):
        print(f"  {c.entity_type} {c.entity_id}（{c.entity_name}） {c.field}: {c.before} → {c.after}")
    print(f"  下流への書き込み合計: {len(mock.write_log)} 件（allow 1 件 + 承認による実行 1 件）")

    rule("4. 監査ログ（rein audit）")
    for a in ledger.audit_log():
        who = (a["declared_client"] or {}).get("name")
        op = f" operator={a['operator']}" if a.get("operator") else ""
        target = f"{a.get('entity_type') or ''} {a.get('entity_id') or ''}".strip()
        print(f"  #{a['id']:<3} {a['occurred_at']} {a['event']:<17} {a['decision']:<16} {a['tool']:<20} {target:<26}"
              f" client={who}（申告）{op}")

    rule("5. 承認キュー（rein approvals list）")
    for row in ledger.list_approvals():
        print(f"  {row['id']}  {row['status']:<9} {row['tool']}  {row.get('status_reason') or ''}")

    print("\nこのプロキシが統制できるのは、プロキシを経由したエージェントのコールだけです。")
    print("Meta 純正 AI のプラットフォーム内部の変更は経由しないため対象外（Phase 1 の検知＋巻き戻しで扱う）。")
    print("Rein は証跡と統制の道具であり、広告成果や法的責任を保証するものではありません。")


def main() -> None:
    import logging
    logging.getLogger("mcp").setLevel(logging.ERROR)  # the SDK warns when an unlisted tool is called (scenario 9)
    with tempfile.TemporaryDirectory(prefix="rein-proxy-demo-") as tmp:
        anyio.run(main_async, Path(tmp))


if __name__ == "__main__":
    main()
