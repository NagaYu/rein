# Rein reference

Details behind the [README](../README.md). Everything here runs against fixtures and mocks. The database is a local
SQLite file (`--db`, or the `REIN_DB` environment variable; default `rein.db`).

## Watch + Revert

### CLI

```bash
# Snapshot a recorded fixture. The first snapshot of an account is the baseline.
rein --db rein.db snapshot --fixture fixtures/scenarios/1_budget_spike/before --policy configs/policy.example.yaml
rein --db rein.db snapshot --fixture fixtures/scenarios/1_budget_spike/after  --policy configs/policy.example.yaml

rein --db rein.db feed --tz Asia/Tokyo          # change ledger in time order
rein --db rein.db check --policy configs/policy.example.yaml   # evaluate rules, emit alerts
rein --db rein.db revert --change 1             # revert plan, dry-run (nothing is executed)
rein --db rein.db revert --change 1 --execute-on-mock fixtures/scenarios/1_budget_spike/after   # in memory only
```

Useful options: `feed --flagged`, `feed --entity <id>`, `feed --since <ISO time>`, `feed --json`, `check --all`
(re-evaluate), `check --no-webhook`, `revert --flagged`, `revert --json`.

The read-only Marketing API client exists for future use and is tested only with a mock transport. It reads its token
from the environment and nowhere else (`REIN_META_ACCESS_TOKEN`; API version `REIN_META_API_VERSION`, default
`v26.0`). It has no write methods, and its transport refuses anything but GET.

### Ledger rows (`changes` table)

| Column | Meaning |
|---|---|
| `id` | Change ID, passed to `rein revert --change` |
| `detected_at` | Time of the snapshot that found the change. Not the time the change was made |
| `from_snapshot_id` / `to_snapshot_id` | The change happened somewhere between these two snapshots |
| `entity_type` / `entity_id` / `entity_name` | campaign / adset / ad / creative |
| `field` | Dotted path such as `targeting.age_max`; `*` means the entity was created or disappeared |
| `change_type` | `modified` / `added` / `removed` / `entity_created` / `entity_deleted` |
| `before` / `after` | Values as JSON |
| `actor_kind` / `attribution` | Actor and the evidence for it (see below) |
| `severity` / `findings` | `violation`, `warning` or empty, and which rules matched and why |

### Attribution

Rein never guesses an actor. A change gets `human`, `external_agent` or `meta_ai` only when all of these hold:

1. The account activity history has an entry for the same object with an `event_type` that corresponds to the field.
2. Its `event_time` falls between the two snapshots.
3. Its `actor_id` or `application_id` is declared by the advertiser in the policy's `attribution` section.

