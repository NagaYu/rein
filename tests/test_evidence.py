"""Phase 3 evidence pack: scenarios 10-12 plus ordering, time format, determinism and the CLI."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path

import pytest

from rein.cli import main
from rein.demo_records import PERIOD, build_demo_ledger
from rein.evidence import (APPROVAL_STATUSES, PROXY_DECISIONS, EvidenceError, build_timeline, export_pack, verify_pack)
from rein.ledger import Ledger
from rein.timeutil import parse_time

ROOT = Path(__file__).resolve().parent.parent
BUILD = {"version": "test", "commit": "test", "dirty": False}
FILES = {"manifest.json", "timeline.json", "timeline.csv", "creative_checks.json", "summary.md",
         "policy/policy.example.yaml", "policy/policy.proxy.example.yaml", "policy/creative_rules.example.yaml"}
AS1, AS2 = "120000000000000201", "120000000000000202"


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    work = tmp_path_factory.mktemp("demo")
    info = build_demo_ledger(work / "demo.db", ROOT, work)
    yield info
    info["ledger"].close()


@pytest.fixture()
def pack(demo, tmp_path):
    out = tmp_path / "pack"
    manifest = export_pack(demo["ledger"], out, *PERIOD, policy_files=demo["policy_files"], clock=demo["clock"],
                           build_info=BUILD)
    return out, manifest


def load(out: Path, name: str):
    return json.loads((out / name).read_text(encoding="utf-8"))


def tables(md: str) -> dict[str, dict[str, int]]:
    """Section heading -> {row key: number} for the two-column tables in summary.md."""
    out, heading = {}, None
    for line in md.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
        m = re.match(r"^\| ([^|]+?) \| (\d+) \|$", line)
        if heading and m:
            out.setdefault(heading, {})[m.group(1)] = int(m.group(2))
    return out


# --------------------------------------------------------------------------- scenario 10


def test_10_manifest_hashes_match_files_and_verify_passes(pack):
    out, manifest = pack
    on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert on_disk == FILES
    assert {f["path"] for f in manifest["files"]} == FILES - {"manifest.json"}
    for f in manifest["files"]:
        data = (out / f["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == f["sha256"] and len(data) == f["bytes"]
    result = verify_pack(out)
    assert result.ok and result.checked == len(FILES) - 1 and result.problems == []


def test_10_manifest_contents(pack, demo):
    out, manifest = pack
    on_disk = load(out, "manifest.json")
    assert on_disk == manifest
    assert manifest["accounts"] == ["act_100000000000001", "act_100000000000002"]
    assert manifest["period"] == {"from": PERIOD[0], "to": PERIOD[1], "inclusive": True}
    assert manifest["generated_at"] == "2026-10-29T18:00:00Z"
    assert manifest["rein"] == BUILD and manifest["hash_algorithm"] == "sha256"
    assert "エクスポート前の台帳" in manifest["integrity_note"]
    # policy/ holds byte-for-byte copies of the Phase 1 and Phase 2 policies (and the creative rules).
    for src in demo["policy_files"]:
        assert (out / "policy" / Path(src).name).read_bytes() == Path(src).read_bytes()


def test_10_default_build_info_has_version_and_commit(demo, tmp_path):
    manifest = export_pack(demo["ledger"], tmp_path / "p", *PERIOD, policy_files=demo["policy_files"])
    from rein import __version__
    assert manifest["rein"]["version"] == __version__ and manifest["rein"]["commit"]


# --------------------------------------------------------------------------- scenario 11


def test_11_timeline_merges_both_ledgers_in_time_order(pack, demo):
    out, _ = pack
    entries = load(out, "timeline.json")["entries"]
    sources = {e["source"] for e in entries}
    assert sources == {"watch.change", "watch.alert", "watch.revert_run", "proxy.audit", "proxy.approval"}
    times = [parse_time(e["time"]) for e in entries]
    assert times == sorted(times)
    ledger = demo["ledger"]
    assert sum(e["source"] == "watch.change" for e in entries) == len(ledger.rows("changes"))
    assert sum(e["source"] == "proxy.audit" for e in entries) == len(ledger.rows("proxy_audit"))
    assert sum(e["source"] == "proxy.approval" for e in entries) == len(ledger.rows("approvals"))
    assert sum(e["source"] == "watch.alert" for e in entries) == len(ledger.rows("alerts"))


def test_11_entity_and_time_join(pack):
    out, _ = pack
    entries = {e["ref"]: e for e in load(out, "timeline.json")["entries"]}
    budget = {e["entity_id"]: e for e in entries.values()
              if e["source"] == "watch.change" and e["field"] == "daily_budget" and e["entity_id"] in (AS1, AS2)}
    assert budget[AS1]["actor_kind"] == "unknown"  # Watch's attribution is not rewritten by the join
    (a1,) = budget[AS1]["related"]
    (a2,) = budget[AS2]["related"]
    assert entries[a1]["event"] == "approval_executed" and entries[a1]["entity_id"] == AS1
    assert entries[a2]["event"] == "call" and entries[a2]["decision"] == "allow"
    assert budget[AS1]["ref"] in entries[a1]["related"]
    # Denied / pending calls on the same entity were never forwarded, so they are not joined.
    for e in entries.values():
        if e["source"] == "proxy.audit" and e["related"]:
            assert e["forwarded"] and e["downstream_ok"]
    approval = next(e for e in entries.values() if e["source"] == "proxy.approval" and e["status"] == "executed")
    assert a1 in approval["related"]


def test_11_summary_numbers_match_timeline_and_creative_checks(pack):
    out, _ = pack
    entries = load(out, "timeline.json")["entries"]
    checks = load(out, "creative_checks.json")["checks"]
    t = tables((out / "summary.md").read_text(encoding="utf-8"))
    changes = [e for e in entries if e["source"] == "watch.change"]
    for kind in ("human", "external_agent", "meta_ai", "unknown"):
        assert t["主体別の変更件数"][kind] == sum(1 for c in changes if c["actor_kind"] == kind)
    assert t["主体別の変更件数"]["合計"] == len(changes)
    assert t["主体別の変更件数"]["unknown"] > 0  # unknown is reported, not dropped
    v = t["違反と巻き戻し"]
    assert v["violation"] == sum(1 for c in changes if c["severity"] == "violation")
    assert v["warning"] == sum(1 for c in changes if c["severity"] == "warning")
    assert v["alert"] == sum(1 for e in entries if e["source"] == "watch.alert")
    assert v["revert_run"] == sum(1 for e in entries if e["source"] == "watch.revert_run")
    calls = [e for e in entries if e["source"] == "proxy.audit" and e["event"] == "call"]
    proxy = t["プロキシ判定の内訳（受け付けたコール）"]
    for d in PROXY_DECISIONS:
        assert proxy[d] == sum(1 for c in calls if c["decision"] == d)
    assert proxy["合計"] == len(calls)
    approvals = [e for e in entries if e["source"] == "proxy.approval"]
    ap = t["承認の内訳（期間内に受け付けた承認待ち）"]
    for s in APPROVAL_STATUSES:
        assert ap[s] == sum(1 for a in approvals if a["status"] == s)
    cc = t["Creative Check"]
    for band in ("high", "medium", "low"):
        assert cc[band] == sum(1 for c in checks if c["band"] == band)
    assert cc["no_original"] == sum(1 for c in checks if c["status"] == "no_original")
    assert cc["合計"] == len(checks)
    md = (out / "summary.md").read_text(encoding="utf-8")
    high_rows = re.findall(r"^\| (\d+) \| (\d+) \| (act_\d+) \| (\d+) \|", md, re.M)
    assert sorted(int(r[0]) for r in high_rows) == sorted(c["check_id"] for c in checks if c["band"] == "high")
    joined = sum(1 for c in changes if c["related"])
    assert f"突き合わせられた変更: {joined} 件" in md


def test_11_demo_numbers_are_what_the_story_says(pack):
    t = tables((pack[0] / "summary.md").read_text(encoding="utf-8"))
    assert t["プロキシ判定の内訳（受け付けたコール）"] == {"allow": 1, "passthrough": 1, "deny": 5, "require_approval": 3,
                                                     "合計": 10}
    ap = t["承認の内訳（期間内に受け付けた承認待ち）"]
    assert (ap["executed"], ap["rejected"], ap["expired"], ap["合計"]) == (1, 1, 1, 3)
    assert t["Creative Check"]["high"] >= 2 and t["Creative Check"]["no_original"] == 1


def test_11_summary_states_its_limits(pack):
    md = (pack[0] / "summary.md").read_text(encoding="utf-8")
    for phrase in ("法的なコンプライアンス判定ではありません", "エクスポート前の台帳（SQLite）自体が改変されていないことは保証しません",
                   "プロキシを経由したエージェントのコールだけ", "自己申告", "不明（unknown）", "言い換えによる回避は検出できません"):
        assert phrase in md


def test_11_csv_mirrors_json(pack):
    out, _ = pack
    entries = load(out, "timeline.json")["entries"]
    rows = list(csv.DictReader(io.StringIO((out / "timeline.csv").read_text(encoding="utf-8"))))
    assert [r["ref"] for r in rows] == [e["ref"] for e in entries]
    assert [r["time"] for r in rows] == [e["time"] for e in entries]


def test_11_times_are_written_as_stored(demo, tmp_path):
    ledger = Ledger(":memory:")
    ledger.record_audit(occurred_at="2026-10-27T10:00:00+09:00", event="call", tool="t", arguments_summary="{}",
                        arguments_sha256="x", decision="deny")
    ledger.record_audit(occurred_at="2026-10-27T00:30:00Z", event="call", tool="t", arguments_summary="{}",
                        arguments_sha256="y", decision="deny")
    entries = build_timeline(ledger, parse_time("2026-10-27T00:00:00Z"), parse_time("2026-10-28T00:00:00Z"))
    # 10:00+09:00 is 01:00Z, after 00:30Z; the original string is kept untouched.
    assert [e["time"] for e in entries] == ["2026-10-27T00:30:00Z", "2026-10-27T10:00:00+09:00"]


def test_11_order_is_deterministic(demo, tmp_path):
    a = export_pack(demo["ledger"], tmp_path / "a", *PERIOD, policy_files=demo["policy_files"], clock=demo["clock"],
                    build_info=BUILD)
    b = export_pack(demo["ledger"], tmp_path / "b", *PERIOD, policy_files=demo["policy_files"], clock=demo["clock"],
                    build_info=BUILD)
    assert a == b
    for f in FILES:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()


def test_11_period_bounds_are_inclusive_and_filter(demo, tmp_path):
    proxy_only = export_pack(demo["ledger"], tmp_path / "p", "2026-10-27T01:00:00Z", "2026-10-27T02:10:00Z",
                             policy_files=demo["policy_files"], clock=demo["clock"], build_info=BUILD)
    entries = load(tmp_path / "p", "timeline.json")["entries"]
    assert {e["source"] for e in entries} == {"proxy.audit", "proxy.approval"}
    assert entries[0]["time"] == "2026-10-27T01:00:00Z" and entries[-1]["time"] == "2026-10-27T02:10:00Z"
    assert proxy_only["accounts"] == []
    assert load(tmp_path / "p", "creative_checks.json")["checks"] == []


def test_export_refuses_bad_input(demo, tmp_path):
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")
    with pytest.raises(EvidenceError, match="空ではありません"):
        export_pack(demo["ledger"], tmp_path / "busy", *PERIOD, policy_files=demo["policy_files"])
    with pytest.raises(EvidenceError, match="--from"):
        export_pack(demo["ledger"], tmp_path / "rev", PERIOD[1], PERIOD[0], policy_files=demo["policy_files"])
    with pytest.raises(EvidenceError, match="重複"):
        export_pack(demo["ledger"], tmp_path / "dup", *PERIOD, policy_files=[demo["policy_files"][0]] * 2)


# --------------------------------------------------------------------------- scenario 12


@pytest.mark.parametrize("name", sorted(FILES - {"manifest.json"}))
def test_12_rewriting_any_file_fails_verify(pack, name):
    out, _ = pack
    path = out / name
    path.write_bytes(path.read_bytes().replace(b"1", b"2", 1) if b"1" in path.read_bytes() else path.read_bytes() + b" ")
    result = verify_pack(out)
    assert not result.ok and any(p.startswith(name) and "sha256" in p for p in result.problems)


def test_12_missing_added_and_manifest_tampering_fail(pack):
    out, _ = pack
    (out / "extra.txt").write_text("added later")
    assert any("manifest に無いファイル" in p for p in verify_pack(out).problems)
    (out / "extra.txt").unlink()
    assert verify_pack(out).ok
    (out / "summary.md").unlink()
    assert any("ファイルが無い" in p for p in verify_pack(out).problems)
    manifest = load(out, "manifest.json")
    manifest["files"].append({"path": "../outside", "sha256": "0", "bytes": 0})
    (out / "manifest.json").write_text(json.dumps(manifest))
    assert any("不正なパス" in p for p in verify_pack(out).problems)
    (out / "manifest.json").write_text("{not json")
    assert not verify_pack(out).ok


def test_12_cli_export_and_verify(demo, tmp_path, capsys):
    db = demo["ledger"].path
    out = tmp_path / "pack"
    policy, proxy_policy, creative_rules = (str(p) for p in demo["policy_files"])
    code = main(["--db", db, "evidence", "export", "--from", PERIOD[0], "--to", PERIOD[1], "--out", str(out),
                 "--policy", policy, "--proxy-policy", proxy_policy, "--creative-rules", creative_rules])
    assert code == 0 and "エクスポート前の台帳" in capsys.readouterr().out
    assert main(["evidence", "verify", str(out)]) == 0
    assert "OK:" in capsys.readouterr().out
    (out / "timeline.csv").write_text("tampered\n", encoding="utf-8")
    assert main(["evidence", "verify", str(out)]) == 2
    assert "timeline.csv: sha256 が一致しない" in capsys.readouterr().out
    assert main(["--db", db, "evidence", "export", "--from", PERIOD[0], "--to", PERIOD[1], "--out", str(out),
                 "--policy", policy, "--proxy-policy", proxy_policy]) == 1
