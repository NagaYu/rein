"""Revert plans: put flagged changes back to their previous values.

Planning works from the ledger alone. Execution is allowed only against `MockAdsClient` in
Phase 1; there is intentionally no code path that sends a revert to the real Marketing API.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .client import ENTITY_TYPES, MockAdsClient, MockWriteError
from .diff import flatten
from .ledger import ChangeRecord, Ledger
from .snapshot import normalize_entity

# Top-level fields we are willing to restore with a plain update. Everything else is manual.
WRITABLE_FIELDS = {
    "campaign": frozenset({"name", "status", "daily_budget", "lifetime_budget", "spend_cap", "bid_strategy"}),
    "adset": frozenset({"name", "status", "daily_budget", "lifetime_budget", "bid_amount", "bid_strategy", "end_time"}),
    "ad": frozenset({"name", "status"}),
    "creative": frozenset(),
}
BUDGET_FIELDS = frozenset({"daily_budget", "lifetime_budget", "spend_cap", "bid_amount"})
STOP_STATUSES = frozenset({"PAUSED", "ARCHIVED"})

# Execution order: stop delivery first, then money, then content, then the rest; re-activation last.
PHASES = ("pause", "budget", "creative", "other", "activate", "manual")


class RealWriteDisabledError(RuntimeError):
    pass


@dataclass
class RevertStep:
    order: int
    phase: str
    action: str  # "update" | "manual"
    entity_type: str
    entity_id: str
    entity_name: str | None
    change_ids: list[int]
    params: dict[str, Any] | None = None    # Graph-style update params (POST /{id})
    expect: dict[str, Any] | None = None    # field path -> value we expect to find right now
    restores: dict[str, Any] | None = None  # field path -> value after the revert
    note: str = ""


@dataclass
class RevertPlan:
    change_ids: list[int]
    steps: list[RevertStep] = field(default_factory=list)

    @property
    def manual_steps(self) -> list[RevertStep]:
        return [s for s in self.steps if s.action == "manual"]

    def to_dict(self) -> dict[str, Any]:
        return {"change_ids": self.change_ids, "steps": [asdict(s) for s in self.steps]}

    def render(self) -> str:
        ids = ", ".join(f"#{i}" for i in self.change_ids)
        lines = [f"巻き戻し計画: 対象変更 {ids}（{len(self.steps)} ステップ、うち手動 {len(self.manual_steps)}）"]
        for s in self.steps:
            label = f"{s.entity_type} {s.entity_id}" + (f"（{s.entity_name}）" if s.entity_name else "")
            refs = ",".join(f"#{i}" for i in s.change_ids)
            if s.action == "update":
                restores = ", ".join(f"{k}: {_short(s.expect.get(k))} → {_short(v)}" for k, v in s.restores.items())
                lines.append(f"  {s.order}. [{s.phase}] {label}  {restores}  (変更 {refs})")
                lines.append(f"     update {json.dumps(s.params, ensure_ascii=False)}  前提: 現在値が {json.dumps(s.expect, ensure_ascii=False)}")
            else:
                lines.append(f"  {s.order}. [手動対応が必要] {label}  (変更 {refs})")
                lines.append(f"     {s.note}")
        return "\n".join(lines)


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class _Draft:
    key: tuple
    phase: str
    action: str
    change: ChangeRecord
    params: dict[str, Any] | None = None
    expect: dict[str, Any] | None = None
    restores: dict[str, Any] | None = None
    note: str = ""


def _manual(change: ChangeRecord, note: str) -> _Draft:
    return _Draft(("manual", change.id), "manual", "manual", change, note=note)


def _draft(change: ChangeRecord, ledger: Ledger) -> _Draft:
    et, eid = change.entity_type, change.entity_id
    if change.change_type == "entity_created":
        return _manual(change, "新規に作成されたエンティティ。戻すべき変更前の値が無いため自動では戻さない。"
                               "停止・削除するかは人が判断する（Phase 1 は削除を自動化しない）。")
    if change.change_type == "entity_deleted":
        return _manual(change, "スナップショットから消えたエンティティ（削除・アーカイブ・閲覧権限の変化のいずれか）。"
                               f"更新 API では復元できない。変更前スナップショット #{change.from_snapshot_id} を基に確認・再作成が必要。")
    if et == "creative":
        return _manual(change, "Meta の広告クリエイティブは作成後に本文・リンク等を更新できない（name/status 等のみ）。"
                               "広告側で旧クリエイティブに差し替えて戻すこと。")
    if et == "ad" and (change.field == "creative.id" or change.field.startswith("creative_resolved.")):
        prev_ad = ledger.load_entity(change.from_snapshot_id, "ad", eid) or {}
        cur_ad = ledger.load_entity(change.to_snapshot_id, "ad", eid) or {}
        old_cr = (prev_ad.get("creative") or {}).get("id")
        new_cr = (cur_ad.get("creative") or {}).get("id")
        if not old_cr:
            return _manual(change, "変更前のクリエイティブ ID が台帳に無いため戻し先を決められない。")
        if old_cr == new_cr:
            return _manual(change, f"クリエイティブ ID {old_cr} は同じまま内容が変わった。クリエイティブは作成後に"
                                   "内容を更新できない前提のため想定外。手動で確認が必要。")
        if ledger.load_entity(change.to_snapshot_id, "creative", old_cr) is None:
            return _manual(change, f"戻し先のクリエイティブ {old_cr} が最新スナップショットに無い（削除された可能性）。"
                                   "新しいクリエイティブの作成が必要。")
        return _Draft((et, eid, "creative"), "creative", "update", change,
                      params={"creative": {"creative_id": old_cr}},
                      expect={"creative.id": new_cr}, restores={"creative.id": old_cr})
    if "." in change.field or change.field not in WRITABLE_FIELDS.get(et, ()):
        return _manual(change, f"{change.field} は Phase 1 の自動巻き戻し対象外。変更前の値: {_short(change.before)}")
    if change.before is None:
        return _manual(change, f"{change.field} は変更前に未設定だった。未設定へ戻す方法はフィールドごとに異なるため手動で対応。")
    if change.field == "status":
        phase = "pause" if change.before in STOP_STATUSES else "activate"
    elif change.field in BUDGET_FIELDS:
        phase = "budget"
    else:
        phase = "other"
    return _Draft((et, eid, change.field), phase, "update", change,
                  params={change.field: change.before},
                  expect={change.field: change.after}, restores={change.field: change.before})


def plan_for_changes(changes: list[ChangeRecord], ledger: Ledger) -> RevertPlan:
    changes = sorted(changes, key=lambda c: (c.detected_at, c.id))
    merged: dict[tuple, tuple[_Draft, list[int]]] = {}
    for change in changes:
        d = _draft(change, ledger)
        if d.key in merged:
            first, ids = merged[d.key]
            # Same target touched by several changes: restore the oldest value, expect the newest.
            first.expect = d.expect
            ids.append(change.id)
        else:
            merged[d.key] = (d, [change.id])

    type_rank = {t: i for i, t in enumerate(ENTITY_TYPES)}
    ordered = sorted(merged.values(), key=lambda item: (
        PHASES.index(item[0].phase), type_rank[item[0].change.entity_type], item[0].change.entity_id, str(item[0].key)))
    plan = RevertPlan(change_ids=[c.id for c in changes])
    for n, (d, ids) in enumerate(ordered, start=1):
        plan.steps.append(RevertStep(
            order=n, phase=d.phase, action=d.action, entity_type=d.change.entity_type,
            entity_id=d.change.entity_id, entity_name=d.change.entity_name, change_ids=ids,
            params=d.params, expect=d.expect, restores=d.restores, note=d.note))
    return plan


@dataclass
class StepResult:
    order: int
    status: str  # applied | skipped_conflict | manual_required | failed
    detail: str = ""


def execute_plan(plan: RevertPlan, client: MockAdsClient) -> list[StepResult]:
    """Run a plan against the mock client. Anything else is refused."""
    if not isinstance(client, MockAdsClient):
        raise RealWriteDisabledError("Phase 1: revert execution is only allowed against MockAdsClient")
    results = []
    for step in plan.steps:
        if step.action == "manual":
            results.append(StepResult(step.order, "manual_required", step.note))
            continue
        current = client.get_entity(step.entity_type, step.entity_id)
        if current is None:
            results.append(StepResult(step.order, "failed", "entity not found"))
            continue
        now = flatten(normalize_entity(step.entity_type, current))
        drift = {k: now.get(k) for k, v in (step.expect or {}).items() if now.get(k) != v}
        if drift:
            results.append(StepResult(step.order, "skipped_conflict",
                                      f"現在値が計画時の想定と異なるため適用しない: {json.dumps(drift, ensure_ascii=False)}"))
            continue
        try:
            client.update(step.entity_type, step.entity_id, step.params)
        except MockWriteError as exc:
            results.append(StepResult(step.order, "failed", str(exc)))
            continue
        results.append(StepResult(step.order, "applied", json.dumps(step.params, ensure_ascii=False)))
    return results