| `attribution.basis` | Meaning | actor_kind |
|---|---|---|
| `activity_match` | All three hold | as declared |
| `activity_matched_actor_undeclared` | History matches, but the actor / app is not declared | unknown |
| `ambiguous_activities` | Several matches that disagree on the actor kind | unknown |
| `no_matching_activity` | No match (outside the history's retention, or a change the history does not record) | unknown |
| `no_event_type_mapping` | Rein has no `event_type` for that field (for example creating a creative) | unknown |

### Rules

| Rule type | Watch result | Notes |
|---|---|---|
| `budget_cap` | violation | `after > max` is a violation; `== max` is fine. Compared as the API's integers. Non-numeric values cannot be verified and count as violations. New entities' budgets are checked too |
| `frozen_fields` | violation | Any change, addition or removal on a matching path; paths are case-sensitive |
| `prohibited_terms` | violation | NFKC-normalized, so full-width "ｎｏ．１" matches "No.1"; case-insensitive by default |
| `change_window` | warning | Changes outside the window. Start inclusive, end exclusive; overnight windows allowed. The time used is, in order: the activity `event_time`, an `updated_time` inside the snapshot interval, the detection time |
| `budget_increase_ratio` | proxy only by default | One change may raise a budget by at most `max_increase_pct` of the current value; exactly the limit is allowed |
| `approval_required` | proxy only by default | Matching changes always go to a person |

Every rule also takes `on_proxy: deny | require_approval` and `scopes: [watch, proxy]`.

**`creative_resolved` and `resolved`.** A creative's copy and link cannot be updated after it is created, so a
"rewrite" shows up as the ad pointing to a different creative. Each ad snapshot therefore carries the link and copy of
the creative it currently uses as `creative_resolved.{link,message,headline,description,call_to_action}`, and a swap
becomes a field change on the ad. The creative's own copy is under `resolved.*`.

### Revert

`rein revert` shows a plan by default. Each step holds the target, the update in Graph API parameter form, and the
expected current value: if the account no longer matches it, the step is not applied. Order: pause-direction status
changes, then budgets and bids, then creative swaps, then the rest, and re-activation last.

Left as **manual** instead of reverted: newly created entities, entities that disappeared, content changes on a
creative itself, swaps whose previous creative is not in the latest snapshot, and fields that were unset before or
that Rein does not revert automatically (such as `targeting`). Execution refuses any client but the mock.

### Alerts

`check` appends one JSON object per alert to `alerts.file` (default `alerts.jsonl`). A webhook POST (https only) is
attempted only when the environment variable named in `alerts.webhook_url_env` is set. A change that has been
evaluated is not alerted again unless `check --all` is used.

## MCP proxy

```bash
rein --db rein.db proxy --policy configs/policy.proxy.example.yaml --mock-fixture fixtures/base --mock-state mock_state.json
```

The proxy is an MCP server on stdio (stdout carries MCP, logs go to stderr). By default its downstream is the mock
Ads MCP, started as a subprocess (`--downstream mock-stdio`, which needs `--mock-fixture`).

### HTTP downstream

```bash
export REIN_ADS_MCP_TOKEN=...   # see docs/real-account-setup.md for a way that keeps it out of shell history
rein --db rein.db proxy --policy configs/policy.proxy.example.yaml --downstream http --downstream-url <endpoint URL>
```

- Selected with CLI flags, like the mock: `--downstream http`, `--downstream-url`, `--downstream-timeout` (seconds
  per operation, default 30). The same flags work on `rein approvals approve` and `rein proxy discover`. The policy
  file is unchanged. No URL is built into the code; Meta's documented endpoint (`https://mcp.facebook.com/ads`)
  appears only in docs.
- URL rules: https for any host, plain http only for loopback (the local mock), no credentials in the URL.
- Transport: the official MCP Python SDK's streamable-HTTP client (`streamable_http_client` with an
  `httpx.AsyncClient` carrying `Authorization: Bearer <token>`). Each downstream operation (one current-value lookup,
  one forwarded call, one `tools/list`) is its own MCP session: connect, `initialize`, one request, close.
  `Mcp-Session-Id` and protocol-version negotiation are handled by the SDK; Rein keeps no session state.
- Token: read only from `REIN_ADS_MCP_TOKEN`. If it is unset or empty, the proxy refuses to start with an error that
  names the variable and never a value. The token is kept in a closure (not in object attributes), never placed in
  audit rows, results or errors, and a log-record scrubber removes it if any library ever logs it.
- Failures are reduced to a class, recorded in the audit, and handled like the stdio path's failures:

  | Failure class | Cause |
  |---|---|
  | `unreachable` | connection refused / reset |
  | `unauthorized (401)` / `forbidden (403)` | the endpoint rejected the token |
  | `http_status (<code>)` | any other non-2xx status |
  | `timeout` | no answer within `--downstream-timeout` |
  | `malformed` | unparseable JSON, unexpected content type |
  | `protocol_error` / `error` | MCP-level error, anything else |

  A current-value lookup that fails makes a write `require_approval` (`rein:current-unavailable` plus
  `rein:downstream-failure: <class>`), never allow. A forwarded call that fails is audited with `downstream_ok = 0`
  and `result_summary = "downstream error: <class>"`; nothing is retried. At approval time, a failed re-check aborts
  the approval.

