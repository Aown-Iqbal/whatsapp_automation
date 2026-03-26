"""
main.py — WhatsApp outreach bot.

Usage:
    python main.py [leads.csv] [--dry-run]
"""

import argparse
import logging
import sys
import time

import config
import reporter
import state as state_store
import whatsapp
from ai import get_decision
from leads import load_leads
from prompt import OPENING_TRIGGER, FOLLOWUP_TRIGGER

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", nargs="?", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


# ── Send helpers ──────────────────────────────────────────────────────────────

def send(jid: str, text: str, dry_run: bool) -> None:
    if dry_run:
        logger.info("[DRY-RUN] → %s: %s", jid, text[:120])
        return
    whatsapp.send_message(jid, text)


# ── Opening / follow-up ───────────────────────────────────────────────────────

def send_opening(chat: state_store.ChatState, dry_run: bool) -> None:
    if not chat.is_active:
        logger.info("Skipping opening for inactive lead %s", chat.business["name"])
        return

    logger.info("Opening → %s (%s)", chat.business["name"], chat.jid)
    decision, updated_history = get_decision(chat.business, [], OPENING_TRIGGER)
    _apply_decision(chat, decision, trigger_text=OPENING_TRIGGER, dry_run=dry_run)
    chat.history           = updated_history
    chat.opening_sent      = True
    chat.opening_sent_time = time.time()
    state_store.save_one(chat)


def send_followup(chat: state_store.ChatState, dry_run: bool) -> None:
    if not chat.is_active:
        return

    logger.info("Follow-up → %s (%s)", chat.business["name"], chat.jid)
    decision, updated_history = get_decision(chat.business, chat.history, FOLLOWUP_TRIGGER)
    _apply_decision(chat, decision, trigger_text=FOLLOWUP_TRIGGER, dry_run=dry_run)
    chat.history       = updated_history
    chat.followup_sent = True
    state_store.save_one(chat)


def needs_followup(chat: state_store.ChatState) -> bool:
    """True when a follow-up should be sent: opening done, no reply yet, timer elapsed."""
    if not chat.is_active:
        return False
    if not chat.opening_sent:
        return False
    if chat.followup_sent:
        return False
    if chat.last_inbound_time is not None:
        # They already replied — no follow-up needed
        return False
    secs = state_store.seconds_since_opening(chat)
    if secs is None:
        return False
    return (secs / 3600) >= config.FOLLOWUP_AFTER_HOURS


# ── Core action dispatcher ────────────────────────────────────────────────────

def _apply_decision(
    chat: state_store.ChatState,
    decision,
    trigger_text: str,
    dry_run: bool,
) -> None:
    # ── Money talk ────────────────────────────────────────────────────────────
    if decision.money_talk_detected and not chat.notified_money:
        logger.info("Money talk detected — %s", chat.business["name"])
        chat.notified_money = True
        reporter.money_talk(jid=chat.jid, business=chat.business, dry_run=dry_run)

    # ── Conversion ────────────────────────────────────────────────────────────
    if decision.conversion_detected and not chat.converted:
        logger.info("Conversion detected — %s", chat.business["name"])
        chat.converted = True
        reporter.conversion(
            jid=chat.jid,
            business=chat.business,
            reasoning=decision.reasoning,
            dry_run=dry_run,
        )

    # ── Action ────────────────────────────────────────────────────────────────
    if decision.action == "reply":
        if decision.reply_text:
            send(chat.jid, decision.reply_text, dry_run)
        else:
            logger.warning(
                "LLM chose 'reply' but reply_text is empty for %s — skipping send",
                chat.business["name"],
            )

    elif decision.action == "ignore":
        logger.info("LLM ignoring message from %s", chat.business["name"])

    elif decision.action == "end_conversation":
        logger.info("Ending conversation with %s — %s", chat.business["name"], decision.reasoning)
        chat.ended = True

    elif decision.action == "request_human":
        logger.info("Escalating %s to human — %s", chat.business["name"], decision.reasoning)
        chat.requires_human = True
        reporter.human_needed(
            jid=chat.jid,
            business=chat.business,
            last_message=trigger_text,
            reasoning=decision.reasoning,
            dry_run=dry_run,
        )

    else:
        logger.error("Unknown action %r from LLM — escalating to human", decision.action)
        chat.requires_human = True


# ── Queue logic ───────────────────────────────────────────────────────────────

def ready_to_move_on(chat: state_store.ChatState) -> bool:
    """
    True when we should stop waiting on this lead and open the next one.

    - Inactive (human/ended) → move on immediately
    - They replied  → wait MOVE_ON_REPLIED_HOURS after their last message
    - No reply yet  → wait MOVE_ON_NO_REPLY_HOURS after we sent the opening
                      (must be > FOLLOWUP_AFTER_HOURS so the follow-up fires first)
    """
    if not chat.is_active:
        return True

    if chat.last_inbound_time is not None:
        hours = (time.time() - chat.last_inbound_time) / 3600
        return hours >= config.MOVE_ON_REPLIED_HOURS
    else:
        secs = state_store.seconds_since_opening(chat)
        if secs is None:
            return False
        return (secs / 3600) >= config.MOVE_ON_NO_REPLY_HOURS


# ── Inbound processing ────────────────────────────────────────────────────────

