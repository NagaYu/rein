"""Evidence pack (Phase 3): a timestamped export of what Rein recorded in a period.

    manifest.json        generation time, period, accounts, Rein version/commit, sha256 of every file
    timeline.json/.csv   Watch ledger (changes, alerts, reverts) + proxy audit/approvals, merged in time order
    policy/              copies of the policy files given at export time
    creative_checks.json Creative Check results in the period
    summary.md           human-readable summary; every number is computed from timeline / creative_checks

Times are written exactly as the ledger stored them (ISO 8601 with offset); nothing is converted.
What the hashes prove: a pack changed *after* export fails `verify`. They say nothing about whether
the ledger itself was altered before export.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .alerts import summarize
from .attribution import ACTOR_KINDS
from .creative import EXTRACTION_LIMIT, NOT_LEGAL
from .ledger import ChangeRecord, Ledger
from .timeutil import iso, now_utc, parse_time

SOURCE_ORDER = {"watch.change": 0, "watch.alert": 1, "watch.revert_run": 2, "proxy.audit": 3, "proxy.approval": 4}
CSV_COLUMNS = ("time", "source", "ref", "account_id", "entity_type", "entity_id", "event", "field", "actor_kind",
               "attribution_basis", "severity", "decision", "status", "rules", "related", "summary")
PROXY_DECISIONS = ("allow", "passthrough", "deny", "require_approval")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "executed", "aborted", "failed")
CREATIVE_BANDS = ("high", "medium", "low")

INTEGRITY_NOTE = ("manifest.json のハッシュが保証するのは、エクスポート後にパックが改変されたことの検出までです。"
                  "エクスポート前の台帳（SQLite）自体が改変されていないことは保証しません。")
POLICY_NOTE = ("policy/ はエクスポート時に指定された policy ファイルのコピーです。台帳はどの時点でどの policy が"
               "有効だったかを記録していないため、期間中に policy を変更していた場合は当時の内容と一致しません。")
CORRELATION_NOTE = ("突き合わせは「同じエンティティ ID」かつ「プロキシが下流へ転送して成功した時刻が、その変更を"
                    "検出したスナップショット区間（前回取得 < 時刻 <= 今回取得）に入る」ものだけです。"
                    "フィールド単位の一致は確認しておらず、Watch の帰属（主体）も書き換えていません。")


class EvidenceError(ValueError):
    pass


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _within(stamp: str | None, start: datetime, end: datetime) -> bool:
    return stamp is not None and start <= parse_time(stamp) <= end


# --------------------------------------------------------------------------- timeline


def build_timeline(ledger: Ledger, start: datetime, end: datetime) -> list[dict[str, Any]]:
    snapshots = {r["id"]: r for r in ledger.rows("snapshots")}
    entries: list[dict[str, Any]] = []
    changes_by_id: dict[int, dict[str, Any]] = {}

    for row in ledger.rows("changes"):
        if not _within(row["detected_at"], start, end):
            continue
        rec = ChangeRecord.from_row(row)
        findings = rec.findings or []
        entry = {
            "time": row["detected_at"], "source": "watch.change", "ref": f"change:{rec.id}", "id": rec.id,
            "account_id": rec.account_id, "entity_type": rec.entity_type, "entity_id": rec.entity_id,
            "entity_name": rec.entity_name, "event": rec.change_type, "field": rec.field, "before": rec.before,
            "after": rec.after, "actor_kind": rec.actor_kind, "attribution_basis": (rec.attribution or {}).get("basis"),
            "attribution": rec.attribution, "evaluated": rec.evaluated_at is not None, "severity": rec.severity,
            "rules": [f["rule_id"] for f in findings], "findings": findings, "summary": summarize(rec),
            "interval": {"from_snapshot": rec.from_snapshot_id, "from": snapshots[rec.from_snapshot_id]["taken_at"],
                         "to_snapshot": rec.to_snapshot_id, "to": snapshots[rec.to_snapshot_id]["taken_at"]},
            "related": [],
        }
        changes_by_id[rec.id] = entry
        entries.append(entry)

    for row in ledger.rows("alerts"):
        if not _within(row["created_at"], start, end):
            continue
        payload = _load(row["payload"])
        change = changes_by_id.get(row["change_id"])
        entries.append({
            "time": row["created_at"], "source": "watch.alert", "ref": f"alert:{row['id']}", "id": row["id"],
            "account_id": payload.get("account_id"), "entity_type": change and change["entity_type"],
            "entity_id": change and change["entity_id"], "event": payload.get("kind"), "severity": payload.get("severity"),
            "rules": payload.get("rules") or [], "summary": payload.get("summary"),
            "related": [f"change:{row['change_id']}"], "payload": payload, "deliveries": _load(row["deliveries"]),
        })

    for row in ledger.rows("revert_runs"):
        if not _within(row["created_at"], start, end):
            continue
        change_ids, results = _load(row["change_ids"]), _load(row["results"])
        statuses = ", ".join(f"{r.get('order')}:{r.get('status')}" for r in results)
        entries.append({
            "time": row["created_at"], "source": "watch.revert_run", "ref": f"revert_run:{row['id']}", "id": row["id"],
            "account_id": next((changes_by_id[c]["account_id"] for c in change_ids if c in changes_by_id), None),
            "event": "revert_run",
            "summary": f"巻き戻し計画を {row['target']} に対して実行（{statuses}）", "target": row["target"],
            "related": [f"change:{c}" for c in change_ids], "plan": _load(row["plan"]), "results": results,
        })

    audits = []
    for row in ledger.rows("proxy_audit"):
        if not _within(row["occurred_at"], start, end):
            continue
        reasons = _load(row["reasons"]) or []
        entry = {
            "time": row["occurred_at"], "source": "proxy.audit", "ref": f"audit:{row['id']}", "id": row["id"],
            "entity_type": row["entity_type"], "entity_id": row["entity_id"], "event": row["event"],
            "decision": row["decision"], "tool": row["tool"], "declared_client": _load(row["declared_client"]),
            "operator": row["operator"], "approval_id": row["approval_id"], "forwarded": bool(row["forwarded"]),
            "downstream_ok": None if row["downstream_ok"] is None else bool(row["downstream_ok"]),
            "rules": [r.get("rule_id") for r in reasons], "reasons": reasons,
            "arguments_summary": row["arguments_summary"], "arguments_sha256": row["arguments_sha256"],
            "result_summary": row["result_summary"], "summary": f"{row['tool']} {row['arguments_summary']}",
            "related": [],
        }
        audits.append(entry)
        entries.append(entry)

    for row in ledger.rows("approvals"):
        if not _within(row["received_at"], start, end):
            continue
        entries.append({
            "time": row["received_at"], "source": "proxy.approval", "ref": f"approval:{row['id']}", "id": row["id"],
            "entity_type": row["entity_type"], "entity_id": row["entity_id"], "event": "approval",
            "status": row["status"], "status_reason": row["status_reason"], "tool": row["tool"],
            "declared_client": _load(row["declared_client"]), "expires_at": row["expires_at"],
            "decided_at": row["decided_at"], "decided_by": row["decided_by"], "executed_at": row["executed_at"],
            "rules": [r.get("rule_id") for r in _load(row["findings"]) or []],
            "summary": f"{row['tool']} 承認 {row['status']}",
            "related": sorted(a["ref"] for a in audits if a["approval_id"] == row["id"]),
        })

    # Watch <-> proxy: same entity, proxy forward inside the snapshot interval that detected the change.
    for change in changes_by_id.values():
        lo, hi = parse_time(change["interval"]["from"]), parse_time(change["interval"]["to"])
        for audit in audits:
            if (audit["forwarded"] and audit["downstream_ok"] and audit["entity_id"] == change["entity_id"]
                    and lo < parse_time(audit["time"]) <= hi):
                change["related"].append(audit["ref"])
                audit["related"].append(change["ref"])

    entries.sort(key=lambda e: (parse_time(e["time"]), SOURCE_ORDER[e["source"]], str(e["id"])))
    return entries


def timeline_csv(entries: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for e in entries:
        row = []
        for col in CSV_COLUMNS:
            value = e.get(col)
            if col in ("rules", "related"):
                value = ";".join(str(v) for v in value or [])
            row.append("" if value is None else value)
        writer.writerow(row)
    return buf.getvalue()


def creative_checks_in(ledger: Ledger, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [{"check_id": r["id"], "checked_at": r["checked_at"], "trigger": r["trigger"], "account_id": r["account_id"],
             "creative_id": r["creative_id"], "original_id": r["original_id"], "status": r["status"],
             "score": r["score"], "band": r["band"], "change_ids": r["change_ids"], "result": r["result"]}
            for r in ledger.creative_checks() if _within(r["checked_at"], start, end)]


# --------------------------------------------------------------------------- summary


def compute_stats(timeline: list[dict[str, Any]], checks: list[dict[str, Any]]) -> dict[str, Any]:
    changes = [e for e in timeline if e["source"] == "watch.change"]
    calls = [e for e in timeline if e["source"] == "proxy.audit" and e["event"] == "call"]
    approvals = [e for e in timeline if e["source"] == "proxy.approval"]
    high = sorted((c for c in checks if c["band"] == "high"), key=lambda c: (-c["score"], c["check_id"]))
    return {
        "changes_by_actor": {k: sum(1 for c in changes if c["actor_kind"] == k) for k in ACTOR_KINDS},
        "changes_total": len(changes),
        "violations": sum(1 for c in changes if c["severity"] == "violation"),
        "warnings": sum(1 for c in changes if c["severity"] == "warning"),
        "alerts": sum(1 for e in timeline if e["source"] == "watch.alert"),
        "revert_runs": sum(1 for e in timeline if e["source"] == "watch.revert_run"),
        "proxy_calls": {k: sum(1 for c in calls if c["decision"] == k) for k in PROXY_DECISIONS},
        "proxy_calls_total": len(calls),
        "approvals": {k: sum(1 for a in approvals if a["status"] == k) for k in APPROVAL_STATUSES},
        "approvals_total": len(approvals),
        "creative_bands": {k: sum(1 for c in checks if c["band"] == k) for k in CREATIVE_BANDS},
        "creative_no_original": sum(1 for c in checks if c["status"] == "no_original"),
        "creative_total": len(checks),
        "high_risk": [{"check_id": c["check_id"], "creative_id": c["creative_id"], "account_id": c["account_id"],
                       "score": c["score"],
                       "rules": sorted({d["rule_id"] for d in c["result"]["detections"] if d["points"]})} for c in high],
        "correlated_changes": sum(1 for c in changes if c["related"]),
    }


def _table(header: tuple[str, str], rows: list[tuple[str, Any]]) -> list[str]:
    return [f"| {header[0]} | {header[1]} |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in rows]


def render_summary(stats: dict[str, Any], manifest: dict[str, Any]) -> str:
    rein = manifest["rein"]
    lines = [
        "# Rein 証跡パック 要約", "",
        f"- 生成時刻: {manifest['generated_at']}",
        f"- 期間: {manifest['period']['from']} 〜 {manifest['period']['to']}（両端を含む）",
        f"- 対象アカウント: {', '.join(manifest['accounts']) or '（なし）'}",
        f"- Rein: {rein['version']}（commit {rein['commit']}{'、未コミットの変更あり' if rein.get('dirty') else ''}）",
        "- 数字はすべて同じパックの timeline.json / creative_checks.json から数えたものです。", "",
        "> Rein は証跡と統制の道具であり、広告成果や法的責任を保証するものではありません。",
        f"> {NOT_LEGAL}",
        "> 主体（帰属）は Meta のアクティビティ履歴で確認できた範囲だけです。確認できないものは「不明（unknown）」です。",
        "> プロキシが統制できるのはプロキシを経由したエージェントのコールだけです。Meta 純正 AI の内部変更は対象外です。",
        "> プロキシの client は MCP の clientInfo による自己申告で、検証済みの身元ではありません。", "",
        "## 主体別の変更件数", "",
        *_table(("主体", "件数"), [(k, v) for k, v in stats["changes_by_actor"].items()] + [("合計", stats["changes_total"])]),
        "", "## 違反と巻き戻し", "",
        *_table(("項目", "件数"), [("violation", stats["violations"]), ("warning", stats["warnings"]),
                                  ("alert", stats["alerts"]), ("revert_run", stats["revert_runs"])]),
        "", "## プロキシ判定の内訳（受け付けたコール）", "",
        *_table(("判定", "件数"), [(k, v) for k, v in stats["proxy_calls"].items()] + [("合計", stats["proxy_calls_total"])]),
        "", "## 承認の内訳（期間内に受け付けた承認待ち）", "",
        *_table(("状態", "件数"), [(k, v) for k, v in stats["approvals"].items()] + [("合計", stats["approvals_total"])]),
        "", "## Creative Check", "",
        *_table(("帯", "件数"), [(k, v) for k, v in stats["creative_bands"].items()]
                + [("no_original", stats["creative_no_original"]), ("合計", stats["creative_total"])]),
        "", f"> {EXTRACTION_LIMIT}", "", "### リスクの高いクリエイティブ（high）", "",
    ]
    if stats["high_risk"]:
        lines += ["| check | creative | アカウント | スコア | 主な検出 |", "|---:|---|---|---:|---|"]
        lines += [f"| {h['check_id']} | {h['creative_id']} | {h['account_id']} | {h['score']} | {', '.join(h['rules'])} |"
                  for h in stats["high_risk"]]
    else:
        lines.append("（なし）")
    lines += [
        "", "## Watch とプロキシの突き合わせ", "",
        f"- プロキシの転送記録と突き合わせられた変更: {stats['correlated_changes']} 件",
        f"- {CORRELATION_NOTE}",
        "", "## このパックの完全性について", "", f"- {INTEGRITY_NOTE}", f"- {POLICY_NOTE}",
        "- 検証: `rein evidence verify <このディレクトリ>`", "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- export / verify


def rein_build_info() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    info: dict[str, Any] = {"version": __version__, "commit": "unknown", "dirty": None}
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        if head.returncode == 0:
            info["commit"] = head.stdout.strip()
            status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True,
                                    timeout=5)
            info["dirty"] = bool(status.stdout.strip()) if status.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_pack(ledger: Ledger, out_dir: str | Path, start: str, end: str, *, policy_files: list[str | Path],
                clock: Callable[[], datetime] = now_utc, build_info: dict[str, Any] | None = None) -> dict[str, Any]:
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise EvidenceError(f"{out} は空ではありません。新しいディレクトリを指定してください")
    t_start, t_end = parse_time(start), parse_time(end)
    if t_start > t_end:
        raise EvidenceError("--from は --to 以前にしてください")
    names = [Path(p).name for p in policy_files]
    if len(names) != len(set(names)):
        raise EvidenceError(f"policy ファイル名が重複しています: {names}")

    timeline = build_timeline(ledger, t_start, t_end)
    checks = creative_checks_in(ledger, t_start, t_end)
    out.mkdir(parents=True, exist_ok=True)
    (out / "policy").mkdir()
    for p in policy_files:
        shutil.copyfile(p, out / "policy" / Path(p).name)
    (out / "timeline.json").write_text(_dump({"entries": timeline}), encoding="utf-8")
    (out / "timeline.csv").write_text(timeline_csv(timeline), encoding="utf-8")
    (out / "creative_checks.json").write_text(_dump({"notes": [NOT_LEGAL, EXTRACTION_LIMIT], "checks": checks}),
                                              encoding="utf-8")

    accounts = sorted({e["account_id"] for e in timeline if e.get("account_id")}
                      | {c["account_id"] for c in checks if c["account_id"]})
    manifest: dict[str, Any] = {
        "kind": "rein.evidence_pack", "version": 1, "generated_at": iso(clock()),
        "period": {"from": start, "to": end, "inclusive": True}, "accounts": accounts,
        "rein": build_info or rein_build_info(), "hash_algorithm": "sha256",
        "counts": {"timeline_entries": len(timeline), "creative_checks": len(checks)},
        "integrity_note": INTEGRITY_NOTE, "policy_note": POLICY_NOTE,
    }
    (out / "summary.md").write_text(render_summary(compute_stats(timeline, checks), manifest), encoding="utf-8")
    files = sorted(p for p in out.rglob("*") if p.is_file())
    manifest["files"] = [{"path": p.relative_to(out).as_posix(), "sha256": _sha256(p), "bytes": p.stat().st_size}
                         for p in files]
    (out / "manifest.json").write_text(_dump(manifest), encoding="utf-8")
    return manifest


@dataclass
class VerifyResult:
    ok: bool
    checked: int = 0
    problems: list[str] = field(default_factory=list)


def verify_pack(pack_dir: str | Path) -> VerifyResult:
    root = Path(pack_dir)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        listed = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return VerifyResult(False, 0, [f"manifest.json を読めない: {type(exc).__name__}: {exc}"])
    problems, seen = [], set()
    for entry in listed:
        rel = entry.get("path", "") if isinstance(entry, dict) else ""
        if not rel or rel.startswith("/") or ".." in Path(rel).parts or rel == "manifest.json":
            problems.append(f"manifest の不正なパス: {rel!r}")
            continue
        seen.add(rel)
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel}: ファイルが無い")
        elif _sha256(path) != entry.get("sha256"):
            problems.append(f"{rel}: sha256 が一致しない（エクスポート後に改変された）")
        elif path.stat().st_size != entry.get("bytes"):
            problems.append(f"{rel}: サイズが一致しない")
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel != "manifest.json" and rel not in seen:
            problems.append(f"{rel}: manifest に無いファイル（エクスポート後に追加された）")
    return VerifyResult(not problems, len(seen), problems)