**Failure detail (Phase 5D).** Next to its class, every HTTP downstream failure keeps the server's own words as
`detail`. For an MCP / JSON-RPC error that is its code and message; for an HTTP error, the status, any
`www-authenticate` challenge and the body; for anything else, the exception text. `detail` is diagnostic only: it
never changes a decision or a failure class. It is one line, at most 500 characters, with the exact token and any
`Bearer <...>` value replaced by `[redacted]` before it is stored or printed. Results the server returns are
token-scrubbed the same way. Where it appears:

- **A forwarded call that fails:** the result text stays `downstream error: <class>`, the result carries
  `structuredContent.rein_downstream = {"failure", "detail"}`, and the audit row gets a `rein:downstream-failure`
  reason with `detail`.
- **A lookup that fails:** the `rein:downstream-failure` reason carries `detail`, and the deny or approval payload
  has `downstream_detail`.
- **`scripts/proxy_call.py`:** its JSON output has a `downstream_detail` key.

**Execution results (Phase 5D).** When `rein approvals approve` runs a queued call, what the downstream answered
is stored on the approval (`result.detail`), written to the audit row's `result_summary`, and printed by the CLI
(`downstream: ...`). The outcome is decided from the answer, not from transport success:

| Outcome | When |
|---|---|
| `executed` | the answer is a normal result (for example a created object's id) |
| `execution_failed` | the answer is flagged `isError`; or its structured content, or its text parsed as JSON, has a non-empty `error` / `errors` field or `success: false`; or its text starts with `error:`, `failed:` or `failure:` |
| `failed` | the call never got an answer (unreachable, 401, timeout, ...) |

Detection is deliberately narrow: ids, status fields and `success: true` never trip it. An empty answer stays
`executed`, with detail `(no text or structured content in the response ...)`, so check it before trusting it.
`execution_failed` and `failed` approvals are consumed: there is no retry and no re-approval, and trying again
needs a fresh call and a fresh approval. Ledgers created before this change get the new status through a table
rebuild that copies every row unchanged.

**Draft publication (Phase 7).**

- `ads_activate_entity` accepts `publish_as_active` and `allow_partial_publish` as literal booleans only; any
  other type is denied.
- With `publish_as_active: true` the call always needs approval (rule `publish-needs-approval`). A deny from
  another rule still wins. `allow_partial_publish` alone changes nothing.
- Lookups read both `{"data": [...]}` and the real server's `{"ad_entities": "<JSON list>", "additional_info": ...}`.
  When the server says an activation target is not an account entity (for example a draft), the call queues with
  `rein:target-not-entity` and the server's words as its detail. Updates of such IDs, and IDs the server says
  nothing about, stay denied.
- An executed create whose answer has top-level `status: "DRAFT"` is tagged `created_draft` (column
  `result_kind`) on the approval and audit rows, and shown by `approvals list`, `approvals approve` and `audit`.
  Reporting only.
- `discard_draft_changes` and `confirmation_token` on `ads_update_entity` stay uninterpretable.

The HTTP mock (`python -m rein.mock_ads_mcp_http --fixture fixtures/base [--state mock_state.json] [--port 8765]`)
serves the same tools on `http://127.0.0.1:<port>/mcp`. It accepts any non-empty Bearer value and answers 401
without one. `--fault` (repeatable) simulates failures; none of the messages is Meta's real wording:

- `jsonrpc-errors`: tool errors come back as JSON-RPC errors.
- `write-iserror`: writes answer `isError: true` with an error message.
- `write-error-body`: writes answer a transport-successful result whose content is `{"error": ...}`.
- `echo-token`: every call fails with an error that echoes the Authorization header.

`--draft-lifecycle` (both mocks) models the Phase 5 behaviour:
- campaign creates answer DRAFT descriptors that are not entities;
- activating a draft fails unless `publish_as_active: true` is given;
- publishing turns the draft into a PAUSED campaign and adds an activity-log entry.

### Discovery (`rein proxy discover`)

```bash
rein proxy discover --policy configs/policy.proxy.example.yaml --mock-fixture fixtures/base
rein proxy discover --policy configs/policy.proxy.example.yaml --downstream http --downstream-url <URL> --json --out report.json
rein proxy discover --policy configs/policy.proxy.example.yaml --tools-file fixtures/discovery/assumed_tools_list.json
```

Discovery sends `initialize` and `tools/list` and nothing else. Its connections use a session class that raises on
any other request, `tools/call` included, before it leaves the process. The JSON report (sorted keys, no timestamps,
so the same input gives the same bytes) lists per tool:

- how the proxy treats it and by which rule: `passthrough` (listed in `proxy.read_tools`), `evaluated` (a known
  write tool with an interpreter) or `denied` (anything else, fail-closed);
- whether the interpreter has an argument mapping (`ASSUMED_ARGUMENTS` in `proxy.py`, the same table the
  interpreter uses);
- the downstream's annotations, shown as hints only;
- gaps: `write_without_mapping`, `read_hint_not_allowlisted`, `allowlisted_but_hints_write`,
  `schema_fields_not_in_rein`, `rein_fields_not_in_schema`, `schema_requires_unknown_to_rein`,
  `rein_requires_not_required_by_schema`, `update_fields_not_in_rein`, `rein_update_fields_not_in_schema`,
  `lookup_args_not_in_schema`, `lookup_omits_required_args`, `schema_missing`.

The report never holds credentials: endpoints are reduced to scheme, host, port and path, and the text is scrubbed
for the token. `fixtures/discovery/assumed_tools_list.json` is test material with assumed shapes, not a capture.

### Preflight (`rein preflight`)

```bash
rein preflight --account act_<digits>          # offline
rein preflight --account act_<digits> --live   # the owner's later step: read-only GETs
```

Offline it checks that `REIN_META_ACCESS_TOKEN` (the variable the read-only client already uses) is set, shown only
as present or absent and its length; that the account has the form `act_<digits>`; and that `REIN_META_API_VERSION`,
if set, looks like `vNN.N`. It also reports whether `REIN_ADS_MCP_TOKEN` is set, for information. Exit code 0 means
ready, 1 means not. `--live` runs only after the offline checks pass and lists the account's campaigns through the
existing read-only client (GET only).

### Real tool shapes (refit, Phase 5C)

The interpreter follows the tool names, property names and required lists in
[discover-live.json](../discover-live.json), the read-only discovery report from Meta's Ads MCP (server
"Meta Ads MCP Server" 1.0.0, 98 tools). That report records names only, not types, enum values or nested shapes, so
those parts are interpreted conservatively.

