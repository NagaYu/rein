"""rein snapshot | feed | check | revert | proxy | approvals | audit | creative | evidence | preflight"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .alerts import FileAlertSink, WebhookAlertSink, summarize
from .attribution import AttributionConfig
from .client import ClientError, MetaGraphReadClient, MockAdsClient
from .ledger import ChangeRecord, Ledger
from .pipeline import ingest, watch_check
from .policy import PolicyError, load_policy
from .revert import execute_plan, plan_for_changes
from .timeutil import parse_time

DB_ENV = "REIN_DB"


def _short(value: Any, limit: int = 70) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _attribution_line(change: ChangeRecord) -> str:
    ev = change.attribution or {}
    parts = [f"主体: {change.actor_kind}", f"根拠: {ev.get('basis')}"]
    for a in ev.get("activities", [])[:3]:
        who = a.get("actor_name") or a.get("actor_id") or "?"
        app = a.get("application_name") or a.get("application_id")
        parts.append(f"[{a.get('event_type')} {a.get('event_time')} by {who}" + (f" via {app}" if app else "") + "]")
    return "  ".join(parts)


def format_change(change: ChangeRecord, tz: ZoneInfo) -> str:
    when = change.detected_at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
    head = f"#{change.id:<4} {when}  {summarize(change)}"
    if change.change_type in ("entity_created", "entity_deleted"):
        head += f"  [{change.change_type}]"
    if change.evaluated_at is None:
        verdict = "判定: 未評価（rein check を実行）"
    elif change.severity is None:
        verdict = "判定: OK"
    else:
        verdict = f"判定: {change.severity.upper()}  " + " / ".join(
            f"{f['rule_id']}: {f['reason']}" for f in change.findings)
    return "\n".join([head, f"       {_attribution_line(change)}", f"       {verdict}"])


def _open_ledger(args) -> Ledger:
    return Ledger(args.db)


def cmd_snapshot(args) -> int:
    attribution = load_policy(args.policy).attribution if args.policy else AttributionConfig()
    if args.fixture:
        reader = MockAdsClient(args.fixture)
    else:
        reader = MetaGraphReadClient(args.account)  # read-only; token from REIN_META_ACCESS_TOKEN
    with _open_ledger(args) as ledger:
        result = ingest(ledger, reader, attribution)
        snap = ledger.load_snapshot(result.snapshot_id)
    counts = ", ".join(f"{k}={v}" for k, v in snap.counts().items())
    print(f"snapshot #{result.snapshot_id} {snap.account_id} taken_at={snap.taken_at.isoformat()} "
          f"api={snap.api_version} source={snap.source} ({counts})")
    if result.previous_snapshot_id is None:
        print("  baseline (no previous snapshot to diff against)")
    else:
        print(f"  diff vs snapshot #{result.previous_snapshot_id}: {len(result.change_ids)} change(s) recorded")
    return 0


def cmd_feed(args) -> int:
    tz = ZoneInfo(args.tz)
    with _open_ledger(args) as ledger:
        rows = ledger.feed(account_id=args.account, since=parse_time(args.since) if args.since else None,
                           entity_id=args.entity, flagged_only=args.flagged, limit=args.limit)
    if args.json:
        print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("（変更なし）")
    for r in rows:
        print(format_change(r, tz))
    return 0


def cmd_check(args) -> int:
    policy = load_policy(args.policy)
    sinks = []
    alerts_file = args.alerts_file or policy.alerts.file
    if alerts_file:
        sinks.append(FileAlertSink(alerts_file))
    if not args.no_webhook:
        sinks.append(WebhookAlertSink(policy.alerts.webhook_url_env))
    creative = None
    if args.creative_rules:
        from .creative import CreativeWatch, load_attributes, load_creative_rules, prohibited_from_policy
        creative = CreativeWatch(load_creative_rules(args.creative_rules), load_attributes(args.attributes),
                                 prohibited_from_policy(policy))
    with _open_ledger(args) as ledger:
        watched = watch_check(ledger, policy, sinks, include_evaluated=args.all, creative=creative)
    results = watched.changes
    counts = {"violation": 0, "warning": 0, None: 0}
    for r in results:
        counts[r.evaluation.severity] += 1
        if r.evaluation.severity:
            print(f"[{r.evaluation.severity.upper()}] #{r.change.id} {summarize(r.change)}")
            for f in r.evaluation.findings:
                print(f"    - {f.rule_id}: {f.reason}")
            for d in r.deliveries:
                print(f"    alert -> {d['sink']}: {d['status']}" + (f" ({d['reason']})" if d.get("reason") else ""))
    print(f"evaluated {len(results)} change(s): {counts['violation']} violation(s), "
          f"{counts['warning']} warning(s), {counts[None]} ok")
    if creative is not None:
        for r in watched.creatives:
            score = "スコアなし（原本なし）" if r["band"] is None else f"score={r['score']} band={r['band']}"
            print(f"[CREATIVE] check #{r['check_id']} creative {r['creative_id']}: {score}"
                  + ("  → alert" if r.get("alert") else ""))
        print(f"creative check: {len(watched.creatives)} creative(s)（法的判定ではなく確認の優先順位付けの材料）")
    return 0


def cmd_revert(args) -> int:
    with _open_ledger(args) as ledger:
        changes = ledger.feed(flagged_only=True) if args.flagged else [ledger.get_change(i) for i in args.change]
        if not changes:
            print("対象の変更がありません")
            return 0
        plan = plan_for_changes(changes, ledger)
        if args.json:
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(plan.render())
        if not args.execute_on_mock:
            print("\n(dry-run: 何も実行していません。Phase 1 では実アカウントへの実行経路はありません)")
            return 0
        mock = MockAdsClient(args.execute_on_mock)
        results = execute_plan(plan, mock)
        ledger.record_revert_run(mock.source, plan.change_ids, plan.to_dict(), [r.__dict__ for r in results])
    print(f"\nモッククライアント（{mock.source}）に対して実行:")
    for r in results:
        print(f"  {r.order}. {r.status}  {r.detail}")
    return 0


# --------------------------------------------------------------------------- Phase 2: proxy


DOWNSTREAMS = ("mock-stdio", "http")


def _add_downstream_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--downstream", choices=DOWNSTREAMS, default="mock-stdio",
                        help="mock-stdio (default): the in-repo mock as a child process; http: an MCP streamable-HTTP "
                             "endpoint, token from REIN_ADS_MCP_TOKEN")
    parser.add_argument("--mock-fixture", help="mock-stdio: fixture directory the mock Ads MCP starts from")
    parser.add_argument("--mock-state", help="mock-stdio: JSON file where the mock keeps its state between processes")
    parser.add_argument("--downstream-url", help="http: endpoint URL (https; plain http only on loopback)")
    parser.add_argument("--downstream-timeout", type=float, default=30.0, help="http: seconds per operation")


def _downstream(args, *, session_class=None):
    """Validate the downstream settings before anything starts. Returns (description, async context manager)."""
    from mcp import ClientSession

    from .downstream_http import http_downstream_from_env, open_http_downstream, public_url
    from .proxy import mock_downstream_over_stdio

    session_class = session_class or ClientSession
    if args.downstream == "http":
        if args.mock_fixture or args.mock_state:
            raise ValueError("--mock-fixture / --mock-state apply to --downstream mock-stdio only")
        downstream = http_downstream_from_env(args.downstream_url or "", timeout=args.downstream_timeout,
                                              session_class=session_class)
        return {"transport": "http", "endpoint": public_url(downstream.url)}, open_http_downstream(downstream)
    if args.downstream_url:
        raise ValueError("--downstream-url applies to --downstream http only")
    if not args.mock_fixture:
        raise ValueError("--mock-fixture is required with --downstream mock-stdio")
    return ({"transport": "mock-stdio", "endpoint": f"mock fixture {args.mock_fixture}"},
            mock_downstream_over_stdio(args.mock_fixture, args.mock_state, session_class=session_class))


def cmd_proxy(args) -> int:
    import anyio

    from .proxy import serve

    if not args.policy:
        raise ValueError("--policy is required")
    policy = load_policy(args.policy)
    desc, downstream = _downstream(args)
    # stdout is the MCP channel; anything human-readable goes to stderr.
    if desc["transport"] == "http":
        print(f"rein proxy: stdio MCP server, downstream = HTTP {desc['endpoint']} (token from env, not shown)",
              file=sys.stderr)
    else:
        print(f"rein proxy: stdio MCP server, downstream = mock Ads MCP ({args.mock_fixture})", file=sys.stderr)
    anyio.run(serve, policy, args.db, downstream)
    return 0


def cmd_proxy_discover(args) -> int:
    import anyio

    from .discover import ListOnlyClientSession, build_report, list_all_tools, load_tools_document, render_summary, \
        report_json
    from .downstream_http import DiscoveryViolation, DownstreamFailure, classify

    policy = load_policy(args.policy)
    if args.tools_file:
        tools, server = load_tools_document(args.tools_file)
        desc = {"transport": "file", "endpoint": Path(args.tools_file).name}
    else:
        desc, downstream_cm = _downstream(args, session_class=ListOnlyClientSession)

        async def fetch():
            async with downstream_cm as downstream:
                if hasattr(downstream, "session"):  # stdio: one session for the whole run
                    found = await list_all_tools(downstream.session)
                else:
                    found = await downstream.list_tools()
                return found, downstream.server_info

        try:
            tools, server = anyio.run(fetch)
        except DiscoveryViolation:
            raise
        except DownstreamFailure:
            raise
        except (Exception, BaseExceptionGroup) as exc:  # stdio and other paths: reduce to a failure class
            raise classify(exc) from None
    report = build_report(tools, policy, downstream=desc, server=server)
    text = report_json(report, os.environ.get("REIN_ADS_MCP_TOKEN"))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text if args.json else render_summary(report), end="" if args.json else "\n")
    return 0


def cmd_preflight(args) -> int:
    from .preflight import as_dict, connectivity_check, offline_preflight, render

    result = offline_preflight(args.account)
    if args.live and result.ok:
        result.checks.append(connectivity_check(args.account))
    if args.json:
        print(json.dumps(as_dict(result), ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0 if result.ok else 1


def _approval_line(a: dict[str, Any]) -> str:
    client = a.get("declared_client") or {}
    rules = ",".join(r["rule_id"] for r in a.get("findings") or [])
    status = a["status"] + (f" [{a['result_kind']}]" if a.get("result_kind") else "")
    line = (f"{a['id']}  {status:<9} received={a['received_at']} expires={a['expires_at']}  "
            f"{a['tool']} {a.get('entity_type') or ''} {a.get('entity_id') or ''}  "
            f"client={client.get('name')}（申告）  rules={rules}")
    if a.get("status_reason"):
        line += f"\n    理由: {a['status_reason']}"
    return line


def cmd_approvals_list(args) -> int:
    from .proxy import ReinProxy

    with _open_ledger(args) as ledger:
        # Expiry needs no downstream; a null downstream is enough to mark overdue approvals.
        ReinProxy(load_policy(args.policy) if args.policy else _empty_policy(), ledger, _NoDownstream()).expire_due()
        rows = ledger.list_approvals(args.status)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("（承認待ちなし）")
    for a in rows:
        print(_approval_line(a))
        print(f"    arguments: {json.dumps(a['arguments'], ensure_ascii=False)}")
        for r in a.get("findings") or []:
            print(f"    - {r['rule_id']}: {r['reason']}")
    return 0


def cmd_approvals_approve(args) -> int:
    import anyio

    from .proxy import ReinProxy

    policy = load_policy(args.policy)

    _desc, downstream_cm = _downstream(args)

    async def run():
        async with downstream_cm as downstream:
            with _open_ledger(args) as ledger:
                return await ReinProxy(policy, ledger, downstream).approve(args.approval_id, args.by)

    outcome = anyio.run(run)
    kind = f" [{outcome.result_kind}]" if outcome.result_kind else ""
    print(f"{outcome.approval_id}: {outcome.status}{kind} — {outcome.message}")
    if outcome.changed and outcome.status in ("executed", "execution_failed", "failed"):
        print(f"  downstream: {outcome.detail or '(nothing)'}")
    return 0 if outcome.executed else 2


def cmd_approvals_reject(args) -> int:
    from .proxy import ReinProxy

    with _open_ledger(args) as ledger:
        outcome = ReinProxy(_empty_policy(), ledger, _NoDownstream()).reject(args.approval_id, args.by, args.note)
    print(f"{outcome.approval_id}: {outcome.status} — {outcome.message}")
    return 0 if outcome.changed else 2


def cmd_audit(args) -> int:
    with _open_ledger(args) as ledger:
        rows = ledger.audit_log(limit=args.limit, approval_id=args.approval)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("（監査記録なし）")
    for r in rows:
        client = (r.get("declared_client") or {}).get("name")
        who = f"client={client}（申告）" + (f" operator={r['operator']}" if r.get("operator") else "")
        target = f"{r.get('entity_type') or ''} {r.get('entity_id') or ''}".strip()
        decision = r["decision"] + (f" [{r['result_kind']}]" if r.get("result_kind") else "")
        print(f"#{r['id']:<4} {r['occurred_at']}  {r['event']:<18} {decision:<16} {r['tool']}  {target}  {who}"
              + (f"  approval={r['approval_id']}" if r.get("approval_id") else ""))
        print(f"       args: {r['arguments_summary']}")
        for reason in r.get("reasons") or []:
            print(f"       - {reason['rule_id']}: {reason['reason']}"
                  + (f"  detail: {reason['detail']}" if reason.get("detail") else ""))
        if r.get("forwarded") and r.get("result_summary"):
            print(f"       result: {r['result_summary']}")
    return 0


# --------------------------------------------------------------------------- Phase 3: creative / evidence


def _snapshot_for(ledger: Ledger, account: str | None, snapshot_id: int | None):
    sid = snapshot_id or ledger.latest_snapshot_id(account)
    if sid is None:
        raise KeyError("no snapshot in the ledger (run `rein snapshot` first)")
    snap = ledger.load_snapshot(sid)
    if account and snap.account_id != account:
        raise KeyError(f"snapshot #{sid} belongs to {snap.account_id}, not {account}")
    return snap


def _memos(args) -> list[dict[str, str]]:
    memos = []
    if args.memo_file:
        memos += json.loads(Path(args.memo_file).read_text(encoding="utf-8"))
    for item in args.memo or []:
        claim, sep, source = item.partition("=")
        if not sep:
            raise KeyError(f"--memo must be CLAIM=SOURCE: {item!r}")
        memos.append({"claim": claim.strip(), "source": source.strip()})
    return memos


def cmd_creative_register(args) -> int:
    from .creative import register_original

    with _open_ledger(args) as ledger:
        snap = _snapshot_for(ledger, args.account, args.snapshot)
        oid = register_original(ledger, snap, creative_id=args.creative, ad_id=args.ad, by=args.by,
                                substantiation=_memos(args), category=args.category, brand=args.brand, note=args.note)
        o = ledger.get_original(oid)
    print(f"original #{oid}: creative {o['creative_id']}（{o['account_id']}, snapshot #{o['snapshot_id']}）"
          f" registered_at={o['registered_at']} by {o['registered_by']['name']}（自己申告・未検証）"
          f" ads={','.join(o['ad_ids']) or '-'} 根拠メモ {len(o['substantiation'])} 件")
    return 0


def cmd_creative_originals(args) -> int:
    with _open_ledger(args) as ledger:
        rows = ledger.list_originals(args.account)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("（登録済みの原本なし）")
    for o in rows:
        print(f"#{o['id']:<4} {o['registered_at']} {o['account_id']} creative {o['creative_id']} "
              f"ads={','.join(o['ad_ids']) or '-'} by {o['registered_by']['name']}（自己申告）")
        for m in o["substantiation"]:
            print(f"       根拠: 「{m['claim']}」 → {m['source']}")
    return 0


def cmd_creative_check(args) -> int:
    from .creative import (creative_view, load_attributes, load_creative_rules, place_on_ad, prohibited_from_policy,
                           render, run_check)
    from .snapshot import normalize_entity

    rules = load_creative_rules(args.rules)
    extra = prohibited_from_policy(load_policy(args.policy)) if args.policy else ()
    with _open_ledger(args) as ledger:
        snap = _snapshot_for(ledger, args.account, args.snapshot)
        if args.candidate_file:
            raw = json.loads(Path(args.candidate_file).read_text(encoding="utf-8"))
            entity = normalize_entity("creative", raw)
            creative_id = str(entity.get("id") or "")
            if not creative_id:
                raise KeyError("candidate file needs an `id`")
            if args.ad:
                snap = place_on_ad(snap, args.ad, creative_id, entity)
        else:
            creative_id = args.creative
            entity = snap.get("creative", creative_id)
            if entity is None:
                raise KeyError(f"creative {creative_id} not found in snapshot #{snap.id}")
        result = run_check(ledger, rules, account_id=snap.account_id, creative_id=creative_id,
                           candidate=creative_view(entity), snapshot=snap, attributes=load_attributes(args.attributes),
                           explicit_original_id=args.original, category=args.category, brand=args.brand,
                           extra_prohibited=extra, record=not args.no_record)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return 0


def cmd_evidence_export(args) -> int:
    from .evidence import export_pack

    files = [args.policy, args.proxy_policy] + ([args.creative_rules] if args.creative_rules else [])
    with _open_ledger(args) as ledger:
        manifest = export_pack(ledger, args.out, args.from_, args.to, policy_files=files)
    print(f"wrote evidence pack to {args.out}: {manifest['counts']['timeline_entries']} timeline entries, "
          f"{manifest['counts']['creative_checks']} creative checks, {len(manifest['files'])} files hashed (sha256)")
    print("※ ハッシュはエクスポート後の改変検出のためのもの。エクスポート前の台帳自体の無改変は保証しない。")
    return 0


def cmd_evidence_verify(args) -> int:
    from .evidence import verify_pack

    result = verify_pack(args.pack)
    if result.ok:
        print(f"OK: {result.checked} files match manifest.json（エクスポート後の改変なし。台帳自体の無改変は対象外）")
        return 0
    print(f"FAILED: {len(result.problems)} problem(s)")
    for problem in result.problems:
        print(f"  - {problem}")
    return 2


class _NoDownstream:
    async def list_tools(self):
        return []

    async def call(self, name, arguments):
        raise RuntimeError("no downstream in this command")


def _empty_policy():
    from .policy import parse_policy
    return parse_policy({"rules": []})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rein", description="Watch + Revert, MCP proxy, Creative Check and evidence packs "
                                                     "for Meta ad accounts (real account access is read-only)")
    p.add_argument("--db", default=os.environ.get(DB_ENV, "rein.db"), help=f"SQLite ledger path (env {DB_ENV})")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("snapshot", help="take a snapshot and record changes vs the previous one")
    src = s.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture", help="read recorded responses from this fixture directory (mock client)")
    src.add_argument("--account", help="read this ad account via the Marketing API (read-only; token from env)")
    s.add_argument("--policy", help="policy.yaml whose `attribution` section is used to label actors")
    s.set_defaults(func=cmd_snapshot)

    f = sub.add_parser("feed", help="show the change ledger in time order")
    f.add_argument("--account")
    f.add_argument("--since", help="ISO 8601 time")
    f.add_argument("--entity", help="entity id")
    f.add_argument("--flagged", action="store_true", help="only violations and warnings")
    f.add_argument("--limit", type=int)
    f.add_argument("--tz", default="UTC", help="display timezone, e.g. Asia/Tokyo")
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_feed)

    c = sub.add_parser("check", help="evaluate recorded changes against policy.yaml and emit alerts")
    c.add_argument("--policy", required=True)
    c.add_argument("--alerts-file", help="override alerts.file from the policy")
    c.add_argument("--all", action="store_true", help="re-evaluate already evaluated changes")
    c.add_argument("--no-webhook", action="store_true", help="do not even consider the webhook sink")
    c.add_argument("--creative-rules", help="also run Creative Check on new/changed creatives (Phase 3)")
    c.add_argument("--attributes", help="category/brand attributes JSON for Creative Check")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("revert", help="show a revert plan (dry-run by default)")
    target = r.add_mutually_exclusive_group(required=True)
    target.add_argument("--change", type=int, action="append", help="change id (repeatable)")
    target.add_argument("--flagged", action="store_true", help="all flagged changes")
    r.add_argument("--execute-on-mock", metavar="FIXTURE_DIR",
                   help="run the plan against a mock client loaded from this fixture (in memory only)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_revert)

    px = sub.add_parser("proxy", help="run the Rein MCP proxy on stdio (downstream: the mock, or an HTTP endpoint)")
    px.add_argument("--policy")
    _add_downstream_args(px)
    px.set_defaults(func=cmd_proxy)
    pxsub = px.add_subparsers(dest="proxy_command")
    dc = pxsub.add_parser("discover", help="read-only: list the downstream's tools and report how Rein fits them "
                                           "(sends initialize and tools/list only)")
    dc.add_argument("--policy", required=True, help="policy whose proxy.read_tools is compared")
    _add_downstream_args(dc)
    dc.add_argument("--tools-file", help="build the report from a saved tools/list JSON document (no connection)")
    dc.add_argument("--json", action="store_true", help="print the JSON report instead of the summary")
    dc.add_argument("--out", help="also write the JSON report to this file")
    dc.set_defaults(func=cmd_proxy_discover)

    ap = sub.add_parser("approvals", help="list / approve / reject queued proxy calls")
    asub = ap.add_subparsers(dest="approvals_command", required=True)
    al = asub.add_parser("list")
    al.add_argument("--status", choices=["pending", "approved", "rejected", "expired", "executed", "aborted", "failed",
                                         "execution_failed"])
    al.add_argument("--policy", help="optional; not needed to list")
    al.add_argument("--json", action="store_true")
    al.set_defaults(func=cmd_approvals_list)
    aa = asub.add_parser("approve", help="approve and run the queued call once (after re-checking)")
    aa.add_argument("approval_id")
    aa.add_argument("--policy", required=True)
    _add_downstream_args(aa)
    aa.add_argument("--by", default=getpass.getuser(), help="operator name recorded in the audit (not verified)")
    aa.set_defaults(func=cmd_approvals_approve)
    ar = asub.add_parser("reject", help="reject a queued call; it will never run")
    ar.add_argument("approval_id")
    ar.add_argument("--by", default=getpass.getuser())
    ar.add_argument("--note")
    ar.set_defaults(func=cmd_approvals_reject)

    au = sub.add_parser("audit", help="show the proxy audit log")
    au.add_argument("--approval", help="only records for this approval id")
    au.add_argument("--limit", type=int)
    au.add_argument("--json", action="store_true")
    au.set_defaults(func=cmd_audit)

    pf = sub.add_parser("preflight", help="check the read-only Watch configuration (offline unless --live)")
    pf.add_argument("--account", help="ad account id, act_<digits>")
    pf.add_argument("--live", action="store_true",
                    help="also make read-only GET requests to the Marketing API (the owner's later, manual step)")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_preflight)

    cr = sub.add_parser("creative", help="Creative Check: register approved originals and check candidates")
    csub = cr.add_subparsers(dest="creative_command", required=True)
    rg = csub.add_parser("register-original", help="register a creative from a snapshot as the approved original")
    which = rg.add_mutually_exclusive_group(required=True)
    which.add_argument("--creative", help="creative id")
    which.add_argument("--ad", help="register the creative this ad uses now")
    rg.add_argument("--account")
    rg.add_argument("--snapshot", type=int, help="snapshot id (default: latest)")
    rg.add_argument("--by", default=getpass.getuser(), help="who registers it (self-declared, not verified)")
    rg.add_argument("--memo", action="append", metavar="CLAIM=SOURCE", help="substantiation note (repeatable)")
    rg.add_argument("--memo-file", help='JSON list of {"claim": ..., "source": ...}')
    rg.add_argument("--category")
    rg.add_argument("--brand")
    rg.add_argument("--note")
    rg.set_defaults(func=cmd_creative_register)
    ol = csub.add_parser("originals", help="list registered originals")
    ol.add_argument("--account")
    ol.add_argument("--json", action="store_true")
    ol.set_defaults(func=cmd_creative_originals)
    ck = csub.add_parser("check", help="check one creative against its original (not a legal judgement)")
    target = ck.add_mutually_exclusive_group(required=True)
    target.add_argument("--creative", help="creative id in the snapshot")
    target.add_argument("--candidate-file", help="raw creative JSON (Marketing API shape) to check")
    ck.add_argument("--ad", help="with --candidate-file: check it as if swapped onto this ad")
    ck.add_argument("--original", type=int, help="original id to compare with (default: same creative / ad lineage)")
    ck.add_argument("--rules", required=True, help="creative rules YAML")
    ck.add_argument("--attributes", help="category/brand attributes JSON")
    ck.add_argument("--policy", help="also apply this policy's prohibited_terms rules")
    ck.add_argument("--category")
    ck.add_argument("--brand")
    ck.add_argument("--account")
    ck.add_argument("--snapshot", type=int)
    ck.add_argument("--no-record", action="store_true", help="do not store the result in the ledger")
    ck.add_argument("--json", action="store_true")
    ck.set_defaults(func=cmd_creative_check)

    ev = sub.add_parser("evidence", help="export / verify an evidence pack")
    esub = ev.add_subparsers(dest="evidence_command", required=True)
    ex = esub.add_parser("export", help="write the records of a period as a hashed evidence pack")
    ex.add_argument("--from", dest="from_", required=True, help="ISO 8601 with offset, inclusive")
    ex.add_argument("--to", required=True, help="ISO 8601 with offset, inclusive")
    ex.add_argument("--out", required=True, help="new (or empty) directory")
    ex.add_argument("--policy", required=True, help="Phase 1 policy file to include")
    ex.add_argument("--proxy-policy", required=True, help="Phase 2 policy file to include")
    ex.add_argument("--creative-rules", help="creative rules file to include")
    ex.set_defaults(func=cmd_evidence_export)
    vf = esub.add_parser("verify", help="check every file of a pack against manifest.json")
    vf.add_argument("pack")
    vf.set_defaults(func=cmd_evidence_verify)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    from .downstream_http import DownstreamFailure

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DownstreamFailure as exc:  # already reduced to a class: no bodies, headers or tokens
        print(f"error: downstream failure: {exc.label} (nothing was executed)", file=sys.stderr)
        return 1
    except (ClientError, PolicyError, KeyError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
