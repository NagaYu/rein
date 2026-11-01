# Phase 5 live findings (2026-11-01)

> **Published copy:** IDs are masked here (account `act_…7745`, campaigns by their last six digits, system user omitted; `[masked]` marks a redaction inside quoted server text). The owner's local record keeps the full IDs.

Findings from Rein's first contact with Meta's real Ads MCP server, run by the repository owner on 2026-10-31 and
2026-11-01. All of it happened on an empty test ad account in the owner's own business portfolio, with no payment
method. No production account was touched and nothing was spent.

Sources are marked so a reader can tell what was recorded from what was seen:

- **[report]:** [discover-live.json](../discover-live.json), the read-only discovery report.
- **[ledger]:** Rein's own audit and approval records from the live runs, in the owner's local ledger (not in the
  repository).
- **[owner]:** observed by the owner in the Marketing API, Ads Manager or Business Settings; not recorded by Rein.

## 1. Server identity and coverage

- **Server:** "Meta Ads MCP Server" 1.0.0 at `https://mcp.facebook.com/ads`, reached over MCP streamable HTTP with
  a system-user Bearer token. It offers **98 tools**. [report]
- **Refit:** Rein's interpreter was refitted offline to the reported names and required lists (Phase 5C). The
  report records names only: no types, enum values or nested shapes.
- **Coverage after the refit, against the full real list:** [report, re-evaluated]

  | Outcome | Tools | What they are |
  |---|---|---|
  | Passthrough | 9 | Reviewed reads on `proxy.read_tools`: ad accounts, entities, creatives, activity logs, images, videos, one custom audience, account pages, Instagram accounts |
  | Evaluated | 6 | The writes Rein interprets: update, activate, and create campaign / ad set / ad / creative |
  | Denied | 83 | 47 read-looking tools not yet reviewed (catalog, insights, experiments, datasets, pixels, ...) and 36 writes without an interpreter (catalog, pixels, custom audiences, creative update/delete/upload, experiments, Instagram boost) |

- **Why the denied remainder stays denied.** The proxy is fail-closed by design:
  - a tool becomes a passthrough read only after its schema is reviewed and it is added to `proxy.read_tools`;
    the server's `readOnlyHint` is not trusted;
  - a write is allowed or queued only when Rein can turn its arguments into a change its rules can judge.

  Every one of the 83 is a review or interpreter task, not a defect.

## 2. The gated live sequence

| Step | What ran | Result |
|---|---|---|
| (a) Discovery | `rein proxy discover` against the real endpoint: initialize and tools/list only | 98 tools, saved as `discover-live.json` [report] |
| (a2) First reads, sandbox ad account | `ads_get_ad_accounts`, `ads_get_ad_entities`, `ads_account_get_activity_logs` through `scripts/proxy_call.py` | The account list was empty; entity and activity reads failed with `protocol_error` [ledger]. The Marketing API answered #200 [owner]. Cause: a sandbox account is app-owned and cannot be assigned to a system user (see [real-account-setup.md](real-account-setup.md)). |
| (a2) Reads, business-owned test account | The same reads after the account was assigned to the system user with full access | The account list showed the test account, with Ads MCP enabled and no payment method; entities `[]`; activity logs `[]` [ledger]. The Marketing API #200 cleared [owner]. |
| (c) Governed write #1 | `ads_create_campaign` (name "Rein Gate C Test", `OUTCOME_TRAFFIC`, `AUCTION`) on `act_…7745` | `require_approval` (rules `business-hours`: Sunday 15:34 JST, outside Mon–Fri 09:00–19:00; and `new-campaign-needs-approval`) → human approval → `executed`, dispatched once [ledger] |
| (c) Governed write #2 | The same call, re-run with the Phase 5D build | Same: `require_approval` (same two rules) → human approval → `executed`, dispatched once; the CLI printed the server's answer [ledger] |
| (d) Attribution in the activity log | — | Unmeasured in Phase 5 (nothing materialised); **measured in Phase 7** (section 6) |

Each creation went through the full loop, and no approval executed more than once. The decisions came from
Rein's policy file, not from the server.

## 3. The draft finding

**What the server answered to `ads_create_campaign`** (both runs, shortened) [ledger]:

```json
{"campaign_id": "…570355 [masked]", "status": "DRAFT",
 "spec": "{\"name\":\"Rein Gate C Test\",\"objective\":\"OUTCOME_TRAFFIC\",\"special_ad_categories\":[],\"status\":\"ACTIVE\",\"buying_type\":\"AUCTION\"}",
 "valid_optimization_goals": "[...]", "recommended_optimization_goal": "LINK_CLICKS", "active_errors": "[]"}
```

Run #1 returned campaign ID `…020355`, and run #2 returned `…570355`. Both answers were
transport-successful, with `active_errors` empty.

**What the same server says about that ID.** `ads_get_ad_entities` with `object_ids: ["…570355"]` on
the same account answered [ledger]:

```json
{"ad_entities": "[]",
 "additional_info": "The following object_ids are not campaign entities in the requested ad account: …570355 [masked]."}
```

**Elsewhere:**
- An unfiltered `ads_get_ad_entities` on the account stayed `[]`, and so did `ads_account_get_activity_logs`, after
  each creation [ledger].
- The Marketing API campaign count stayed 0 [owner].
- Ads Manager showed 0 campaigns and no drafts, and review-and-publish was disabled [owner].

**Is there a publish step?** The 98 tools were scanned [report]:
- No tool is named publish, apply, submit or launch.
- Some arguments point that way, though:
  - `ads_activate_entity` declares `publish_as_active` and `allow_partial_publish`;
  - `ads_update_entity` declares `discard_draft_changes` and `confirmation_token`.

This suggests drafts may be published through activation, but **that is not verified**. (Phase 7 later published one draft this way; see section 6.) Rein currently denies all
four arguments, because none has been reviewed or interpreted.

**Reading.** The Ads MCP's campaign creation returns a draft descriptor. Its ID is not an entity of the ad account,
and nothing reaches the account through the tools Rein used. Rein recorded each creation as `executed`, because
the server accepted the call. Only the verification reads show that no campaign exists.

Section 6 records what happened when the run #2 draft was later published through a governed activation (Phase 7).

## 4. The Phase 5D lesson

- **Run #1 used the pre-5D build.** The approval CLI printed only that the call had been executed. The server's
  answer (the draft descriptor) was stored in the ledger's approval record and, cut to 240 characters, in the
  audit row. But nothing showed it to the operator, and nothing set it apart from a real creation. To the person
  approving, the creation was silent. That silence is why every write in the gated sequence is now followed by
  verification reads.
- **What changed in Phase 5D:**
  - every execution carries the server's answer, bounded and token-scrubbed, on the approval record, in the audit,
    and in the CLI output;
  - an answer that is flagged `isError`, or whose content is an error object, ends as `execution_failed`, never
    `executed`;
  - before 5D, such an answer, or an empty one, could be recorded as `executed`.
- **What 5D does not catch.** A draft answer is not an error by any of those tests, so run #2 is still `executed`,
  now with the draft visible in its detail. Telling "accepted as a draft" apart from "created" needs a rule of its
  own, such as an answer with `status: DRAFT` or a follow-up entity read. That is a candidate for a later phase,
  not something Rein does today.

## 5. Limits of these findings

- **Attribution is measured for one kind of change only.** The activity log names the system user and the app
  for a change made through this app and system user: one publish, section 6. Changes made by Meta's own AI
  features were never in scope here and remain unmeasured, as do changes by people in Ads Manager or by other
  apps.
- **The UI and the account-level listing do not show the published campaign.** Ads Manager and the MCP
  account-level entities listing still showed no campaign about 27 minutes after the publish, while the by-ID
  read, the Marketing API count and the activity log all show it (section 6). The cause is unproven.
- **Drafts elsewhere are unverified.** Whether these drafts surface in some other Meta agent or assistant UI was not
  checked, and was not chased.
- **Small sample.** One empty test account in the owner's own business, with no payment method, so no spend was
  possible; two creation calls and one publish; no updates, ad sets, ads or creatives. Nothing here shows how a
  production account behaves.
- **Mixed sources.** Sections 3 and 6 combine Rein's records with the owner's console and browser observations
  ([owner]). Those observations are not reproducible from this repository.
- **No secrets.** This note contains no token values or lengths and no personal contact details. The account,
  campaign and system-user IDs are the owner's own test data, included in full as local evidence. Masking them is
  a publication-export step (see the publication checklist), not an edit to this note.

## 6. Phase 7: the activation path (built offline, run live on 2026-11-01)

**Status:** the offline work is described first, then the live run. The owner ran the live run from their own
Terminal, one step at a time, with each step confirmed in chat.

**What was implemented:**
- **Publish flags.** `ads_activate_entity` now accepts `publish_as_active` and `allow_partial_publish`, but only as
  literal booleans. A string `"true"`, `1`, `null` or an object stays uninterpretable, and the call is denied.
- **`publish-needs-approval`.** A call with `publish_as_active: true` always needs a person's approval: never
  allow, whatever the business hours or the target's state. A deny from another rule stays a deny.
  `allow_partial_publish` on its own changes nothing.
- **Real lookup shape.** Rein's current-value lookups now also read the real server's answer shape
  (`ad_entities` as a JSON string, plus `additional_info`). When the server states that an ID is not an account
  entity, an activation of that ID is queued for approval with the server's words as the reason's detail
  (`rein:target-not-entity`). An update of such an ID, or an ID the server says nothing about, is still denied.
- **`created_draft` tag.** An executed create whose answer has top-level `status: "DRAFT"` is tagged
  `created_draft` on the approval and the audit row, in addition to `executed`. `approvals list`,
  `approvals approve` and `audit` show the tag. It is reporting only; states and decisions are unchanged.