- **Account:** every write takes `ad_account_id` (`act_<digits>` or `<digits>`), normalised internally to
  `act_<digits>`. Lookups send it exactly as the agent wrote it.
- **Agent metadata:** `advertiser_job`, `advertiser_request`, `client_conversation_id` and `scheduled_task_run`
  appear on nearly every real tool. They are accepted, forwarded unchanged, kept in the audit's argument summary
  and hash like any argument, and never used for a decision.
- **Unknown arguments, including declared optionals:** a write is interpreted only when every other argument is
  one Rein interprets or one of the declared benign optionals (`adlabels`, which cannot change spend, delivery,
  targeting, destination or content). Anything else, even when the real schema declares it, makes the call
  uninterpretable, and it is denied. The discovery report lists these arguments per tool as
  `schema_fields_not_in_rein`; each one needs a review before it is accepted.
- **`entity_type`:** required on updates and activations. Rein reads `campaign`, `adset`, `ad_set` and `ad` in any
  case; anything else is denied.
- **`ads_update_entity` without `fields`:** valid per the real schema, but there is no change to judge and Rein
  cannot see what the downstream will do, so it is `require_approval` (`rein:no-field-changes`). The target must
  still exist.
- **`ads_create_campaign`:** `campaign_name`, `objective`, `buying_type`. Budget rules evaluate
  `campaign_daily_budget` / `campaign_lifetime_budget` / `campaign_spend_cap` as the entity's daily, lifetime and
  spend-cap budgets.
