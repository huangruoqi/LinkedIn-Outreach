---
name: send-connection-request
description: Send a LinkedIn connection request (with an optional personalised note) via the MCP send_connection_request tool, then persist pipeline state with save_connection, upsert_conversation, append_action_log, and remove_pending_queue_entry — never raw outreach/ paths. Use when the user asks to connect with, invite, or add a LinkedIn profile.
---

# Send Connection Request

Scrape a LinkedIn profile, then immediately send a connection request — no confirmation step needed.

**Filesystem rule:** Do not read or write `outreach/` files via workspace paths. Use MCP tools from
`tools/server.py`: **`save_connection`**, **`get_conversation`**, **`upsert_conversation`**,
**`append_action_log`**, **`remove_pending_queue_entry`**.

**Test / fixtures:** Never read, edit, or overwrite `tests/fixtures/` or other `tests/` files during
connection flows. Do not seed MCP upserts from fixture JSON unless the user is explicitly maintaining tests.

## When to Use

- User asks to connect with, invite, or add a LinkedIn profile
- First step in an outreach sequence for a 2nd or 3rd-degree prospect
- Prospect file exists and `next_action` is `"send_connection_request"`

## Inputs

- `profile_url` (required) — full LinkedIn profile URL, e.g. `https://www.linkedin.com/in/username/`
- `note` (optional) — personalised connection note (LinkedIn limit: **300 chars**). Omit to send without a note.
- `campaign_topic` (optional) — campaign topic id (e.g. `founder_outreach`, `technical_peer`) that selects tone + CTA from `campaign_topics` in the runtime planner config. Omit to use `default_campaign_topic`. **Unknown ids must hard-fail** — list valid ids back to the operator instead of guessing.

**Prospect JSON** (via `upsert_prospect` before you generate the note) drives the message planner:

- `end_goal` — **`schedule_meeting`** (default when omitted): steer toward a short intro call or meeting. **`obtain_resume`**: recruiting path toward sharing a resume or profile artifact (maps from legacy `target_action: request_resume`). **`none`**: warm connect only — no meeting, resume, or scheduling ask in generated copy (e.g. old friend).
- `outreach_topic` — optional string that anchors what to talk about (overrides relying only on `notes` / profile). Examples: a specific role, product area, or “catching up after grad school.”
- `campaign_topic` — optional campaign topic id. Persist the operator's selection here when provided so the conversation-planner reuses the same tone + CTA later.

### Campaign topics (tone + CTA)

Campaign topics let one connection skill ship distinct outreach intents without
changing the workflow. Each entry in `campaign_topics` (under
`outreach/config/conversation_planner.json`) has at minimum a `tone` and a
`cta`. Built-in examples include:

| Topic id                 | When to use                                              |
|--------------------------|----------------------------------------------------------|
| `founder_outreach`       | Talking to founders (concise, founder-friendly).         |
| `recruiter_outreach`     | Warm-up with in-house / agency recruiters.               |
| `investor_outreach`      | Connect with investors around thesis or portfolio fit.   |
| `technical_peer`         | Engineer-to-engineer peer comparison.                    |
| `customer_discovery`     | Learn how a target user solves a workflow today.         |
| `hiring_manager_outreach`| Warm a hiring manager around candidate referrals.        |

**Resolution order** (first non-empty wins):

1. The `campaign_topic` argument the operator passed to the skill.
2. `prospect.campaign_topic` (snapshot from a previous step).
3. `planner_config.default_campaign_topic` (configured fallback).

If none are set, send the note with the default planner tone (no campaign topic
applied — same as before this feature existed).

If the resolved id is **not** a key in `campaign_topics`, stop before sending
and report: `"Unknown campaign_topic '<id>'. Valid topics: [<list>]"`. Do not
silently pick another topic.

## Steps

### 0. Resolve the campaign topic

1. Call **`get_conversation_planner_config`** and parse the JSON.
2. Read `campaign_topics` (mapping of id → `{tone, cta, ...}`) and
   `default_campaign_topic`.
