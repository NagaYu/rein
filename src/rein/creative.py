"""Creative Check (Phase 3): compare a candidate creative with the advertiser-approved original,
extract claims, check them against declared rules, and score the result.

Everything is deterministic and rule-based (declaration file + pattern matching); no LLM.
What it is not: a legal compliance judgement. The score only orders what a person should look
at first. Claims are found only when a declared pattern matches, so rewording evades it.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .alerts import AlertSink, dispatch
from .policy import PolicyError, ProhibitedTermsRule, _fold
from .snapshot import Snapshot, normalize_entity
from .timeutil import iso

if TYPE_CHECKING:
    from .ledger import ChangeRecord, Ledger
    from .policy import Policy

SEVERITIES = ("critical", "major", "minor", "info")
BANDS = ("low", "medium", "high")
CLAIM_KINDS = ("numeric", "price", "superlative", "guarantee", "efficacy", "limited_time")
# Fields of the "creative view": Phase 1's normalized `resolved.*` plus the image hash(es).
VIEW_FIELDS = ("link", "message", "headline", "description", "call_to_action", "image_hash")
ALL_CATEGORIES = "*"
BRAND_SEVERITY = "major"  # missing required phrase / banned word, for every brand

NOT_LEGAL = ("この結果は法的なコンプライアンス判定ではありません。人が確認する優先順位を決めるための材料です。")
EXTRACTION_LIMIT = ("訴求の抽出は宣言されたパターンに一致したものだけです。言い換えによる回避は検出できません。")


# --------------------------------------------------------------------------- rules (declaration file)


@dataclass(frozen=True)
class ClaimPattern:
    id: str
    kind: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class ProhibitedList:
    id: str
    categories: frozenset[str]
    terms: tuple[str, ...]
    severity: str = "critical"
    case_sensitive: bool = False
    source: str = "creative_rules"

    def applies(self, categories: tuple[str, ...]) -> bool:
        return ALL_CATEGORIES in self.categories or bool(self.categories & set(categories))


@dataclass(frozen=True)
class BrandRules:
    required_phrases: tuple[str, ...] = ()
    banned_words: tuple[str, ...] = ()


@dataclass
class CreativeRules:
    text_fields: tuple[str, ...]
    claim_patterns: tuple[ClaimPattern, ...]
    prohibited: tuple[ProhibitedList, ...]
    substantiation_kinds: frozenset[str]
    unsubstantiated_severity: str
    substantiated_severity: str
    brands: dict[str, BrandRules]
    diff_link: str
    diff_default: str
    weights: dict[str, int]
    band_medium: int
    band_high: int
    max_score: int


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise PolicyError(f"creative rules: {msg}")


def _strings(value: Any, what: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    ok = isinstance(value, list) and all(isinstance(v, str) and v for v in value)
    _require(ok and (allow_empty or value), f"{what} must be a {'list' if allow_empty else 'non-empty list'} of non-empty strings")
    return tuple(value)


def _severity(value: Any, what: str) -> str:
    _require(value in SEVERITIES, f"{what} must be one of {SEVERITIES}")
    return value


def _int(value: Any, what: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0, f"{what} must be a non-negative integer")
    return value


def parse_creative_rules(raw: dict[str, Any]) -> CreativeRules:
    _require(isinstance(raw, dict), "must be a mapping")
    _require(raw.get("version", 1) == 1, "unsupported version")
    unknown = set(raw) - {"version", "text_fields", "claim_patterns", "prohibited", "substantiation", "brands",
                          "diff", "scoring"}
    _require(not unknown, f"unknown keys {sorted(unknown)}")
    text_fields = _strings(raw.get("text_fields", ["message", "headline", "description"]), "text_fields")
    _require(set(text_fields) <= set(VIEW_FIELDS), f"text_fields must be among {VIEW_FIELDS}")

    patterns, seen = [], set()
    for p in raw.get("claim_patterns") or []:
        _require(isinstance(p, dict) and set(p) == {"id", "kind", "pattern"}, "each claim pattern needs exactly id, kind, pattern")
        _require(p["id"] not in seen, f"duplicate claim pattern id {p['id']}")
        _require(p["kind"] in CLAIM_KINDS, f"claim pattern {p['id']}: kind must be one of {CLAIM_KINDS}")
        try:
            regex = re.compile(_fold(p["pattern"], False))
        except re.error as exc:
            raise PolicyError(f"creative rules: claim pattern {p['id']}: invalid regex: {exc}") from exc
        seen.add(p["id"])
        patterns.append(ClaimPattern(p["id"], p["kind"], regex))

    prohibited = []
    for entry in raw.get("prohibited") or []:
        _require(isinstance(entry, dict) and isinstance(entry.get("id"), str), "each prohibited entry needs an id")
        extra = set(entry) - {"id", "categories", "terms", "severity", "case_sensitive"}
        _require(not extra, f"prohibited {entry['id']}: unknown keys {sorted(extra)}")
        prohibited.append(ProhibitedList(
            entry["id"], frozenset(_strings(entry.get("categories", [ALL_CATEGORIES]), f"prohibited {entry['id']}.categories")),
            _strings(entry.get("terms"), f"prohibited {entry['id']}.terms"),
            _severity(entry.get("severity", "critical"), f"prohibited {entry['id']}.severity"),
            bool(entry.get("case_sensitive", False))))
    ids = [p.id for p in prohibited]
    _require(len(ids) == len(set(ids)), "duplicate prohibited ids")

    sub = raw.get("substantiation") or {}
    _require(isinstance(sub, dict) and not set(sub) - {"claim_kinds", "severity", "substantiated_severity"},
             "substantiation has unknown keys")
    kinds = _strings(sub.get("claim_kinds", []), "substantiation.claim_kinds", allow_empty=True)
    _require(set(kinds) <= set(CLAIM_KINDS), f"substantiation.claim_kinds must be among {CLAIM_KINDS}")

    brands = {}
    for name, spec in (raw.get("brands") or {}).items():
        _require(isinstance(spec, dict) and not set(spec) - {"required_phrases", "banned_words"},
                 f"brand {name}: only required_phrases and banned_words are allowed")
        brands[str(name)] = BrandRules(
            _strings(spec.get("required_phrases", []), f"brand {name}.required_phrases", allow_empty=True),
            _strings(spec.get("banned_words", []), f"brand {name}.banned_words", allow_empty=True))

    diff = raw.get("diff") or {}
    _require(isinstance(diff, dict) and not set(diff) - {"link", "default"}, "diff allows only link and default")
    scoring = raw.get("scoring") or {}
    _require(isinstance(scoring, dict) and not set(scoring) - {"weights", "bands", "max"}, "scoring has unknown keys")
    # Defaults apply only when a section is absent; a declared section must be complete.
    weights = scoring.get("weights") or {"critical": 50, "major": 20, "minor": 5, "info": 0}
    _require(isinstance(weights, dict), "scoring.weights must be a mapping")
    _require(set(weights) == set(SEVERITIES), f"scoring.weights must cover exactly {SEVERITIES}")
    weights = {k: _int(v, f"scoring.weights.{k}") for k, v in weights.items()}
    bands = scoring.get("bands") or {"medium": 25, "high": 50}
    _require(isinstance(bands, dict), "scoring.bands must be a mapping")
    _require(set(bands) == {"medium", "high"}, "scoring.bands must be {medium, high}")
    medium, high = _int(bands["medium"], "scoring.bands.medium"), _int(bands["high"], "scoring.bands.high")
    _require(0 < medium <= high, "scoring.bands must satisfy 0 < medium <= high")
    max_score = _int(scoring.get("max", 100), "scoring.max")
    _require(max_score >= high, "scoring.max must be at least the high threshold")

    return CreativeRules(
        text_fields=text_fields, claim_patterns=tuple(patterns), prohibited=tuple(prohibited),
        substantiation_kinds=frozenset(kinds),
        unsubstantiated_severity=_severity(sub.get("severity", "major"), "substantiation.severity"),
        substantiated_severity=_severity(sub.get("substantiated_severity", "info"), "substantiation.substantiated_severity"),
        brands=brands, diff_link=_severity(diff.get("link", "critical"), "diff.link"),
        diff_default=_severity(diff.get("default", "minor"), "diff.default"),
        weights=weights, band_medium=medium, band_high=high, max_score=max_score)


def load_creative_rules(path: str | Path) -> CreativeRules:
    with open(path, encoding="utf-8") as fh:
        return parse_creative_rules(yaml.safe_load(fh) or {})


def prohibited_from_policy(policy: "Policy") -> tuple[ProhibitedList, ...]:
    """Reuse Phase 1 prohibited_terms rules as category-independent prohibited lists."""
    return tuple(ProhibitedList(f"policy:{r.id}", frozenset({ALL_CATEGORIES}), r.terms, "critical", r.case_sensitive,
                                source="policy")
                 for r in policy.rules if isinstance(r, ProhibitedTermsRule))


# --------------------------------------------------------------------------- creative view / context


def _image_hashes(entity: dict[str, Any]) -> Any:
    spec = entity.get("object_story_spec") or {}
    hashes = [h for h in ((spec.get("link_data") or {}).get("image_hash"), (spec.get("video_data") or {}).get("image_hash")) if h]
    hashes += [i.get("hash") for i in (entity.get("asset_feed_spec") or {}).get("images", []) if i.get("hash")]
    return hashes[0] if len(hashes) == 1 else (hashes or None)


def creative_view(entity: dict[str, Any]) -> dict[str, Any]:
    """The fields Creative Check compares, from a Phase 1 normalized creative entity."""
    resolved = entity.get("resolved") or {}
    view = {k: resolved.get(k) for k in VIEW_FIELDS if k != "image_hash"}
    view["image_hash"] = _image_hashes(entity)
    return {k: v for k, v in view.items() if v is not None}


def view_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    return creative_view(normalize_entity("creative", raw))


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def fold(text: str) -> str:
    """NFKC + casefold (as Phase 1 prohibited_terms), with runs of whitespace collapsed."""
    return re.sub(r"\s+", " ", _fold(text, False)).strip()


def _key(text: str) -> str:
    return re.sub(r"\s+", "", fold(text))


@dataclass
class Context:
    categories: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"categories": list(self.categories), "brands": list(self.brands), "sources": self.sources}


def load_attributes(path: str | Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    """Category / brand attributes by entity: {"creative"|"adset"|"campaign": {id: {"category", "brand"}}}."""
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _require(isinstance(raw, dict), "attributes file must be a mapping")
    out = {}
    for level in ("creative", "adset", "campaign"):
        entries = raw.get(level) or {}
        _require(isinstance(entries, dict), f"attributes.{level} must be a mapping")
        out[level] = {str(k): dict(v) for k, v in entries.items()}
    return out


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return (value,) if isinstance(value, str) else tuple(str(v) for v in value)


def resolve_context(creative_id: str, snapshot: Snapshot | None, attributes: dict[str, Any] | None,
                    original: dict[str, Any] | None = None, *, category: str | None = None,
                    brand: str | None = None) -> Context:
    """Explicit value > creative attribute > attributes of the ad sets / campaigns whose ads use it
    > what was recorded with the original. Several values at one level are all applied."""
    attributes = attributes or {}
    parents: list[tuple[str, str]] = []
    if snapshot is not None:
        for ad in snapshot.entities.get("ad", {}).values():
            if (ad.get("creative") or {}).get("id") == creative_id:
                parents += [("adset", str(ad.get("adset_id"))), ("campaign", str(ad.get("campaign_id")))]
    ctx = Context()
    for attr, explicit in (("category", category), ("brand", brand)):
        levels = [("explicit", [explicit] if explicit else [])]
        levels.append(("creative", list(_as_tuple((attributes.get("creative", {}).get(creative_id) or {}).get(attr)))))
        for level in ("adset", "campaign"):
            vals = []
            for kind, pid in parents:
                if kind == level:
                    vals += _as_tuple((attributes.get(level, {}).get(pid) or {}).get(attr))
            levels.append((level, vals))
        levels.append(("original", list(_as_tuple((original or {}).get(attr)))))
        for source, vals in levels:
            if vals:
                values = tuple(sorted(set(vals)))
                if attr == "category":
                    ctx.categories = values
                else:
                    ctx.brands = values
                ctx.sources[attr] = source
                break
        else:
            ctx.sources[attr] = "unset"
    return ctx


# --------------------------------------------------------------------------- registry / matching


def registered_by(name: str, source: str = "cli --by") -> dict[str, Any]:
    return {"name": name, "source": source, "verified": False}


def register_original(ledger: "Ledger", snapshot: Snapshot, *, creative_id: str | None = None, ad_id: str | None = None,
                      by: str, by_source: str = "cli --by", substantiation: list[dict[str, str]] | None = None,
                      category: str | None = None, brand: str | None = None, note: str | None = None) -> int:
    """Register the creative as it appears in `snapshot` as the advertiser-approved original."""
    if creative_id is None:
        if ad_id is None:
            raise KeyError("register-original needs a creative id or an ad id")
        ad = snapshot.get("ad", ad_id)
        if ad is None or not (ad.get("creative") or {}).get("id"):
            raise KeyError(f"ad {ad_id} with a creative not found in snapshot #{snapshot.id}")
        creative_id = ad["creative"]["id"]
    entity = snapshot.get("creative", creative_id)
    if entity is None:
        raise KeyError(f"creative {creative_id} not found in snapshot #{snapshot.id}")
    ad_ids = sorted(a["id"] for a in snapshot.entities.get("ad", {}).values()
                    if (a.get("creative") or {}).get("id") == creative_id)
    if ad_id and ad_id not in ad_ids:
        ad_ids.append(ad_id)
    for i, memo in enumerate(substantiation or []):
        if not (isinstance(memo, dict) and isinstance(memo.get("claim"), str) and memo["claim"].strip()
                and isinstance(memo.get("source"), str) and memo["source"].strip()):
            raise KeyError(f"substantiation[{i}] needs non-empty 'claim' and 'source'")
    return ledger.insert_original(
        registered_at=iso(ledger.clock()), registered_by=registered_by(by, by_source), account_id=snapshot.account_id,
        creative_id=creative_id, ad_ids=ad_ids, snapshot_id=snapshot.id, fields=creative_view(entity),
        substantiation=list(substantiation or []), category=category, brand=brand, note=note)


def place_on_ad(snapshot: Snapshot, ad_id: str, creative_id: str, entity: dict[str, Any]) -> Snapshot:
    """A copy of `snapshot` with `entity` added and `ad_id` pointing at it — how a candidate that is not
    in any snapshot yet (a file, a fixture) is checked as if it had been swapped onto that ad."""
    if snapshot.get("ad", ad_id) is None:
        raise KeyError(f"ad {ad_id} not found in snapshot #{snapshot.id}")
    entities = copy.deepcopy(snapshot.entities)
    entities.setdefault("creative", {})[creative_id] = entity
    entities["ad"][ad_id]["creative"] = {"id": creative_id}
    return Snapshot(snapshot.account_id, snapshot.taken_at, snapshot.api_version, snapshot.source, entities,
                    snapshot.activities, id=snapshot.id)


def resolve_original(ledger: "Ledger", account_id: str, creative_id: str, snapshot: Snapshot | None, *,
                     explicit_original_id: int | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Find the original for a candidate: explicit id, the same creative id, or the lineage of the ads
    that now use the candidate (originals registered for those ads, or creatives they pointed at before)."""
    if explicit_original_id is not None:
        original = ledger.get_original(explicit_original_id)
        if original is None:
            raise KeyError(f"original {explicit_original_id} not found")
        return original, {"method": "explicit", "original_id": original["id"]}
    same = ledger.latest_original_for(account_id, creative_id)
    if same is not None:
        return same, {"method": "same_creative", "original_id": same["id"]}
    ads = sorted(a["id"] for a in (snapshot.entities.get("ad", {}).values() if snapshot else [])
                 if (a.get("creative") or {}).get("id") == creative_id)
    found: dict[str, dict[str, Any]] = {}
    via: dict[str, list[str]] = {}
    for original in ledger.list_originals(account_id):
        for ad_id in set(original["ad_ids"]) & set(ads):
            found[original["creative_id"]] = ledger.latest_original_for(account_id, original["creative_id"])
            via.setdefault(original["creative_id"], []).append(f"ad {ad_id}（原本登録時にこの原本を使用）")
    for ad_id in ads:
        for previous in ledger.ad_creative_history(account_id, ad_id):
            original = ledger.latest_original_for(account_id, previous)
            if original is not None and previous != creative_id:
                found[previous] = original
                via.setdefault(previous, []).append(f"ad {ad_id}（Watch の creative.id 変更履歴）")
    if len(found) == 1:
        (ocid, original), = found.items()
        return original, {"method": "ad_lineage", "original_id": original["id"], "via": sorted(set(via[ocid]))}
    if len(found) > 1:
        return None, {"method": None, "reason": "複数の原本候補があり 1 つに特定できない",
                      "candidates": sorted(o["id"] for o in found.values()), "ads": ads}
    return None, {"method": None, "reason": "対応する承認済み原本が登録されていない", "ads": ads}