- **`ads_create_ad_set`:** `ad_set_name`, `campaign_id`, `optimization_goal`, `billing_event`, `targeting`
  (required, an object), plus budgets, `bid_amount`, `start_time`, `end_time`.
- **`ads_create_ad`:** `ad_name`, `ad_set_id`, and `creative`. The one nested form Rein reads is a reference,
  `{"creative_id": "<id>"}`: the creative is looked up and its link and text are checked like a swap. Without
  `creative`, or with any other shape (an inline spec, extra keys), the call is `require_approval`
  (`rein:creative-not-resolved`), never allow.
- **`ads_create_creative`:** only `ad_account_id` is required. Rein reads `link_url`, `message` / `primary_texts`,
  `headline` / `headlines`, `description` / `descriptions`, `call_to_action_type`, `name`, `page_id` and
  `image_hash`. Single texts map to `object_story_spec.link_data`. When any text has several variants, all of them
  go to `asset_feed_spec`, so text rules and claim extraction see every variant.
- **Current-value lookups:** `ads_get_ad_entities` with `{ad_account_id, object_ids, fields}` and
  `ads_get_creatives` with `{creative_ids, fields}` (plus `ad_account_id` when known). `level` is not sent: the
  report does not give its allowed values, and the entity type is already known from the call. `fields` lists the
  values Rein compares; its exact type in the real schema is unknown, and a failed lookup means approval, never
  allow.
- **Read allowlist:** in addition to the original three reads, `ads_account_get_activity_logs`,
  `ads_get_ad_images`, `ads_get_ad_videos`, `ads_get_custom_audience`, `ads_get_ad_account_pages` and
  `ads_get_ig_accounts` pass through, after their schemas were reviewed (see the comment in the policy file). The
  other 47 read-looking tools and the 36 writes without an interpreter are denied.

`rein proxy discover --tools-file` also accepts a saved discovery report, so the refit can be re-checked offline:

```bash
rein proxy discover --policy configs/policy.proxy.example.yaml --tools-file discover-live.json
```

Classified downstream failures (for example `unauthorized (401)`) are printed as one line,
`error: downstream failure: <class> (nothing was executed)`, with exit code 1.

`scripts/proxy_call.py` sends one tool call through a `rein proxy` child process, as an MCP client, and prints the
decision. It refuses tools outside `proxy.read_tools` unless `--allow-write` is given.

### Decisions

1. Tools listed in `proxy.read_tools` pass through (still audited). Downstream annotations such as `readOnlyHint` are
   not trusted.
2. Write tools Rein can interpret (`ads_update_entity`, `ads_activate_entity`, `ads_create_campaign`,
   `ads_create_ad_set`, `ads_create_ad`, `ads_create_creative`) are turned into the change they would make. The
   current values the rules need, such as a budget or an ad's current creative, are read from the downstream.
3. That change is evaluated with the same rules as Watch. When several rules match, deny beats require_approval,
   which beats allow.
4. Every other tool is denied.

| Situation | Decision | Rule in the example |
|---|---|---|
| Budget above the absolute cap | deny | `budget_cap` |
| One budget raise above the allowed ratio, even under the cap | require_approval | `budget_increase_ratio` |
| Landing URL changes, including through a creative swap | deny | `frozen_fields` |
| New campaign | require_approval | `approval_required` |
| Write outside the change window | require_approval | `change_window` |
| Creative with prohibited terms (full-width included) | deny | `prohibited_terms` |
| Unknown tool | deny | `rein:unknown-tool` |
| Uninterpretable arguments (wrong type, unknown or missing argument, unsupported field) | deny | `rein:uninterpretable` |
| Target does not exist downstream | deny | `rein:unresolvable` |
| Current value needed for the decision cannot be read | require_approval, never allow | `rein:current-unavailable` |