- **Not covered:** `discard_draft_changes` and `confirmation_token` on `ads_update_entity` stay uninterpretable,
  so calls using them are denied.

**The mock draft lifecycle** (`--draft-lifecycle`, off by default):
1. `ads_create_campaign` answers a DRAFT descriptor with a fresh ID.
2. The entities tool does not list that ID, and an `object_ids` query for it answers with the same "not campaign
   entities" note the real server gave.
3. `ads_activate_entity` on the draft without `publish_as_active: true` fails with an error, which ends as
   `execution_failed` after approval.
4. With `publish_as_active: true`, the draft becomes a real campaign (status PAUSED), visible both by `object_ids`
   and at account level.
5. The activity log gains one entry for that activation, so the whole verification chain can be rehearsed offline.

**Live run, 2026-11-01** (times JST; the audit row numbers are in the owner's local ledger):

**Target and control.** The target was the run #2 draft, `…570355`. The run #1 draft,
`…020355`, is the control: no activation was ever attempted on it. Its ID appears only in the run #1
approval and audit records [ledger].

| Time | Step | Result | Source |
|---|---|---|---|
| 17:12:55 | Baseline: `ads_get_ad_entities` by `object_ids` | `ad_entities` `[]`, with "The following object_ids are not campaign entities in the requested ad account: …570355 [masked]." The draft was still not an entity | [ledger] audit #16 |
| 17:13:08 | `ads_activate_entity` with `{ad_account_id, entity_id: "…570355", entity_type: "campaign", publish_as_active: true}`; `allow_partial_publish` not set | `require_approval`, approval `apr_93c093f868e6`, rules exactly `rein:target-not-entity`, `publish-needs-approval`, `business-hours`. The target-not-entity reason carries the server's sentence as its detail | [ledger] audit #17 |
| 17:14:05 / 17:14:12 | Owner's approval, then execution | `executed`, dispatched once | [ledger] audit #18, approval record |
| 17:15:25 | Entities by `object_ids` | `[{"id": "…570355 [masked]", "name": "Rein Gate C Test"}]`: the campaign reads back by ID | [ledger] audit #19 |
| 17:15–17:16 | `rein preflight --live` (Marketing API) | "1 campaign(s) visible", where it had been 0 | [owner] |
| 17:16:08 | Account-level entities (no `level`, no `fields`) | `ad_entities` `[]` | [ledger] audit #20 |
| 17:16:19 | Activity log | One entry for the object: see attribution below | [ledger] audit #21, [owner] |
| 17:17–17:18 | Ads Manager, read-only check | 0 campaigns; review-and-publish disabled | [owner] |
| 17:21:01 | Account-level entities, again | `ad_entities` `[]` | [ledger] audit #22 |
| 17:41–17:42 | Ads Manager, again | 0 campaigns; review-and-publish still disabled | [owner] |

**The server's answer to the publish** [ledger, audit #18]:

```json
{"ad_account_id": "act_…7745 [masked]", "entity_id": "…570355 [masked]", "entity_type": "campaign",
 "status": "PUBLISHING", "success": null, "publish_as_active_applied": true}
```

The answer is not error content by Rein's tests, so the approval is `executed`. It is not a create answer either,
so it carries no `created_draft` tag.

**Attribution (gate d), measured.** The activity log returned one entry for `…570355`. Rein's ledger
holds the first 240 characters of that answer [ledger, audit #21]:
- event type "キャンペーンが作成されました" ("a campaign was created");
- `actor_id`: the system user's ID (omitted in this copy);
- `actor_name` "rein-system";
- the object ID.

The owner's console showed the rest of the entry: time 2026/11/1 17:14, and `application_name` "Rein" [owner].
For a change carried by this app and its system user, the log therefore names the system user and the app. It
names neither the account owner nor "Meta AI". This is a measurement of one change made through Rein's own app; it
says nothing about how Meta's own AI features or other apps appear.

**The split that did not resolve.** Three API-side surfaces show the campaign after the publish:
- the by-ID entities read [ledger, audit #19];
- the Marketing API count [owner];
- the activity log [ledger, audit #21].

Two surfaces do not:
- the MCP account-level entities listing was `[]` at 17:16:08 and again at 17:21:01 [ledger, audit #20, #22];
- Ads Manager showed 0 campaigns at 17:17–17:18 and again at 17:41–17:42, with review-and-publish disabled
  throughout [owner]. That is about 27 minutes after the server answered `PUBLISHING`.

This is recorded as a standing observation, and its cause is unproven. Two possibilities fit the evidence, and
neither has been tested:
- the account-level listing call sends no `level` or `fields`;
- the Ads Manager index may differ from the object store.

**What the campaign is.** A bare shell: no ad sets, no ads, no budget, and no payment method on the account. It
cannot spend, and the account was never charged [owner].