def process_inbound(chat: state_store.ChatState, dry_run: bool) -> None:
    if not chat.is_active:
        logger.debug("process_inbound: chat %s not active", chat.jid)
        return

    try:
        messages = whatsapp.get_messages(chat.jid)
        logger.debug("process_inbound: got %d messages for %s", len(messages), chat.jid)
    except RuntimeError as exc:
        logger.warning("Could not fetch messages for %s: %s", chat.jid, exc)
        return

    if not messages:
        logger.debug("process_inbound: no messages for %s", chat.jid)
        return

    new_messages = []
    for msg in messages:
        msg_id = msg.get("id")
        if msg_id is not None and msg_id == chat.last_seen_id:
            logger.debug("process_inbound: reached last_seen_id %s for %s", chat.last_seen_id, chat.jid)
            break
        new_messages.append(msg)

    if not new_messages:
        logger.debug("process_inbound: no new messages for %s (last_seen_id=%s)", chat.jid, chat.last_seen_id)
        return

    # Always advance the cursor so we don't re-process the same messages
    # Find the first message with a non-None ID to use as cursor
    for msg in messages:
        msg_id = msg.get("id")
        if msg_id is not None:
            chat.last_seen_id = msg_id
            logger.debug("process_inbound: set last_seen_id to %s for %s", msg_id, chat.jid)
            break
    else:
        # No message has an ID - unusual but handle it
        logger.warning("No message with ID found for %s", chat.jid)
        chat.last_seen_id = None

    # Default fromMe to False — if the key is missing, assume it's inbound.
    # wacli always sets fromMe=True explicitly on outbound messages.
    inbound = [m for m in new_messages if not m.get("fromMe", False)]

    logger.debug("process_inbound: %d new messages, %d inbound for %s", len(new_messages), len(inbound), chat.jid)
    # Debug: log IDs of new messages
    if new_messages:
        ids = [msg.get("id") for msg in new_messages]
        logger.debug("process_inbound: new message IDs: %s", ids[:5])  # First 5 IDs

    if not inbound:
        logger.debug("process_inbound: no inbound messages for %s, saving state", chat.jid)
        state_store.save_one(chat)
        return

    logger.info(
        "%d inbound from %s (jid: %s) — waiting %ds...",
        len(inbound), chat.business["name"], chat.jid, config.WAIT_AFTER_REPLY_SECONDS,
    )
    if not dry_run:
        time.sleep(config.WAIT_AFTER_REPLY_SECONDS)

    latest_text = inbound[0].get("text") or inbound[0].get("body") or ""
    chat.last_inbound_time = time.time()

    decision, updated_history = get_decision(chat.business, chat.history, latest_text)
    _apply_decision(chat, decision, trigger_text=latest_text, dry_run=dry_run)
    chat.history = updated_history
    state_store.save_one(chat)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    whatsapp.start_sync()
    logger.info("wacli sync started")

    try:
        while True:
            all_chats = state_store.get_all()
            opened    = [c for c in all_chats if c.opening_sent]
            unopened  = [c for c in all_chats if not c.opening_sent]

            # ── Follow-ups ────────────────────────────────────────────────────
            # Runs across ALL opened chats, not just the current focus lead,
            # because we might have moved on before the follow-up timer fired.
            for chat in opened:
                if needs_followup(chat):
                    try:
                        send_followup(chat, dry_run)
                    except Exception as exc:
                        logger.error("Follow-up failed for %s: %s", chat.jid, exc)

            # ── Advance the opening queue ─────────────────────────────────────
            if unopened:
                if not opened:
                    try:
                        send_opening(unopened[0], dry_run)
                    except Exception as exc:
                        logger.error("Opening failed for %s: %s", unopened[0].jid, exc)

                elif ready_to_move_on(opened[-1]):
                    logger.info(
                        "%s is done — moving on to %s",
                        opened[-1].business["name"], unopened[0].business["name"],
                    )
                    try:
                        send_opening(unopened[0], dry_run)
                    except Exception as exc:
                        logger.error("Opening failed for %s: %s", unopened[0].jid, exc)

                else:
                    last = opened[-1]
                    if last.last_inbound_time:
                        remaining = config.MOVE_ON_REPLIED_HOURS * 3600 - (time.time() - last.last_inbound_time)
                    else:
                        remaining = config.MOVE_ON_NO_REPLY_HOURS * 3600 - (time.time() - (last.opening_sent_time or time.time()))
                    logger.debug(
                        "Waiting on %s — %.0fm until next opening",
                        last.business["name"], remaining / 60,
                    )

            # ── Poll ALL opened chats for inbound replies ─────────────────────
            # Includes chats we've "moved on" from — they can still reply.
            for chat in opened:
                try:
                    process_inbound(chat, dry_run)
                except Exception as exc:
                    logger.error("Inbound error for %s: %s", chat.jid, exc)

            time.sleep(config.POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
    finally:
        whatsapp.stop_sync()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()

    if args.csv:
        config.LEADS_CSV = args.csv

    if args.dry_run:
        logger.info("DRY-RUN mode")

    leads = load_leads()
    if not leads:
        logger.error("No leads found.")
        sys.exit(1)

    logger.info("Loaded %d lead(s)", len(leads))
    state_store.load(leads)
    run(dry_run=args.dry_run)