Boundaries fixed by tests: exactly +50% is allowed; exactly the cap is allowed; window start is inclusive and end
exclusive; an approval expires exactly at its expiry time. The time used for windows is when the proxy received the
call. A denied or queued call is not forwarded; the result has `isError: true` and a structured `rein` object with
the decision, reasons and approval ID.

### Approvals

```
pending --approve--> approved --(re-check OK)--> executed
   |                    +--(state changed)-----> aborted
   |                    +--(downstream error)--> failed
   +--reject--> rejected
   +--expiry--> expired
```

```bash
rein --db rein.db approvals list
rein --db rein.db approvals approve apr_xxxx --policy configs/policy.proxy.example.yaml --mock-fixture fixtures/base --mock-state mock_state.json --by "Staff B"
rein --db rein.db approvals reject  apr_xxxx --by "Staff B" --note "not this quarter"
rein --db rein.db audit
```

- Approve takes `pending → approved` with one conditional UPDATE; only the caller that wins it forwards the call, and
  only once. Double or concurrent approvals are refused and audited as `approval_refused`.
- Just before execution the stored precondition (for example "budget is 5000") is checked again. If it changed, if
  the call would now be denied, or if the current value cannot be read, the approval is `aborted`.
- `failed` and `aborted` approvals are never retried automatically.
- `--by` is self-declared (defaults to the OS user name). There is no approver authentication.

### Audit log (`proxy_audit` table)

| Column | Meaning |
|---|---|
| `occurred_at` | When the proxy received the call, or when the approval event happened (UTC) |
| `event` | `call`, `approval_executed`, `approval_rejected`, `approval_expired`, `approval_aborted`, `approval_failed`, `approval_refused` |
| `declared_client` | `{"name", "version", "source": "mcp.initialize.clientInfo", "verified": false}` |
| `operator` | Who approved or rejected (self-declared) |
| `tool` / `arguments_summary` / `arguments_sha256` | Tool, a 240-character summary and the SHA-256 of the full arguments |
| `decision` / `reasons` | Decision and the rules behind it |
| `entity_type` / `entity_id` | Target; for creates, the ID the downstream returned |
| `approval_id` / `forwarded` / `downstream_ok` / `result_summary` | Link to the approval, whether it was forwarded, whether it succeeded (on failure: `downstream error: <class>`) |

## Creative Check

1. **Register originals.** An advertiser-approved creative is stored as it appears in a snapshot
   (`creative_originals`). The registrant is self-declared (`"verified": false`). Substantiation notes (claim text plus
   where the evidence lives) can be attached.
2. **Find the original**, in this order: an explicit `--original`; an original with the same creative ID; the ad's
   lineage, meaning the original that ad used when it was registered, or the previous creative in Watch's recorded
   `creative.id` changes. Two or more candidates means "no original (ambiguous)". No original means no score.
3. **Diff** the normalized fields (`link`, `message`, `headline`, `description`, `call_to_action`) plus the image hash.
   Differences only in notation (width, spacing) are labelled `notation_only` but still counted. A landing-link change
   is always critical.
4. **Extract claims** with the regular expressions in the rules file, after NFKC normalization and case folding:
   numeric, price, superlative, guarantee, efficacy, limited_time. Claims missing from the original are marked new.
5. **Check:**
   - prohibited terms per category (`"*"` = all categories). The Phase 1 `prohibited_terms` can be added as an
     all-category list. The same term in the same field counts once.
   - substantiation: claim kinds listed under `substantiation.claim_kinds` need a matching note on the original,
     otherwise they are flagged as needing substantiation (major). With a note they drop to info (0 points).
   - brand rules: a missing required phrase or a banned word (major).
6. **Score:** sum of weights capped at the maximum, mapped to bands. Weights, thresholds and the cap are declared in
   the rules file, and each result keeps a per-detection `score_breakdown`.

Category and brand are not Marketing API fields; they come from an attributes file. Priority: explicit option >
creative > ad set > campaign > value stored with the original. With no category, only all-category lists apply and
the result says so.