# --------------------------------------------------------------------------- check


def extract_claims(view: dict[str, Any], rules: CreativeRules) -> list[dict[str, Any]]:
    claims, seen = [], set()
    for fname in rules.text_fields:
        for text in _texts(view.get(fname)):
            folded = fold(text)
            for pattern in rules.claim_patterns:
                for m in pattern.regex.finditer(folded):
                    span = m.group(0).strip()
                    key = (pattern.id, fname, _key(span))
                    if not span or key in seen:
                        continue
                    seen.add(key)
                    claims.append({"pattern_id": pattern.id, "kind": pattern.kind, "field": fname, "text": span,
                                   "start": m.start(), "end": m.end()})
    return claims


def diff_views(original: dict[str, Any], candidate: dict[str, Any], rules: CreativeRules) -> list[dict[str, Any]]:
    out = []
    for fname in VIEW_FIELDS:
        a, b = original.get(fname), candidate.get(fname)
        if a == b:
            continue
        notation_only = bool(_texts(a)) and [fold(t) for t in _texts(a)] == [fold(t) for t in _texts(b)]
        out.append({"field": fname, "original": a, "candidate": b,
                    "kind": "notation_only" if notation_only else "content",
                    "severity": rules.diff_link if fname == "link" else rules.diff_default})
    return out


