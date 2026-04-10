import json
import logging
import time

import requests

import whatsapp
from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    MAX_HISTORY,
    MAX_TOOL_ITERATIONS,
    OWNER_JID,
)
from prompt import build_system_prompt

logger = logging.getLogger(__name__)


# ── Typing delay ──────────────────────────────────────────────────────────────

def _typing_delay(text: str) -> float:
    """
    Return a realistic delay in seconds based on message length.
    Assumes ~200 characters per minute typing speed, clamped to [2, 12] seconds.
    """
    return max(2.0, min(len(text) / 200 * 60, 12.0))


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a WhatsApp message to the lead. Call once per message. Call multiple times to send multiple separate messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The message text to send",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_owner",
            "description": (
                "Alert the agency owner when you encounter something you cannot handle: "
                "audio messages, images, files, questions about agency pricing or revenue, "
                "or anything else requiring human judgment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why the owner needs to step in",
                    }
                },
                "required": ["reason"],
            },
        },
    },
]


# ── Raw API call ──────────────────────────────────────────────────────────────

def _call_deepseek(
    turn_messages: list[dict],
    retries: int = 3,
    backoff: float = 5.0,
) -> dict:
    """
    Make one API call to DeepSeek and return the raw response dict.
    Retries on network/server errors with exponential backoff.
    """
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": turn_messages,
        "tools": TOOLS,
    }

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            logger.debug("Token usage: %s", data.get("usage"))
            return data

        except (requests.RequestException, KeyError) as exc:
            last_exc = exc
            wait = backoff * attempt
            logger.warning(
                "DeepSeek attempt %d/%d failed: %s — retrying in %.0fs",
                attempt, retries, exc, wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"DeepSeek call failed after {retries} attempts") from last_exc


# ── Main entry point ──────────────────────────────────────────────────────────

def run_turn(
    jid: str,
    history: list[dict],
    business: dict,
    user_message: str | None,
) -> dict:
    """
    Run one conversational turn for a lead.

    Args:
        jid:          The lead's ChatJID (used to send messages via wacli)
        history:      The persisted LLM conversation history for this chat
        business:     Business data dict for this lead
        user_message: The lead's new message text, or None for the opening turn

    Returns a dict:
        {
            "history":          updated history list (persist this to state),
            "messages_sent":    list of strings actually sent to the lead,
            "owner_notified":   bool,
        }
    """
    system_prompt = build_system_prompt(business)

    # Build the turn message list: system + persisted history + new user message
    # For the opening turn, we use an internal trigger that is NOT persisted to history
    turn_messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        *history[-MAX_HISTORY:],
    ]

    if user_message:
        turn_messages.append({"role": "user", "content": user_message})
    else:
        # Opening turn — transient trigger, never saved to history
        turn_messages.append({"role": "user", "content": "Conversation shuru karo. Pehla message bhejo."})

    messages_sent: list[str] = []
    owner_notified: bool = False

    # ── Agentic loop ──────────────────────────────────────────────────────────
    for iteration in range(MAX_TOOL_ITERATIONS):
        data = _call_deepseek(turn_messages)
        choice = data["choices"][0]
        assistant_msg = choice["message"]

        # Append the assistant's response (with or without tool_calls) to turn context
        turn_messages.append(assistant_msg)

        tool_calls = assistant_msg.get("tool_calls")
        if not tool_calls:
            # Model chose not to act — either done or staying silent intentionally
            logger.info("[%s] Model chose not to reply (iteration %d)", jid, iteration + 1)
            break

        # Execute each tool call and feed results back
        for tool_call in tool_calls:
            fn_name = tool_call["function"]["name"]
            try:
                args = json.loads(tool_call["function"]["arguments"])
            except json.JSONDecodeError as exc:
                logger.error("Failed to parse tool arguments: %s", exc)
                args = {}

            if fn_name == "send_message":
                text = args.get("text", "")
                try:
                    whatsapp.send_message(jid, text)
                    messages_sent.append(text)
                    result_content = "sent"
                    time.sleep(_typing_delay(text))
                except RuntimeError as exc:
                    logger.error("send_message failed: %s", exc)
                    result_content = f"error: {exc}"

            elif fn_name == "notify_owner":
                reason = args.get("reason", "")
                notify_text = f"[Lead {jid}] {reason}"
                try:
                    whatsapp.send_message(OWNER_JID, notify_text)
                    owner_notified = True
                    result_content = "owner notified"
                    logger.info("Owner notified for %s: %s", jid, reason)
                except RuntimeError as exc:
                    logger.error("notify_owner failed: %s", exc)
                    result_content = f"error: {exc}"

            else:
                logger.warning("Unknown tool called: %s", fn_name)
                result_content = "unknown tool"

            # Feed the tool result back into the turn context
            turn_messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": result_content,
            })

    # ── Update persistent history ─────────────────────────────────────────────
    # process_lead already wrote the user message and any manual outbound messages
    # to history before calling run_turn. We only need to append what the AI sent.
    updated_history = list(history)

    for text in messages_sent:
        updated_history.append({"role": "assistant", "content": text})

    return {
        "history": updated_history,
        "messages_sent": messages_sent,
        "owner_notified": owner_notified,
    }