```bash
rein creative register-original --ad 130000000000000302 --by "Staff A" --memo "売上200%アップ=internal POS report EV-0001"
rein creative originals
rein creative check --creative 130000000000000405 --rules configs/creative_rules.example.yaml --attributes fixtures/creative/attributes.json
rein creative check --candidate-file candidate.json --ad 130000000000000301 --rules configs/creative_rules.example.yaml --json
rein check --policy configs/policy.example.yaml --creative-rules configs/creative_rules.example.yaml --attributes fixtures/creative/attributes.json
```

With `rein check --creative-rules`, Watch checks every new or changed creative and every creative a changed ad now
points to. A high-band result becomes a `rein.creative_alert` attached to the triggering ad change. The proxy's
decisions are unchanged by Creative Check.

## Evidence pack

```bash
rein evidence export --from 2026-10-27T00:00:00Z --to 2026-10-31T00:00:00Z --out ./evidence-pack \
  --policy configs/policy.example.yaml --proxy-policy configs/policy.proxy.example.yaml \
  --creative-rules configs/creative_rules.example.yaml
rein evidence verify ./evidence-pack     # exit code 2 on failure
```

| File | Content |
|---|---|
| `manifest.json` | Generation time, period, accounts, Rein version and commit, sha256 and size of every other file |
| `timeline.json` / `timeline.csv` | Watch changes, alerts and revert runs merged with proxy audit entries and approvals |
| `policy/` | Copies of the policy and rules files given at export time |
| `creative_checks.json` | Creative Check results in the period |
| `summary.md` | Changes by actor (including unknown), violations and reverts, proxy decisions, approvals, creative bands, high-risk creatives |

- The period includes both ends. Times are written exactly as stored in the ledger (ISO 8601), without conversion.
- Order is deterministic: time, then record type, then ID.
- Watch changes and proxy calls are linked when they have the same entity ID and the call was forwarded successfully
  inside the snapshot interval that found the change. Fields are not compared, and the Watch actor is not rewritten.
- The counts in `summary.md` are computed from the pack's own `timeline.json` and `creative_checks.json`, and tests
  check that they agree.
- The manifest detects changes after export only. It does not prove the ledger was untouched before export. The
  `policy/` copies are the files given at export time, not necessarily the ones active during the period.

## Relationship to the official documentation

Where Meta's documentation and the original design brief disagreed, the documentation won:

- **Creatives are immutable.** The
  [Ad Creative reference](https://developers.facebook.com/docs/marketing-api/reference/ad-creative/) only allows
  updating a few fields such as `name` and `status`. Link and copy changes are therefore tracked as an ad's creative
  swap (`creative_resolved.*`), reverts point the ad back to the old creative, and Creative Check finds originals
  through ad lineage.
- **Activity history range.** The
  [Ad Activity reference](https://developers.facebook.com/docs/marketing-api/reference/ad-activity/) documents no
  time-range parameters and a default of one week, so Rein does not send `since` / `until` and filters by time itself.
- **API version.** Default `v26.0`
  ([Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog/version26.0)).
- **Ads MCP tool surface.** Budget, status and creative changes are one tool, `ads_update_entity`, and activation is
  `ads_activate_entity`, following the published
  [tool list](https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-tools-ad-creation-and-management).
  Creates are made PAUSED, as that page describes. The argument shapes are Rein's assumptions.
- **Transport.** The real Ads MCP is served over HTTP
  ([Get started](https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-get-started)).
  The proxy still serves agents over stdio. Its downstream can be streamable HTTP with a Bearer token; OAuth flows
  are not implemented, and the HTTP path has been tested only against a local mock.
- **Category and brand** are not Marketing API fields. `special_ad_categories` covers housing, employment, credit and
  similar categories, not industries, so Rein reads category and brand from an attributes file.
- **Image hash** is read from `object_story_spec` (which Watch already fetches) and is not added to Watch's normalized
  fields, so Watch's diffs are unchanged.