def _memo_for(claim: dict[str, Any], memos: list[dict[str, str]]) -> dict[str, str] | None:
    span = _key(claim["text"])
    return next((m for m in memos if span and span in _key(m["claim"])), None)


def score(detections: list[dict[str, Any]], rules: CreativeRules) -> tuple[int, str, dict[str, Any]]:
    by_severity = {s: {"count": 0, "points": 0} for s in SEVERITIES}
    for d in detections:
        by_severity[d["severity"]]["count"] += 1
        by_severity[d["severity"]]["points"] += d["points"]
    raw = sum(d["points"] for d in detections)
    total = min(raw, rules.max_score)
    return total, band_for(total, rules), {
        "weights": dict(rules.weights), "bands": {"medium": rules.band_medium, "high": rules.band_high},
        "by_severity": by_severity, "raw_total": raw, "max": rules.max_score, "capped": raw > rules.max_score,
        "items": [{"detection": d["id"], "rule_id": d["rule_id"], "severity": d["severity"], "points": d["points"]}
                  for d in detections],
    }


def band_for(value: int, rules: CreativeRules) -> str:
    if value >= rules.band_high:
        return "high"
    return "medium" if value >= rules.band_medium else "low"


def check_creative(candidate: dict[str, Any], *, creative_id: str, account_id: str | None, rules: CreativeRules,
                   original: dict[str, Any] | None, resolution: dict[str, Any], context: Context,
                   extra_prohibited: tuple[ProhibitedList, ...] = ()) -> dict[str, Any]:
    """`candidate` is a creative view (see creative_view). Returns a JSON-ready result."""
    detections: list[dict[str, Any]] = []

    def detect(type_: str, rule_id: str, severity: str, message: str, **details: Any) -> None:
        detections.append({"id": f"d{len(detections) + 1}", "type": type_, "rule_id": rule_id, "severity": severity,
                           "points": rules.weights[severity], "message": message, **details})

    diff = diff_views(original["fields"], candidate, rules) if original else []
    link_change = None
    for d in diff:
        note = "（表記のみの差分）" if d["kind"] == "notation_only" else ""
        if d["field"] == "link":
            link_change = {"original": d["original"], "candidate": d["candidate"], "kind": d["kind"]}
            detect("link_changed", "diff:link", d["severity"],
                   f"リンク先 URL が原本と異なる（最重要の差分）{note}: {d['original']} → {d['candidate']}", field="link")
        else:
            detect("field_changed", f"diff:{d['field']}", d["severity"], f"{d['field']} が原本から変わった{note}",
                   field=d["field"])

    texts = {f: _texts(candidate.get(f)) for f in rules.text_fields}
    # One term in one field is one detection, however many lists contain it; the others are noted.
    seen_terms: dict[tuple[str, str], dict[str, Any]] = {}
    for plist in (*rules.prohibited, *extra_prohibited):
        if not plist.applies(context.categories):
            continue
        scope = "全カテゴリ" if ALL_CATEGORIES in plist.categories else "カテゴリ " + ",".join(sorted(plist.categories))
        for fname, values in texts.items():
            for term in plist.terms:
                if not any(_fold(term, plist.case_sensitive) in _fold(t, plist.case_sensitive) for t in values):
                    continue
                key = (fname, _fold(term, False))
                if key in seen_terms:
                    if plist.id not in seen_terms[key]["also_in"] and plist.id != seen_terms[key]["rule_id"]:
                        seen_terms[key]["also_in"].append(plist.id)
                    continue
                detect("prohibited_term", plist.id, plist.severity, f"{fname} に禁止表現「{term}」（{scope}）",
                       field=fname, term=term, source=plist.source, also_in=[])
                seen_terms[key] = detections[-1]

    claims = extract_claims(candidate, rules)
    original_keys = {(c["kind"], _key(c["text"])) for c in extract_claims(original["fields"], rules)} if original else set()
    memos = list((original or {}).get("substantiation") or [])
    substantiated_spans = set()
    for c in claims:
        c["new"] = (c["kind"], _key(c["text"])) not in original_keys if original else None
        if c["kind"] not in rules.substantiation_kinds or (c["field"], _key(c["text"])) in substantiated_spans:
            continue
        substantiated_spans.add((c["field"], _key(c["text"])))
        memo = _memo_for(c, memos)
        c["substantiation"] = memo
        if memo:
            detect("substantiated", f"substantiation:{c['kind']}", rules.substantiated_severity,
                   f"根拠記録あり: 「{c['text']}」（{c['kind']}）→ {memo['source']}", field=c["field"], claim=c["text"])
        else:
            why = "根拠未記録" if original else "原本なしのため根拠メモを参照できない"
            detect("needs_substantiation", f"substantiation:{c['kind']}", rules.unsubstantiated_severity,
                   f"要根拠（{why}）: 「{c['text']}」（{c['kind']}）", field=c["field"], claim=c["text"])

    joined = fold(" ".join(t for values in texts.values() for t in values))
    for brand in context.brands:
        brules = rules.brands.get(brand)
        if brules is None:
            continue
        for phrase in brules.required_phrases:
            if fold(phrase) not in joined:
                detect("brand_required_missing", f"brand:{brand}:required", BRAND_SEVERITY,
                       f"ブランド {brand} の必須文言「{phrase}」が無い", phrase=phrase)
        for word in brules.banned_words:
            hit = next((f for f, values in texts.items() if any(fold(word) in fold(t) for t in values)), None)
            if hit:
                detect("brand_banned_word", f"brand:{brand}:banned", BRAND_SEVERITY,
                       f"ブランド {brand} の使用禁止語「{word}」が {hit} に含まれる", field=hit, word=word)

    notes = [NOT_LEGAL, EXTRACTION_LIMIT]
    if not context.categories:
        notes.append("カテゴリ未設定のため、業種別の禁止表現は全カテゴリ共通のものだけを適用した。")
    unknown_brands = [b for b in context.brands if b not in rules.brands]
    if unknown_brands:
        notes.append(f"ブランド規則が宣言されていないブランド: {', '.join(unknown_brands)}")

    result: dict[str, Any] = {
        "kind": "rein.creative_check", "version": 1, "creative_id": creative_id, "account_id": account_id,
        "status": "checked" if original else "no_original",
        "original": None if original is None else {
            "id": original["id"], "creative_id": original["creative_id"], "registered_at": original["registered_at"],
            "registered_by": original["registered_by"]},
        "original_resolution": resolution, "context": context.to_dict(), "candidate": candidate,
        "diff": diff, "link_change": link_change, "claims": claims, "detections": detections, "notes": notes,
    }
    if original is None:
        result.update(score=None, band=None, score_breakdown=None)
        notes.insert(0, f"原本なし: {resolution.get('reason')}。スコアは付けていない。")
    else:
        result["score"], result["band"], result["score_breakdown"] = score(detections, rules)
    return result


