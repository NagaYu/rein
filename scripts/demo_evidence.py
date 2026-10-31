"""Phase 3 demo: Creative Check results and an evidence pack built from Phase 1 + 2 + 3 demo records.

    fixtures + mock clients --> one SQLite ledger --> rein evidence export --> rein evidence verify

Everything runs locally against fixtures and mocks (fictional accounts); nothing touches Meta or the network.
The clock is simulated so the records have reproducible times.
Run: .venv/bin/python scripts/demo_evidence.py
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rein.creative import render  # noqa: E402
from rein.demo_records import PERIOD, build_demo_ledger  # noqa: E402
from rein.evidence import export_pack, verify_pack  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> None:
    logging.getLogger("mcp").setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="rein-evidence-demo-") as tmp:
        work = Path(tmp)
        info = build_demo_ledger(work / "rein.db", ROOT, work)
        ledger = info["ledger"]

        rule("1. デモ用の台帳（Phase 1 Watch・Phase 2 プロキシ・Phase 3 Creative Check の記録）")
        for table in ("snapshots", "changes", "alerts", "revert_runs", "proxy_audit", "approvals"):
            print(f"  {table:<12} {len(ledger.rows(table)):>3} 行")
        print(f"  creative_originals {len(ledger.list_originals()):>3} 行   creative_checks {len(ledger.creative_checks()):>3} 行")
        print(f"  下流モック Ads MCP への書き込み: {info['mock_writes']} 件（allow 1 + 承認後の実行 1）")

        rule("2. Watch が拾った新規・変更クリエイティブの Creative Check（rein check --creative-rules）")
        for watch in info["watch"]:
            for result in watch.creatives:
                print(render(result))
                if result.get("alert"):
                    print(f"  → high 帯のため Phase 1 のアラート経路へ: change #{result['alert']['change_id']}")
                print()

        rule("3. 証跡パックを書き出す（rein evidence export）")
        out = work / "pack"
        manifest = export_pack(ledger, out, *PERIOD, policy_files=info["policy_files"], clock=info["clock"])
        print(f"  期間: {PERIOD[0]} 〜 {PERIOD[1]}  対象アカウント: {', '.join(manifest['accounts'])}")
        print(f"  Rein {manifest['rein']['version']}  commit {manifest['rein']['commit'][:12]}")
        for f in manifest["files"]:
            print(f"  {f['path']:<38} {f['bytes']:>7} bytes  sha256 {f['sha256'][:16]}…")

        rule("4. summary.md")
        print((out / "summary.md").read_text(encoding="utf-8"))

        rule("5. 検証（rein evidence verify）")
        result = verify_pack(out)
        print(f"  無改変のパック: {'OK' if result.ok else 'FAILED'}（{result.checked} ファイルが manifest と一致）")
        tampered = work / "pack-tampered"
        shutil.copytree(out, tampered)
        path = tampered / "summary.md"
        path.write_text(path.read_text(encoding="utf-8").replace("| violation | ", "| violation | 0", 1), encoding="utf-8")
        result = verify_pack(tampered)
        print(f"  summary.md を 1 か所書き換えたコピー: {'OK' if result.ok else 'FAILED'}")
        for problem in result.problems:
            print(f"    - {problem}")
        ledger.close()

    print("\nCreative Check のスコアは法的なコンプライアンス判定ではなく、人が確認する優先順位を決める材料です。")
    print("訴求の抽出は宣言されたパターンに一致したものだけで、言い換えによる回避は検出できません。")
    print("ハッシュが保証するのはエクスポート後の改変の検出までで、エクスポート前の台帳自体の無改変は保証しません。")
    print("Rein は証跡と統制の道具であり、広告成果や法的責任を保証するものではありません。")


if __name__ == "__main__":
    main()
