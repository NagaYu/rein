"""End-to-end demo: all three Rein layers in one run, against one demo ledger.

    scripted agent --MCP--> Rein proxy --> mock Ads MCP          (pre-execution control)
    fixtures snapshots --> diff --> ledger --> policy --> revert (Watch + Revert, revert on a mock only)
    new / changed creatives --> Creative Check --> evidence pack --> verify

Everything runs locally against fixtures and mocks (fictional accounts); nothing touches Meta or the network.
The clock is simulated, so every run prints the same records (approval ids are random).
Run: .venv/bin/python scripts/demo_all.py
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rein.demo_records import PERIOD, build_demo_ledger  # noqa: E402
from rein.evidence import build_timeline, export_pack, verify_pack  # noqa: E402
from rein.timeutil import parse_time  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def short(value, width: int = 28) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= width else text[: width - 1] + "…"


def loads(value):
    return json.loads(value) if isinstance(value, str) else value


def rule_ids(reasons) -> str:
    ids = [r["rule_id"] for r in loads(reasons) or []]
    return ", ".join(dict.fromkeys(ids)) or "-"


def main() -> None:
    logging.getLogger("mcp").setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="rein-demo-all-") as tmp:
        work = Path(tmp)
        info = build_demo_ledger(work / "rein.db", ROOT, work)
        ledger = info["ledger"]
        print("Rein end-to-end demo: fixtures and mocks only (fictional accounts, no network).")
        print(f"One SQLite ledger, simulated clock {PERIOD[0]} .. {PERIOD[1]}.")

        rule("1. Baseline: snapshots and approved creative originals")
        for snap in ledger.rows("snapshots"):
            print(f"  snapshot #{snap['id']}  {snap['taken_at']}  account {snap['account_id']}")
        originals = ledger.list_originals()
        per_account = ", ".join(f"{a}: {sum(o['account_id'] == a for o in originals)}"
                                for a in dict.fromkeys(o["account_id"] for o in originals))
        print(f"  creative originals registered: {len(originals)} ({per_account}); the registrant is self-declared")

        rule("2. MCP proxy: an agent's tool calls are decided before they run")
        calls = [r for r in ledger.rows("proxy_audit") if r["event"] == "call"]
        print(f"  {'#':>2}  {'tool':<22} {'target':<20} {'decision':<17} rule(s)")
        for n, row in enumerate(calls, 1):
            target = row["entity_id"] or ("(new entity)" if "_create_" in row["tool"] else "-")
            print(f"  {n:>2}  {row['tool']:<22} {target:<20} {row['decision']:<17} {rule_ids(row['reasons'])}")
        client = loads(calls[0]["declared_client"])
        print(f"  client: {client['name']} {client['version']} (declared via MCP clientInfo, verified={client['verified']})")
        print("  approvals:")
        for row in ledger.rows("approvals"):
            print(f"    {row['id']}  {row['tool']:<20} {short(loads(row['arguments']).get('fields', '-'), 26):<26} "
                  f"-> {row['status']}  (decided {row['decided_at'] or '-'})")
        events = [r for r in ledger.rows("proxy_audit") if r["event"] != "call"]
        print("  approval events in the audit log: " + ", ".join(f"{r['event']}" for r in events))
        print(f"  writes that reached the mock Ads MCP: {info['mock_writes']} (1 allowed call + 1 approved call)")

        rule("3. Watch + Revert: the next snapshot, diffed into the change ledger")
        first = info["watch"][0]
        print(f"  {'id':>3}  {'entity':<9} {'field':<26} {'actor':<15} {'severity':<9} rule(s)")
        for r in first.changes:
            c = r.change
            rules = ", ".join(dict.fromkeys(f.rule_id for f in r.evaluation.findings)) or "-"
            print(f"  {c.id:>3}  {c.entity_type:<9} {short(c.field, 26):<26} {c.actor_kind:<15} "
                  f"{r.evaluation.severity or '-':<9} {rules}")
        print("  (actor is set only when the activity history matches a declared app/actor; otherwise 'unknown')")
        run = ledger.rows("revert_runs")[0]
        print(f"  revert plan for the violations, executed on the mock client only: changes {loads(run['change_ids'])}")
        for step in info["revert"]:
            detail = step.detail if step.detail.startswith("{") else "(not reverted automatically; reason in the ledger)"
            print(f"    step {step.order}: {step.status}  {detail}")

        timeline = build_timeline(ledger, parse_time(PERIOD[0]), parse_time(PERIOD[1]))
        linked = [e for e in timeline if e["source"] == "watch.change" and any(x.startswith("audit:") for x in e["related"])]
        print("  Watch changes that line up with proxy-forwarded calls (same entity, inside the snapshot interval):")
        for e in linked:
            print(f"    {e['ref']:<10} {e['entity_type']} {e['entity_id']} {e['field']}  actor={e['actor_kind']}  "
                  f"<-> {', '.join(x for x in e['related'] if x.startswith('audit:'))}")

        rule("4. Creative Check: new and changed creatives found by Watch")
        print(f"  {'creative':<20} {'original':<20} {'score':>5}  {'band':<7} detections")
        for watch in info["watch"]:
            for res in watch.creatives:
                original = (res.get("original") or {}).get("creative_id") or "none"
                score = "-" if res["score"] is None else res["score"]
                band = res["band"] or res["status"]
                dets = ", ".join(d["rule_id"] for d in res["detections"]) or "-"
                alert = "  -> alert" if res.get("alert") else ""
                print(f"  {res['creative_id']:<20} {original:<20} {score:>5}  {band:<7} {dets}{alert}")
        print("  (high band goes down the Phase 1 alert path; no original -> no score)")

        rule("5. Evidence pack: export, then verify")
        out = work / "pack"
        manifest = export_pack(ledger, out, *PERIOD, policy_files=info["policy_files"], clock=info["clock"])
        print(f"  accounts: {', '.join(manifest['accounts'])}   timeline entries: {len(timeline)}")
        for f in manifest["files"]:
            print(f"  {f['path']:<38} {f['bytes']:>7} bytes  sha256 {f['sha256'][:16]}…")
        result = verify_pack(out)
        print(f"  verify untouched pack: {'OK' if result.ok else 'FAILED'} ({result.checked} files match the manifest)")
        tampered = work / "pack-tampered"
        shutil.copytree(out, tampered)
        path = tampered / "timeline.csv"
        path.write_text(path.read_text(encoding="utf-8").replace("violation", "warning", 1), encoding="utf-8")
        result = verify_pack(tampered)
        print(f"  verify copy with one edited line in timeline.csv: {'OK' if result.ok else 'FAILED'} "
              f"({len(result.problems)} problem(s) reported)")
        ledger.close()

    print("\nNotes")
    print("- Creative Check scores rank what a person should review first; they are not a legal compliance judgement.")
    print("- Claim extraction only finds declared patterns; reworded claims and text inside images are not detected.")
    print("- Pack hashes detect changes made after export; they do not prove the ledger was untouched before export.")
    print("- Rule messages inside the records are in Japanese, the language of the fictional ads they check.")


if __name__ == "__main__":
    main()
