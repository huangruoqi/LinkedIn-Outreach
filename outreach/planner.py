"""
Message Planner — core module.

Two modes:
  - API mode:  ANTHROPIC_API_KEY is set → calls Claude API
  - Stub mode: no key → uses deterministic template (safe for offline tests)

Usage:
    from outreach.planner import plan_message

    result = plan_message(prospect_dict, conversation_dict)
    # result = { prospect_id, stage, action, message, generated_at, mode }
"""

import json
import os
import ssl
import urllib.request
import urllib.error
from datetime import datetime, timezone

import certifi

# macOS + python.org builds don't bundle CA certs — use certifi's bundle.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# ── Prompt templates ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert LinkedIn outreach writer (recruiting, networking, or warm reconnection).

Rules:
- Connection request notes must be ≤300 characters (LinkedIn hard limit — count carefully)
- Follow-up messages must be ≤500 characters
- Reference at least one specific detail from the prospect's recent posts or background when relevant
- Never use these phrases: "I came across your profile", "I'd love to pick your brain",
  "synergy", "hope this message finds you", "reaching out to connect", "touching base"
- Sound human and specific — not like a mass template
- You MUST open with or include the prospect's first name somewhere in the message
- Honor the stated end_goal: for none, write a warm note with no ask for a meeting, call, resume, or next-step scheduling
- Do not add any preamble or explanation — return only the message text itself
"""

_VALID_END_GOALS = frozenset({"schedule_meeting", "obtain_resume", "none"})


class UnknownCampaignTopicError(ValueError):
    """Raised when a campaign_topic id is set but not present in planner config."""


def resolve_campaign_topic(
    prospect: dict,
    conversation: dict | None = None,
    planner_config: dict | None = None,
) -> dict | None:
    """
    Resolve the campaign topic (tone + CTA) to use for this prospect.

    Selection order:
      1. ``prospect["campaign_topic"]`` (operator override on the prospect)
      2. ``conversation["campaign_topic"]`` (snapshot taken at connect time)
      3. ``planner_config["default_campaign_topic"]`` (organization default)

    If a topic id is provided anywhere in the chain but is not a key inside
    ``planner_config["campaign_topics"]``, raise ``UnknownCampaignTopicError``
    with a message that lists the valid ids — callers (skills, worker) should
    surface this to the operator instead of silently picking a wrong tone.

    If no id is provided anywhere, return ``None`` so the planner keeps its
    legacy "no campaign topic" behavior (tone + CTA come from message_rules).
    """
    conversation = conversation or {}
    config = planner_config or {}
    topics = config.get("campaign_topics")
    if not isinstance(topics, dict):
        topics = {}

    def _clean(value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    topic_id = (
        _clean(prospect.get("campaign_topic"))
        or _clean(conversation.get("campaign_topic"))
        or _clean(config.get("default_campaign_topic"))
    )
    if topic_id is None:
        return None

    topic = topics.get(topic_id)
    if not isinstance(topic, dict):
        valid = sorted(topics.keys())
        raise UnknownCampaignTopicError(
            f"Unknown campaign_topic {topic_id!r}. "
            f"Valid topics: {valid if valid else '(none configured)'}."
        )

    return {
        "id": topic_id,
        "label": topic.get("label") or topic_id.replace("_", " ").title(),
        "description": topic.get("description") or "",
        "tone": str(topic.get("tone") or "").strip(),
        "cta": str(topic.get("cta") or "").strip(),
    }


def resolve_end_goal(prospect: dict) -> str:
    """
    Canonical end goal for prompts and persistence.

    Prefer prospect['end_goal']; if missing, map legacy target_action; else default schedule_meeting.
    """
    raw = prospect.get("end_goal")
    if isinstance(raw, str) and raw in _VALID_END_GOALS:
        return raw
    legacy = prospect.get("target_action")
    if legacy == "request_resume":
        return "obtain_resume"
    if legacy == "schedule_call":
        return "schedule_meeting"
    if legacy == "general_outreach":
        return "schedule_meeting"
    return "schedule_meeting"


def _outreach_topic_line(prospect: dict, conversation: dict) -> str:
    topic = prospect.get("outreach_topic")
    if isinstance(topic, str) and topic.strip():
        return topic.strip()
    snap = conversation.get("outreach_topic")
    if isinstance(snap, str) and snap.strip():
        return snap.strip()
    return "(no dedicated topic — use notes and profile signals only)"


def _end_goal_instructions(end_goal: str, action: str) -> str:
    if end_goal == "obtain_resume":
        return (
            "End goal: recruiting / obtain_resume. "
            "Steer naturally toward learning fit; it is OK to hint that sharing a resume or profile "
            "could help match them to opportunities"
            + (" in a later message" if action == "send_connection_request" else "")
            + ". Do not demand a resume in the connection note."
        )
    if end_goal == "none":
        return (
            "End goal: none (relationship only). "
            "No pitch, no meeting request, no calendar link, no resume ask, no 'let's find time to chat' "
            "about work opportunities. A light human reason to connect is fine."
        )
    return (
        "End goal: schedule_meeting (default). "
        "It is OK to suggest a brief intro call or meeting when natural"
        + ("; the connection note can lightly tee that up without being pushy" if action == "send_connection_request" else "")
        + "."
    )


def _campaign_topic_block(topic: dict | None) -> str:
    if not topic:
        return (
            "Campaign topic: (no campaign topic configured — fall back to message_rules tone)"
        )
    label = topic.get("label") or topic["id"]
    description = topic.get("description") or ""
    tone = topic.get("tone") or ""
    cta = topic.get("cta") or ""
    lines = [
        f"Campaign topic: {label} ({topic['id']})",
    ]
    if description:
        lines.append(f"Topic intent: {description}")
    if tone:
        lines.append(f"Required tone for this campaign: {tone}")
    if cta:
        lines.append(
            f"Suggested CTA flavor (rephrase naturally — do not paste verbatim): {cta}"
        )
    return "\n".join(lines)


def _build_user_prompt(
    prospect: dict,
    conversation: dict,
    action: str,
    planner_config: dict | None = None,
) -> str:
    recent_posts = prospect.get("recent_posts", [])
    posts_text = "\n".join(
        f'- "{p["text"][:120]}..." ({p["timestamp"]}, {p["likes"]} likes)'
        for p in recent_posts[:3]
    )

    prior_messages = conversation.get("messages", [])
    history_text = "\n".join(
        f'[{m["sender"]}] {m["text"]}'
        for m in prior_messages[-4:]
    ) or "(no prior messages)"

    first_name = prospect.get("name", "").split()[0]
    char_limit = 300 if action == "send_connection_request" else 500
    end_goal = resolve_end_goal(prospect)
    topic_line = _outreach_topic_line(prospect, conversation)
    goal_rules = _end_goal_instructions(end_goal, action)
    campaign_topic = resolve_campaign_topic(prospect, conversation, planner_config)
    campaign_block = _campaign_topic_block(campaign_topic)
    return f"""Generate a LinkedIn message for this action: {action.replace("_", " ")}