3. Pick the topic id with the resolution order documented above
   (`campaign_topic` arg → `prospect.campaign_topic` → `default_campaign_topic`).
4. If a topic id was selected and it is not present in `campaign_topics`, abort
   immediately with: `"Unknown campaign_topic '<id>'. Valid topics: [<sorted list>]"`.
5. Carry the resolved `{id, tone, cta}` into the note generation in step 2 so
   the message matches the campaign intent.

### 1. Scrape the profile

Call the `scrape_profile` MCP tool first to fetch the prospect's details:

```
Tool: scrape_profile
  profile_url: <the LinkedIn URL>
```

Use the scraped data to:
- Check `connection_degree` — if it is `1`, abort and report: `"<Name> is already a 1st-degree connection. Use send_message to reach them directly."`
- Personalise the note (if one is being generated based on the user's instructions) using name, title, about, and recent_posts

### 2. Send the connection request

Call `send_connection_request` immediately — no need to ask for confirmation:

```
Tool: send_connection_request
  profile_url: <the LinkedIn URL>
  note:        <note text, or omit for no note>
```

If a note is provided, verify it is ≤ 300 characters before calling the tool. Trim silently if needed.

The tool attaches to the running Chrome session, navigates to the profile, clicks the Connect button (or opens the More menu if Connect is hidden), optionally adds the note, and submits the invitation.

### 3. Handle the response

| Response | Meaning                                    | Action                                              |
|----------|--------------------------------------------|-----------------------------------------------------|
| `"ok"`   | Request sent successfully                  | Print confirmation (see below)                       |
| anything else | Send failed (already connected, pending, button not found, etc.) | Report the error; do NOT retry automatically |

### 4. Print confirmation

On success:

```
── Connection Request Sent ───────────────────────────────────
To:       <Name> (<profile_url>)
Title:    <title from scrape>
Sent at:  <current ISO timestamp>
Note:     "<note text>" (or "(none)")
─────────────────────────────────────────────────────────────
```

### 5. Update conversation state (if using outreach pipeline)

When you have a `prospect_id` for the pipeline:

1. **`get_conversation(prospect_id)`** — if the tool returns JSON text, parse it into `conversation`.
   If it returns `error: conversation not found`, build a minimal valid `conversation` object (schema:
   `prospect_id`, `outreach_stage`, `messages: []`, etc.) in memory.
2. Append to `conversation.messages` (conversation schema — no extra keys):
   `{ "sender": "operator", "text": "<note text or brief system line>", "timestamp": "<ISO UTC>", "sequence_step": 1 }`.
   Use the real note when one was sent; if none, use a short line such as `(connection request sent, no note)`.
3. Set `last_action` → `"send_connection_request"`, `last_action_timestamp` → now,
   `next_action` → `null`, and advance `outreach_stage` / `stage_history` per your pipeline (e.g.
   toward `pending_connection`). Snapshot **`end_goal`** (resolved: default `schedule_meeting` unless
   the prospect sets `end_goal` or legacy `target_action`), **`outreach_topic`**, and the resolved
   **`campaign_topic`** id onto the conversation so later steps reuse the same tone + CTA.
4. **`upsert_conversation(prospect_id, json.dumps(conversation))`**
5. **`append_action_log(entry=json.dumps({...}))`**:
```json
{ "action": "connection_request_sent", "prospect_id": "<id>", "timestamp": "<ISO>", "note_char_count": <n> }
```
6. If you use the pending queue: **`remove_pending_queue_entry(prospect_id)`**

### 6. Update the connections list (MCP)

Call **`save_connection`** with:

| Parameter | Value |
|-----------|--------|
| `profile_url` | same LinkedIn URL |
| `name` | from scrape |
| `title` | from scrape (headline) |
| `prospect_id` | pipeline id if you already have one; if omitted, **`save_connection` fills it** from the LinkedIn URL slug (so conversation-planner batch mode can resolve the prospect) |
| `note_sent` | note text, or `null` if sent without a note |
| `connection_status` | `"pending"` |

`save_connection` upserts by `profile_url` inside the project’s `connections.json` — do **not** edit
that file manually.

## Examples

### Default topic (no override)

**User:** "Connect with https://www.linkedin.com/in/alexchen/ and say we met at NeurIPS"

```
Tool call → get_conversation_planner_config()
→ default_campaign_topic = "founder_outreach"
   campaign_topics["founder_outreach"] = {
     tone: "concise and founder-friendly", cta: "open to a quick chat"
   }

Tool call → scrape_profile(profile_url="https://www.linkedin.com/in/alexchen/")
→ { name: "Alex Chen", title: "ML Engineer at Acme", connection_degree: 2, ... }

Tool call → send_connection_request(
  profile_url="https://www.linkedin.com/in/alexchen/",
  note="Hi Alex — great meeting you at NeurIPS! Open to a quick chat once you're back?"
)

── Connection Request Sent ───────────────────────────────────
To:       Alex Chen (https://www.linkedin.com/in/alexchen/)
Title:    ML Engineer at Acme
Topic:    founder_outreach (tone: concise and founder-friendly)
Sent at:  2026-04-03T14:10:00+00:00
Note:     "Hi Alex — great meeting you at NeurIPS! Open to a quick chat once you're back?"
─────────────────────────────────────────────────────────────
```

### Explicit campaign topic — technical peer

**User:** "Connect with https://www.linkedin.com/in/priyak/ as a technical peer"

```
Tool call → get_conversation_planner_config()
→ campaign_topics["technical_peer"] = {
    tone: "technical and collaborative", cta: "compare notes"
  }

Tool call → scrape_profile(profile_url="https://www.linkedin.com/in/priyak/")
→ { name: "Priya K.", title: "Staff Engineer, Inference", connection_degree: 2, ... }

Tool call → send_connection_request(
  profile_url="https://www.linkedin.com/in/priyak/",
  note="Hi Priya — your post on KV cache eviction matched what we hit last week. Would love to connect and compare notes."
)
```

### Unknown campaign topic — hard fail

**User:** "Connect with https://www.linkedin.com/in/sam/ for `sales_outreach`"

`sales_outreach` is not defined in `campaign_topics`. Do **not** call
`send_connection_request`; report:

```
Unknown campaign_topic 'sales_outreach'.
Valid topics: ['customer_discovery', 'founder_outreach', 'hiring_manager_outreach',
'investor_outreach', 'recruiter_outreach', 'technical_peer'].
```

## Error Handling

- **Unknown `campaign_topic`** — resolution returned an id missing from `campaign_topics`. Do **not** send; report `"Unknown campaign_topic '<id>'. Valid topics: [<sorted list>]"` and ask the operator to pick a valid id or add it to `outreach/config/conversation_planner.json`.
- **Already a 1st-degree connection** — detected via scrape; do not call the tool. Report: `"<Name> is already a 1st-degree connection. Use send_message to reach them directly."`
- **Note too long** — trim to 300 chars before calling the tool.
- **Connect button not found** — tool returns an error string. Possible causes: pending request already sent, profile set to followers-only, or InMail-only. Report the raw error and suggest checking the profile manually.
- **Chrome not running** — CDP connection fails. Report: `"Could not connect to Chrome. Make sure Chrome is running with --remote-debugging-port=9222."`
- **Not logged in** — tool raises an error. Report: `"Not logged in to LinkedIn. Log in manually in the Chrome window and retry."`
- **Bot detection** — if the action fails with a timeout or unexpected redirect, stop immediately and report: `"LinkedIn may have triggered bot detection. Wait a few minutes before retrying."`
- **Daily limit** — LinkedIn imposes weekly invitation limits (~100–200). If errors appear after several sends in a session, pause and report: `"You may have hit LinkedIn's weekly invitation limit. Check your My Network page."`