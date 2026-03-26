import logging
import time

import ai
import whatsapp
from config import (
    POLL_INTERVAL_SECONDS,
    TARGET_JID,
    WAIT_AFTER_REPLY_SECONDS,
)

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def collect_their_messages(messages: list[dict], since_id: str) -> str:
    """
    Walk the message list (newest first) and collect all consecutive inbound
    messages that arrived after since_id, then return them as one combined string.
    """
    new_parts: list[str] = []
    for msg in messages:
        if msg["MsgID"] == since_id:
            break
        if msg["FromMe"]:
            break
        text = msg.get("Text") or msg.get("DisplayText") or ""
        new_parts.append(text)
    return " ".join(reversed(new_parts)).strip()


def send_and_record(jid: str, reply: str) -> None:
    """Send a reply and record every part in the AI history."""
    parts = whatsapp.send_message(jid, reply)
    ai.add_assistant_messages(parts)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    whatsapp.start_sync()
    logger.info("Sync started")

    # ── Opening message ───────────────────────────────────────────────────────
    logger.info("Generating opening message...")
    opening = ai.get_reply("generate the opening message")
    send_and_record(TARGET_JID, opening)
    logger.info("Opening sent, waiting for reply...")

    # Record the last message we sent so we don't re-process it
    messages = whatsapp.get_messages(TARGET_JID)
    last_seen_id: str | None = messages[0]["MsgID"] if messages else None

    # ── Poll loop ─────────────────────────────────────────────────────────────
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)

        try:
            messages = whatsapp.get_messages(TARGET_JID)
        except RuntimeError as exc:
            logger.error("Failed to fetch messages: %s", exc)
            continue

        if not messages:
            continue

        latest = messages[0]

        # Ignore our own messages and anything we've already processed
        if latest["FromMe"] or latest["MsgID"] == last_seen_id:
            continue

        # New inbound message detected — wait in case they're still typing
        logger.info("New message detected, waiting %ds...", WAIT_AFTER_REPLY_SECONDS)
        time.sleep(WAIT_AFTER_REPLY_SECONDS)

        # Re-fetch after the wait; collect everything they sent since last_seen_id
        try:
            messages = whatsapp.get_messages(TARGET_JID)
        except RuntimeError as exc:
            logger.error("Failed to re-fetch messages: %s", exc)
            continue

        combined_text = collect_their_messages(messages, last_seen_id)
        if not combined_text:
            logger.warning("Could not extract text from new messages, skipping")
            continue

        last_seen_id = messages[0]["MsgID"]
        logger.info("They said: %s", combined_text)

        try:
            reply = ai.get_reply(combined_text)
        except RuntimeError as exc:
            logger.error("AI call failed: %s", exc)
            continue

        try:
            send_and_record(TARGET_JID, reply)
        except RuntimeError as exc:
            logger.error("Failed to send message: %s", exc)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted by user, stopping sync...")
        whatsapp.stop_sync()
    except Exception:
        logger.exception("Fatal error")
        whatsapp.stop_sync()
        raise