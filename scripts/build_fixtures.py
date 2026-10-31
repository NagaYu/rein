"""Generate the recorded-response fixtures and the planted-violation scenarios.

Everything here is fictional: the business, IDs, numbers, people and applications.
Domains use example.com / example.net (reserved for documentation).

Run: .venv/bin/python scripts/build_fixtures.py
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "fixtures"
API_VERSION = "v26.0"
ACCOUNT = "act_100000000000001"
PAGE_ID = "990000000000009"

T_BEFORE = "2026-10-27T00:00:00Z"  # Tue 09:00 JST
T_AFTER = "2026-10-28T00:00:00Z"   # Wed 09:00 JST
BASE_UPDATED = "2026-10-20T10:00:00+0900"

# Fictional actors / applications. Their classification is declared in configs/policy.example.yaml.
ADS_MANAGER_APP = ("900000000000001", "Ads Manager (fixture label)")
THIRD_PARTY_APP = ("900000000000002", "Example AI Optimizer (fictional third-party)")
META_AI_APP = ("900000000000003", "Advantage+ creative (fixture label)")
HUMAN_ACTOR = ("990000000000555", "担当者A (fictional)")
BOT_ACTOR = ("990000000000777", "Example Optimizer Bot (fictional)")
META_ACTOR = ("990000000000888", "Meta (fixture label)")

C1, C2 = "120000000000000101", "120000000000000102"
AS1, AS2, AS3 = "120000000000000201", "120000000000000202", "120000000000000203"
AD1, AD2, AD3, AD4 = "120000000000000301", "120000000000000302", "120000000000000303", "120000000000000304"
CR1, CR2, CR3, CR4, CR5, CR6 = (f"12000000000000040{i}" for i in range(1, 7))


def creative(cid: str, name: str, link: str, message: str, headline: str, description: str,
             image_hash: str | None = None, page_id: str = PAGE_ID) -> dict:
    link_data = {
        "link": link,
        "message": message,
        "name": headline,
        "description": description,
        "call_to_action": {"type": "SHOP_NOW"},
    }
    if image_hash:
        link_data["image_hash"] = image_hash
    return {
        "id": cid,
        "name": name,
        "status": "ACTIVE",
        "object_story_spec": {"page_id": page_id, "link_data": link_data},
    }


def base_account() -> dict:
    targeting_kanto = {"geo_locations": {"regions": [{"key": "fixture-kanto"}]}, "age_min": 25, "age_max": 44}
    targeting_kansai = {"geo_locations": {"regions": [{"key": "fixture-kansai"}]}, "age_min": 25, "age_max": 44}
    return {
        "campaigns": [
            {"id": C1, "name": "秋の新作パン_認知", "objective": "OUTCOME_AWARENESS", "status": "ACTIVE",
             "effective_status": "ACTIVE", "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
             "special_ad_categories": [], "updated_time": BASE_UPDATED},
            {"id": C2, "name": "定番商品_リターゲティング", "objective": "OUTCOME_SALES", "status": "ACTIVE",
             "effective_status": "ACTIVE", "daily_budget": "30000", "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
             "special_ad_categories": [], "updated_time": BASE_UPDATED},
        ],
        "adsets": [
            {"id": AS1, "name": "首都圏_25-44", "campaign_id": C1, "status": "ACTIVE", "effective_status": "ACTIVE",
             "daily_budget": "5000", "billing_event": "IMPRESSIONS", "optimization_goal": "REACH",
             "targeting": targeting_kanto, "start_time": "2026-10-01T00:00:00+0900", "updated_time": BASE_UPDATED},
            {"id": AS2, "name": "関西_25-44", "campaign_id": C1, "status": "ACTIVE", "effective_status": "ACTIVE",
             "daily_budget": "4000", "billing_event": "IMPRESSIONS", "optimization_goal": "REACH",
             "targeting": targeting_kansai, "start_time": "2026-10-01T00:00:00+0900", "updated_time": BASE_UPDATED},
            {"id": AS3, "name": "サイト訪問者_30日", "campaign_id": C2, "status": "ACTIVE", "effective_status": "ACTIVE",
             "billing_event": "IMPRESSIONS", "optimization_goal": "OFFSITE_CONVERSIONS",
             "targeting": {"custom_audiences": [{"id": "fixture-aud-1"}]},
             "start_time": "2026-09-15T00:00:00+0900", "updated_time": BASE_UPDATED},
        ],
        "ads": [
            {"id": AD1, "name": "栗デニッシュ_静止画", "adset_id": AS1, "campaign_id": C1, "status": "ACTIVE",
             "effective_status": "ACTIVE", "creative": {"id": CR1}, "updated_time": BASE_UPDATED},
            {"id": AD2, "name": "さつまいもブレッド_静止画", "adset_id": AS1, "campaign_id": C1, "status": "ACTIVE",
             "effective_status": "ACTIVE", "creative": {"id": CR2}, "updated_time": BASE_UPDATED},
            {"id": AD3, "name": "栗デニッシュ_関西", "adset_id": AS2, "campaign_id": C1, "status": "ACTIVE",
             "effective_status": "ACTIVE", "creative": {"id": CR3}, "updated_time": BASE_UPDATED},
            {"id": AD4, "name": "定番食パン_リタゲ", "adset_id": AS3, "campaign_id": C2, "status": "ACTIVE",
             "effective_status": "ACTIVE", "creative": {"id": CR4}, "updated_time": BASE_UPDATED},
        ],
        "adcreatives": [
            creative(CR1, "栗デニッシュ_v1", "https://shop.example.com/autumn/marron",
                     "秋限定、栗のデニッシュ。毎朝焼きたてをお届けします。", "栗のデニッシュ", "Example Bakery（架空）"),
            creative(CR2, "さつまいも_v1", "https://shop.example.com/autumn/satsumaimo",
                     "ほくほくのさつまいもを練り込んだ秋のパン。", "さつまいもブレッド", "Example Bakery（架空）"),
            creative(CR3, "栗デニッシュ_関西_v1", "https://shop.example.com/autumn/marron?area=kansai",
                     "関西のお店でも、秋限定の栗デニッシュ。", "栗のデニッシュ", "Example Bakery（架空）"),
            creative(CR4, "定番食パン_v1", "https://shop.example.com/classic/shokupan",
                     "いつもの食パン、まとめ買いがお得です。", "定番の食パン", "Example Bakery（架空）"),
        ],
    }


def activity(event_type: str, object_id: str, object_type: str, event_time: str,
             actor: tuple[str, str], app: tuple[str, str], extra: dict | None = None) -> dict:
    return {
        "event_type": event_type,
        "translated_event_type": event_type.replace("_", " "),
        "event_time": event_time,
        "object_id": object_id,
        "object_type": object_type,
        "actor_id": actor[0],
        "actor_name": actor[1],
        "application_id": app[0],
        "application_name": app[1],
        "extra_data": json.dumps(extra or {}, ensure_ascii=False),
    }


def find(rows: list[dict], entity_id: str) -> dict:
    return next(r for r in rows if r["id"] == entity_id)


def scenario_budget_spike(acct: dict) -> tuple[list[dict], dict]:
    s = find(acct["adsets"], AS1)
    s["daily_budget"] = "50000"
    s["updated_time"] = "2026-10-27T14:05:00+0900"
    acts = [activity("update_ad_set_budget", AS1, "CAMPAIGN", "2026-10-27T14:05:00+0900",
                     BOT_ACTOR, THIRD_PARTY_APP, {"old_value": 5000, "new_value": 50000})]
    return acts, {
        "title": "予算が上限を超えて跳ね上がる",
        "expected": [{"entity_type": "adset", "entity_id": AS1, "field": "daily_budget",
                      "severity": "violation", "rules": ["adset-daily-cap"], "actor_kind": "external_agent"}],
    }


def scenario_url_swap(acct: dict) -> tuple[list[dict], dict]:
    acct["adcreatives"].append(creative(
        CR5, "さつまいも_v2", "https://shop.example.net/autumn/satsumaimo",
        "ほくほくのさつまいもを練り込んだ秋のパン。", "さつまいもブレッド", "Example Bakery（架空）"))
    ad = find(acct["ads"], AD2)
    ad["creative"] = {"id": CR5}
    ad["updated_time"] = "2026-10-27T11:30:00+0900"
    # No activity recorded for this change -> attribution must stay unknown.
    return [], {
        "title": "リンク先 URL が差し替わる（クリエイティブ差し替え経由）",
        "expected": [{"entity_type": "ad", "entity_id": AD2, "field": "creative_resolved.link",
                      "severity": "violation", "rules": ["frozen-landing-url"], "actor_kind": "unknown"}],
    }


def scenario_prohibited_term(acct: dict) -> tuple[list[dict], dict]:
    acct["adcreatives"].append(creative(
        CR6, "栗デニッシュ_関西_auto", "https://shop.example.com/autumn/marron?area=kansai",
        # Full-width "ｎｏ．１" on purpose: matching is NFKC + case-insensitive.
        "【ｎｏ．１】食べるだけで絶対やせる栗デニッシュ。", "栗のデニッシュ", "Example Bakery（架空）"))
    ad = find(acct["ads"], AD3)
    ad["creative"] = {"id": CR6}
    ad["updated_time"] = "2026-10-27T16:20:00+0900"
    acts = [activity("update_ad_creative", AD3, "ADGROUP", "2026-10-27T16:20:00+0900",
                     META_ACTOR, META_AI_APP)]
    return acts, {
        "title": "文面に禁止語句が入る",
        "expected": [
            {"entity_type": "ad", "entity_id": AD3, "field": "creative_resolved.message",
             "severity": "violation", "rules": ["banned-claims"], "actor_kind": "meta_ai"},
            {"entity_type": "creative", "entity_id": CR6, "field": "*",
             "severity": "violation", "rules": ["banned-claims"], "actor_kind": "unknown"},
        ],
    }


def scenario_off_hours(acct: dict) -> tuple[list[dict], dict]:
    s = find(acct["adsets"], AS2)
    s["status"] = "PAUSED"
    s["effective_status"] = "PAUSED"
    s["updated_time"] = "2026-10-28T03:12:00+0900"
    acts = [activity("update_ad_set_run_status", AS2, "CAMPAIGN", "2026-10-28T03:12:00+0900",
                     HUMAN_ACTOR, ADS_MANAGER_APP, {"old_value": "ACTIVE", "new_value": "PAUSED"})]
    return acts, {
        "title": "想定時間外の変更",
        "expected": [{"entity_type": "adset", "entity_id": AS2, "field": "status",
                      "severity": "warning", "rules": ["business-hours"], "actor_kind": "human"}],
    }


def scenario_normal_change(acct: dict) -> tuple[list[dict], dict]:
    s = find(acct["adsets"], AS1)
    s["daily_budget"] = "8000"
    s["updated_time"] = "2026-10-27T13:00:00+0900"
    acts = [activity("update_ad_set_budget", AS1, "CAMPAIGN", "2026-10-27T13:00:00+0900",
                     HUMAN_ACTOR, ADS_MANAGER_APP, {"old_value": 5000, "new_value": 8000})]
    return acts, {
        "title": "正常な変更（違反にならない）",
        "expected": [{"entity_type": "adset", "entity_id": AS1, "field": "daily_budget",
                      "severity": None, "rules": [], "actor_kind": "human"}],
    }


SCENARIOS = {
    "1_budget_spike": scenario_budget_spike,
    "2_url_swap": scenario_url_swap,
    "3_prohibited_term": scenario_prohibited_term,
    "4_off_hours": scenario_off_hours,
    "5_normal_change": scenario_normal_change,
}


def write_account(path: Path, acct: dict, captured_at: str, activities: list[dict], account: str = ACCOUNT) -> None:
    path.mkdir(parents=True, exist_ok=True)
    meta = {"account_id": account, "api_version": API_VERSION, "captured_at": captured_at,
            "currency": "JPY", "timezone_name": "Asia/Tokyo",
            "note": "Fictional recorded responses for tests. Not real data."}
    (path / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for edge, rows in acct.items():
        body = {"data": rows, "paging": {"cursors": {"before": "fixture", "after": "fixture"}}}
        (path / f"{edge}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (path / "activities.json").write_text(
        json.dumps({"data": activities}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- Phase 3

def evidence_after(base: dict) -> tuple[dict, list[dict]]:
    """Account 1 one day later, for the evidence-pack demo: the two budget changes the Phase 2 proxy
    forwarded (no activity entry, so Watch cannot attribute them), plus changes by other actors."""
    acct = copy.deepcopy(base)
    s1 = find(acct["adsets"], AS1)
    s1["daily_budget"], s1["updated_time"] = "9000", "2026-10-27T11:00:00+0900"   # proxy: approved call
    s2 = find(acct["adsets"], AS2)
    s2["daily_budget"], s2["updated_time"] = "4400", "2026-10-27T10:00:00+0900"   # proxy: allowed call
    c2 = find(acct["campaigns"], C2)
    c2["daily_budget"], c2["updated_time"] = "70000", "2026-10-27T15:00:00+0900"
    s3 = find(acct["adsets"], AS3)
    s3["status"], s3["effective_status"], s3["updated_time"] = "PAUSED", "PAUSED", "2026-10-28T03:12:00+0900"
    acts = [
        activity("update_campaign_budget", C2, "CAMPAIGN_GROUP", "2026-10-27T15:00:00+0900",
                 BOT_ACTOR, THIRD_PARTY_APP, {"old_value": 30000, "new_value": 70000}),
        activity("update_ad_set_run_status", AS3, "CAMPAIGN", "2026-10-28T03:12:00+0900",
                 HUMAN_ACTOR, ADS_MANAGER_APP, {"old_value": "ACTIVE", "new_value": "PAUSED"}),
    ]
    acts += scenario_url_swap(acct)[0] + scenario_prohibited_term(acct)[0]
    return acct, acts


CREATIVE_ACCOUNT = "act_100000000000002"
CREATIVE_PAGE = "990000000000019"
K_C1, K_C2 = "130000000000000101", "130000000000000102"
K_AS1, K_AS2 = "130000000000000201", "130000000000000202"
K_AD1, K_AD2, K_AD3 = "130000000000000301", "130000000000000302", "130000000000000303"
K_CR = {i: f"13000000000000040{i}" for i in range(1, 8)}


def creative_account() -> dict:
    def ad(aid, name, adset, campaign, cid):
        return {"id": aid, "name": name, "adset_id": adset, "campaign_id": campaign, "status": "ACTIVE",
                "effective_status": "ACTIVE", "creative": {"id": cid}, "updated_time": BASE_UPDATED}

    def adset(sid, name, campaign, budget):
        return {"id": sid, "name": name, "campaign_id": campaign, "status": "ACTIVE", "effective_status": "ACTIVE",
                "daily_budget": budget, "billing_event": "IMPRESSIONS", "optimization_goal": "REACH",
                "targeting": {"geo_locations": {"countries": ["JP"]}, "age_min": 25, "age_max": 54},
                "start_time": "2026-10-01T00:00:00+0900", "updated_time": BASE_UPDATED}

    return {
        "campaigns": [
            {"id": K_C1, "name": "秋のパン_認知", "objective": "OUTCOME_AWARENESS", "status": "ACTIVE",
             "effective_status": "ACTIVE", "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "special_ad_categories": [],
             "updated_time": BASE_UPDATED},
            {"id": K_C2, "name": "うるおいドリンク_獲得", "objective": "OUTCOME_SALES", "status": "ACTIVE",
             "effective_status": "ACTIVE", "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "special_ad_categories": [],
             "updated_time": BASE_UPDATED},
        ],
        "adsets": [adset(K_AS1, "パン_全国_25-54", K_C1, "6000"), adset(K_AS2, "ドリンク_全国_25-54", K_C2, "6000")],
        "ads": [
            ad(K_AD1, "栗デニッシュ_静止画", K_AS1, K_C1, K_CR[1]),
            ad(K_AD2, "さつまいもブレッド_静止画", K_AS1, K_C1, K_CR[2]),
            ad(K_AD3, "うるおいドリンク_静止画", K_AS2, K_C2, K_CR[3]),
        ],
        "adcreatives": [
            creative(K_CR[1], "栗デニッシュ_v1", "https://shop.example.com/autumn/marron",
                     "秋限定、栗のデニッシュ。毎朝焼きたてをお届けします。※写真はイメージです。", "栗のデニッシュ",
                     "Example Bakery（架空）", "fixtureimg0401", CREATIVE_PAGE),
            creative(K_CR[2], "さつまいも_v1", "https://shop.example.com/autumn/satsumaimo",
                     "ほくほくのさつまいもを練り込んだ秋のパン。※写真はイメージです。", "さつまいもブレッド",
                     "Example Bakery（架空）", "fixtureimg0402", CREATIVE_PAGE),
            creative(K_CR[3], "うるおいドリンク_v1", "https://wellness.example.com/drink",
                     "毎朝の一杯で、すこやかな習慣を。※個人の感想です。", "Example Wellness うるおいドリンク",
                     "Example Wellness（架空）", "fixtureimg0403", CREATIVE_PAGE),
        ],
    }


def creative_after(base: dict) -> tuple[dict, list[dict]]:
    """AI rewrites swapped onto two ads, plus one new creative no ad uses yet."""
    acct = copy.deepcopy(base)
    acct["adcreatives"] += [
        creative(K_CR[5], "栗デニッシュ_auto", "https://shop.example.com/autumn/marron",
                 "【ｎｏ．１】絶対においしい栗のデニッシュ。毎朝焼きたてをお届けします。", "栗のデニッシュ",
                 "Example Bakery（架空）", "fixtureimg0401", CREATIVE_PAGE),
        creative(K_CR[6], "うるおいドリンク_auto", "https://wellness.example.com/drink",
                 "毎朝の一杯で、うるおい2倍。※個人の感想です。", "Example Wellness うるおいドリンク",
                 "Example Wellness（架空）", "fixtureimg0403", CREATIVE_PAGE),
        creative(K_CR[7], "試作_未使用", "https://shop.example.com/autumn/new",
                 "新食感の栗パン、今だけ半額。", "新作の栗パン", "Example Bakery（架空）", "fixtureimg0407", CREATIVE_PAGE),
    ]
    a1 = find(acct["ads"], K_AD1)
    a1["creative"], a1["updated_time"] = {"id": K_CR[5]}, "2026-10-28T16:00:00+0900"
    a3 = find(acct["ads"], K_AD3)
    a3["creative"], a3["updated_time"] = {"id": K_CR[6]}, "2026-10-28T17:30:00+0900"
    acts = [activity("update_ad_creative", K_AD1, "ADGROUP", "2026-10-28T16:00:00+0900", META_ACTOR, META_AI_APP)]
    return acct, acts  # the K_AD3 swap has no activity entry: its actor stays unknown


def main() -> None:
    for generated in (ROOT / "base", ROOT / "scenarios", ROOT / "evidence" / "after",
                      ROOT / "creative" / "base", ROOT / "creative" / "after"):
        if generated.exists():
            shutil.rmtree(generated)
    base = base_account()
    write_account(ROOT / "base", base, T_BEFORE, [])
    for name, build in SCENARIOS.items():
        after = copy.deepcopy(base)
        acts, info = build(after)
        sdir = ROOT / "scenarios" / name
        write_account(sdir / "before", base, T_BEFORE, [])
        write_account(sdir / "after", after, T_AFTER, acts)
        (sdir / "scenario.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    after, acts = evidence_after(base)
    write_account(ROOT / "evidence" / "after", after, T_AFTER, acts)
    kbase = creative_account()
    write_account(ROOT / "creative" / "base", kbase, "2026-10-28T00:00:00Z", [], CREATIVE_ACCOUNT)
    kafter, kacts = creative_after(kbase)
    write_account(ROOT / "creative" / "after", kafter, "2026-10-29T00:00:00Z", kacts, CREATIVE_ACCOUNT)
    print(f"wrote fixtures under {ROOT}")


if __name__ == "__main__":
    main()