HARD LIMIT: {char_limit} characters maximum. Count carefully before responding.
The prospect's first name is "{first_name}" — you must use it in the message.

--- PROSPECT ---
Name:       {prospect.get("name")}
Title:      {prospect.get("title")}
Company:    {prospect.get("company")}
Location:   {prospect.get("location")}
Notes:      {prospect.get("notes", "")}

--- TOPIC & GOAL ---
Conversation topic (anchor the angle of the message): {topic_line}
Resolved end_goal: {end_goal}
{goal_rules}

--- CAMPAIGN TOPIC (tone + CTA) ---
{campaign_block}

Recent posts:
{posts_text or "(none)"}

--- CONVERSATION HISTORY ---
{history_text}

--- TASK ---
Action:     {action}
Stage:      {prospect.get("outreach_stage", "cold")}

Write the message now. Return only the message text.
"""


# ── API planner ───────────────────────────────────────────────────────────────

def _plan_with_api(
    prospect: dict,
    conversation: dict,
    action: str,
    planner_config: dict | None = None,
) -> str:
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    payload = json.dumps({
        "model": model,
        "max_tokens": 256,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": _build_user_prompt(
                    prospect, conversation, action, planner_config
                ),
            },
        ],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as r:
            return json.loads(r.read())["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Anthropic API error {e.code}: {e.read().decode()}")


# ── Stub planner (offline / testing) ─────────────────────────────────────────

def _plan_stub(
    prospect: dict,
    conversation: dict,
    action: str,
    planner_config: dict | None = None,
) -> str:
    name  = prospect["name"].split()[0]
    posts = prospect.get("recent_posts", [])
    hook  = posts[0]["text"][:60] if posts else prospect.get("notes", "your background")[:60]
    end_goal = resolve_end_goal(prospect)
    topic = _outreach_topic_line(prospect, conversation)
    topic_bit = (
        f" On {topic} —" if topic != "(no dedicated topic — use notes and profile signals only)" else ""
    )
    campaign_topic = resolve_campaign_topic(prospect, conversation, planner_config)
    cta_phrase = (campaign_topic or {}).get("cta") or ""
    cta_bit = f" — {cta_phrase}" if cta_phrase else ""

    if action == "send_connection_request":
        if end_goal == "none":
            return (
                f"Hey {name} —{topic_bit} been a while and great to see what you're up to lately. "
                f"Would love to connect here{cta_bit}."
            ).replace("  ", " ")[:300]
        if end_goal == "obtain_resume":
            return (
                f"Hey {name} — saw your post on {hook}... "
                f"We're hiring in that space and your angle stood out.{topic_bit} "
                f"Would love to connect{cta_bit}."
            ).replace("  ", " ")[:300]
        connect_tail = cta_phrase or "find time for a quick intro if you're open"
        return (
            f"Hey {name} — saw your recent post on {hook}... "
            f"Your take resonated.{topic_bit} Would love to connect and {connect_tail}."
        ).replace("  ", " ")[:300]

    if action == "send_followup_message":
        if end_goal == "none":
            return (
                f"Thanks for connecting, {name}! "
                f"Really glad we're in touch here — hope we can catch up informally when life allows{cta_bit}."
            )[:500]
        if end_goal == "obtain_resume":
            return (
                f"Thanks for connecting, {name}! "
                f"We're hiring for a role that maps closely to your {prospect.get('title', 'background')} experience. "
                f"Would you be open to sharing your resume so I can pass it along to the team{cta_bit}?"
            )[:500]
        followup_tail = cta_phrase or "a short intro call this week or next"
        return (
            f"Thanks for connecting, {name}! "
            f"Loved your background in {prospect.get('title', 'this space')}. "
            f"Would you be open to {followup_tail}? Happy to work around your schedule."
        )[:500]

    return f"Hi {name}, following up — happy to answer any questions about the role."


# ── Public interface ──────────────────────────────────────────────────────────

def plan_message(
    prospect: dict,
    conversation: dict,
    planner_config: dict | None = None,
) -> dict:
    """
    Returns a planned message dict.
    Uses the Claude API if ANTHROPIC_API_KEY is set, otherwise falls back to the stub.

    ``planner_config`` is the merged JSON returned by MCP
    ``get_conversation_planner_config``. When provided, ``campaign_topics`` and
    ``default_campaign_topic`` drive the resolved campaign tone + CTA used in
    the prompt and the stub output. Unknown topic ids raise
    ``UnknownCampaignTopicError``.
    """
    action = conversation.get("next_action", "send_connection_request")
    campaign_topic = resolve_campaign_topic(prospect, conversation, planner_config)

    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        message = _plan_with_api(prospect, conversation, action, planner_config)
        mode = "api"
    else:
        message = _plan_stub(prospect, conversation, action, planner_config)
        mode = "stub"

    return {
        "prospect_id":  prospect["id"],
        "stage":        prospect.get("outreach_stage", "cold"),
        "action":       action,
        "message":      message,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode":         mode,
        "campaign_topic": campaign_topic,
    }
