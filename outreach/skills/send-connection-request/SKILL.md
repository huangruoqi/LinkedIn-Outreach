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

**Prospect JSON** (via `upsert_prospect` before you generate the note) drives the message planner:

- `end_goal` — **`schedule_meeting`** (default when omitted): steer toward a short intro call or meeting. **`obtain_resume`**: recruiting path toward sharing a resume or profile artifact (maps from legacy `target_action: request_resume`). **`none`**: warm connect only — no meeting, resume, or scheduling ask in generated copy (e.g. old friend).
- `outreach_topic` — optional string that anchors what to talk about (overrides relying only on `notes` / profile). Examples: a specific role, product area, or “catching up after grad school.”

## Preflight: Check for updates

Before doing anything else, verify the local install is current and let the
operator decide whether to update. This protects against running stale skill
logic against a moved-on MCP server.

### 0a. Call `check_for_updates`

```
Tool: check_for_updates
```

The response is a JSON object (see `tools/updater.py`):

- `installed.git_short_sha`, `installed.git_branch`, `installed.project_version`
- `remote.latest_short_sha`, `remote.latest_committed_at`, `remote.latest_message`
- `remote.error` — populated when GitHub was unreachable (offline / rate limit)
- `update_available` (bool), `behind_by` (int or null), `rollback_sha`

### 0b. Decide what to tell the operator

| Condition | Action |
|-----------|--------|
| `remote.error` is non-null | **Silently** continue — print a short note like `"(could not reach GitHub: <error>; continuing with installed <short_sha>)"` and skip the prompt. Never block on transient network issues. |
| `update_available` is `false` | **Silently** continue. Optionally print one line: `"Install up to date (<short_sha>)."` |
| `update_available` is `true` | **Pause and ask** before scraping (see below). |

### 0c. Prompt the operator (only when an update is available)

Print a single concise prompt that surfaces what changed and exactly how to
apply / skip the update, then **wait for the operator's answer before
continuing**:

```
── LinkedIn-Outreach update available ───────────────────────
Installed : <git_short_sha> on <git_branch>
Latest    : <remote.latest_short_sha>  (<remote.latest_committed_at>)
Behind by : <behind_by> commit(s)
Message   : <remote.latest_message>

To update before continuing, in another terminal run:
  make update

Reply:
  • "update"   → I'll pause here; run `make update` then say "done".
  • "skip"     → continue with the current version.
  • "abort"    → cancel this connection request.
──────────────────────────────────────────────────────────────
```

Interpret the reply as follows:

- **update / yes / y** — Do **not** call any further MCP tools. Wait for the
  operator to run `make update` (which fast-forwards git, runs `uv sync`,
  and re-syncs `~/.claude/skills`) and reply that it is done. After they
  confirm, **re-invoke this skill from the top** so the refreshed skill /
  server are in play. Do not silently continue against the old install.
- **skip / no / n / continue** — Proceed to Step 1 below with the current
  install. Add one line to the audit log so the choice is recorded:
  ```
  Tool: append_action_log
    entry: {"action":"update_skipped","timestamp":"<ISO>",
            "installed_sha":"<git_sha>","latest_sha":"<remote.latest_sha>",
            "behind_by":<behind_by>,"skill":"send-connection-request"}
  ```
- **abort / cancel / quit** — Stop. Do not scrape or send. Report:
  `"Connection request aborted at update prompt. Run `make check-update` and
  `make update` when ready."`

Never call `make update` yourself — the skill has no shell tool and the
update flow must remain an explicit operator action.

## Steps

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
   the prospect sets `end_goal` or legacy `target_action`) and **`outreach_topic`** from the prospect
   onto the conversation so later steps know what angle was used at connect time.
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

## Example

**User:** "Connect with https://www.linkedin.com/in/alexchen/ and say we met at NeurIPS"

```
Tool call → scrape_profile(profile_url="https://www.linkedin.com/in/alexchen/")
→ { name: "Alex Chen", title: "ML Engineer at Acme", connection_degree: 2, ... }

Tool call → send_connection_request(
  profile_url="https://www.linkedin.com/in/alexchen/",
  note="Hi Alex — great meeting you at NeurIPS! Would love to stay connected."
)

── Connection Request Sent ───────────────────────────────────
To:       Alex Chen (https://www.linkedin.com/in/alexchen/)
Title:    ML Engineer at Acme
Sent at:  2026-04-03T14:10:00+00:00
Note:     "Hi Alex — great meeting you at NeurIPS! Would love to stay connected."
─────────────────────────────────────────────────────────────
```

## Error Handling

- **Already a 1st-degree connection** — detected via scrape; do not call the tool. Report: `"<Name> is already a 1st-degree connection. Use send_message to reach them directly."`
- **Note too long** — trim to 300 chars before calling the tool.
- **Connect button not found** — tool returns an error string. Possible causes: pending request already sent, profile set to followers-only, or InMail-only. Report the raw error and suggest checking the profile manually.
- **Chrome not running** — CDP connection fails. Report: `"Could not connect to Chrome. Make sure Chrome is running with --remote-debugging-port=9222."`
- **Not logged in** — tool raises an error. Report: `"Not logged in to LinkedIn. Log in manually in the Chrome window and retry."`
- **Bot detection** — if the action fails with a timeout or unexpected redirect, stop immediately and report: `"LinkedIn may have triggered bot detection. Wait a few minutes before retrying."`
- **Daily limit** — LinkedIn imposes weekly invitation limits (~100–200). If errors appear after several sends in a session, pause and report: `"You may have hit LinkedIn's weekly invitation limit. Check your My Network page."`