def render(result: dict[str, Any]) -> str:
    lines = [f"creative {result['creative_id']}（{result['account_id']}）"]
    if result["status"] == "no_original":
        lines.append(f"  判定: 原本なし — {result['original_resolution'].get('reason')}（スコアなし）")
    else:
        o, res = result["original"], result["original_resolution"]
        by = o["registered_by"]
        lines.append(f"  原本: #{o['id']} creative {o['creative_id']}（{res['method']}、登録 {o['registered_at']} "
                     f"by {by['name']}・自己申告）")
        lines.append(f"  スコア: {result['score']} / {result['score_breakdown']['max']}  帯: {result['band']}")
    ctx = result["context"]
    lines.append(f"  カテゴリ: {','.join(ctx['categories']) or '未設定'}  ブランド: {','.join(ctx['brands']) or '未設定'}")
    if result["link_change"]:
        lc = result["link_change"]
        lines.append(f"  !! リンク先が原本と異なる: {lc['original']} → {lc['candidate']}")
    lines.append("  差分:" + ("" if result["diff"] else " なし"))
    for d in result["diff"]:
        lines.append(f"    - {d['field']} [{d['kind']}]: {json.dumps(d['original'], ensure_ascii=False)} → "
                     f"{json.dumps(d['candidate'], ensure_ascii=False)}")
    lines.append("  抽出された訴求:" + ("" if result["claims"] else " なし"))
    for c in result["claims"]:
        new = "" if c["new"] is None else ("（新規）" if c["new"] else "（原本にもある）")
        lines.append(f"    - {c['kind']} {c['field']}: 「{c['text']}」{new}")
    lines.append("  検出と点数:" + ("" if result["detections"] else " なし"))
    for d in result["detections"]:
        lines.append(f"    - [{d['severity']} +{d['points']}] {d['rule_id']}: {d['message']}")
    for note in result["notes"]:
        lines.append(f"  ※ {note}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- runs (CLI + Watch)


def run_check(ledger: "Ledger", rules: CreativeRules, *, account_id: str, creative_id: str,
              candidate: dict[str, Any], snapshot: Snapshot | None, attributes: dict[str, Any] | None = None,
              explicit_original_id: int | None = None, category: str | None = None, brand: str | None = None,
              extra_prohibited: tuple[ProhibitedList, ...] = (), trigger: str = "cli",
              change_ids: list[int] | None = None, record: bool = True) -> dict[str, Any]:
    original, resolution = resolve_original(ledger, account_id, creative_id, snapshot,
                                            explicit_original_id=explicit_original_id)
    context = resolve_context(creative_id, snapshot, attributes, original, category=category, brand=brand)
    result = check_creative(candidate, creative_id=creative_id, account_id=account_id, rules=rules, original=original,
                            resolution=resolution, context=context, extra_prohibited=extra_prohibited)
    result["trigger"] = trigger
    result["change_ids"] = list(change_ids or [])
    if record:
        result["check_id"] = ledger.record_creative_check(
            trigger=trigger, account_id=account_id, creative_id=creative_id,
            original_id=original["id"] if original else None, status=result["status"], score=result["score"],
            band=result["band"], change_ids=result["change_ids"], result=result)
    return result


@dataclass
class CreativeWatch:
    """Creative Check settings for Watch: new or changed creatives found by the diff are checked."""

    rules: CreativeRules
    attributes: dict[str, Any] | None = None
    extra_prohibited: tuple[ProhibitedList, ...] = ()


def candidates_from_changes(changes: list["ChangeRecord"]) -> dict[tuple[str, str], list["ChangeRecord"]]:
    """(account, creative id) -> the changes that put it in front of us: created/modified creatives and
    ads whose creative changed or that were created with one."""
    out: dict[tuple[str, str], list["ChangeRecord"]] = {}
    for c in changes:
        cid = None
        if c.entity_type == "creative" and c.change_type != "entity_deleted":
            cid = c.entity_id
        elif c.entity_type == "ad" and c.field == "creative.id" and c.change_type in ("modified", "added"):
            cid = str(c.after)
        elif c.entity_type == "ad" and c.change_type == "entity_created":
            cid = ((c.after or {}).get("creative") or {}).get("id")
        if cid:
            out.setdefault((c.account_id, str(cid)), []).append(c)
    return out


def build_creative_alert(result: dict[str, Any], change: "ChangeRecord") -> dict[str, Any]:
    original = result["original"] or {}
    reasons = [d["message"] for d in result["detections"] if d["severity"] in ("critical", "major")]
    return {
        "kind": "rein.creative_alert", "version": 1, "severity": result["band"], "band": result["band"],
        "score": result["score"], "check_id": result.get("check_id"), "change_id": change.id,
        "account_id": change.account_id, "detected_at": iso(change.detected_at), "creative_id": result["creative_id"],
        "original_id": original.get("id"),
        "summary": (f"creative {result['creative_id']} のリスクスコア {result['score']}（{result['band']}）"
                    f" — 原本 creative {original.get('creative_id')} との比較"),
        "actor_kind": change.actor_kind, "attribution_basis": (change.attribution or {}).get("basis"),
        "rules": sorted({d["rule_id"] for d in result["detections"] if d["points"]}), "reasons": reasons,
        "note": NOT_LEGAL,
    }


def watch_changes(ledger: "Ledger", changes: list["ChangeRecord"], watch: CreativeWatch,
                  sinks: list[AlertSink]) -> list[dict[str, Any]]:
    """Check every creative the changes brought in; send high-band results down the Phase 1 alert path."""
    results = []
    snapshots: dict[int, Snapshot] = {}
    for (account_id, creative_id), triggers in candidates_from_changes(changes).items():
        sid = max(c.to_snapshot_id for c in triggers)
        snap = snapshots.get(sid) or snapshots.setdefault(sid, ledger.load_snapshot(sid))
        entity = snap.get("creative", creative_id)
        if entity is None:
            continue  # the ad points at a creative this snapshot cannot see; nothing to compare
        result = run_check(ledger, watch.rules, account_id=account_id, creative_id=creative_id,
                           candidate=creative_view(entity), snapshot=snap, attributes=watch.attributes,
                           extra_prohibited=watch.extra_prohibited, trigger="watch",
                           change_ids=[c.id for c in triggers])
        if result["band"] == "high":
            anchor = next((c for c in triggers if c.entity_type == "ad"), triggers[0])
            alert = build_creative_alert(result, anchor)
            result["alert"] = alert
            result["deliveries"] = dispatch(alert, sinks)
            ledger.record_alert(anchor.id, alert, result["deliveries"])
        results.append(result)
    return results
