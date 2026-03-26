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
import state as state_store
import whatsapp
from ai import get_reply, contains_money_talk
from leads import load_leads

logging.basicConfig(
    level=logging.INFO,
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


def notify_owner(jid: str, business_name: str, dry_run: bool) -> None:
    send(config.NOTIFY_JID, f"[Money talk] {business_name} ({jid})", dry_run)


# ── Opening / follow-up ───────────────────────────────────────────────────────

OPENING_TRIGGER  = "Start the conversation now. Send the very first greeting message to the business owner."
FOLLOWUP_TRIGGER = "The person hasn't replied in a while. Send a short, polite follow-up message."


def send_opening(chat: state_store.ChatState, dry_run: bool) -> None:
    logger.info("Opening → %s (%s)", chat.business["name"], chat.jid)
    reply, updated_history = get_reply(chat.business, [], OPENING_TRIGGER)
    send(chat.jid, reply, dry_run)
    chat.history          = updated_history
    chat.opening_sent     = True
    chat.opening_sent_time = time.time()
    state_store.save_one(chat)


def send_followup(chat: state_store.ChatState, dry_run: bool) -> None:
    logger.info("Follow-up → %s (%s)", chat.business["name"], chat.jid)
    reply, updated_history = get_reply(chat.business, chat.history, FOLLOWUP_TRIGGER)
    send(chat.jid, reply, dry_run)
    chat.history       = updated_history
    chat.followup_sent = True
    state_store.save_one(chat)


# ── Queue logic ───────────────────────────────────────────────────────────────

def ready_to_move_on(chat: state_store.ChatState) -> bool:
    """
    True when we should stop waiting on this lead and open the next one.

    - They replied  → wait MOVE_ON_REPLIED_HOURS after their last message
    - No reply yet  → wait MOVE_ON_NO_REPLY_HOURS after we sent the opening
    """
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
    try:
        messages = whatsapp.get_messages(chat.jid)
    except RuntimeError as exc:
        logger.warning("Could not fetch messages for %s: %s", chat.jid, exc)
        return

    if not messages:
        return

    new_messages = []
    for msg in messages:
        if msg.get("id") == chat.last_seen_id:
            break
        new_messages.append(msg)

    if not new_messages:
        return

    chat.last_seen_id = messages[0].get("id")
    inbound = [m for m in new_messages if not m.get("fromMe", True)]

    if not inbound:
        state_store.save_one(chat)
        return

    logger.info(
        "%d inbound from %s — waiting %ds...",
        len(inbound), chat.business["name"], config.WAIT_AFTER_REPLY_SECONDS,
    )
    if not dry_run:
        time.sleep(config.WAIT_AFTER_REPLY_SECONDS)

    latest_text = inbound[0].get("text") or inbound[0].get("body") or ""
    chat.last_inbound_time = time.time()

    if not chat.notified_money and contains_money_talk(latest_text):
        logger.info("Money talk — %s", chat.business["name"])
        notify_owner(chat.jid, chat.business["name"], dry_run)
        chat.notified_money = True

    reply, updated_history = get_reply(chat.business, chat.history, latest_text)
    send(chat.jid, reply, dry_run)
    chat.history = updated_history
    state_store.save_one(chat)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    whatsapp.start_sync()
    logger.info("wacli sync started")

    try:
        while True:
            all_chats = state_store.get_all()

            # ── Advance the opening queue ─────────────────────────────────────
            # Find the latest chat we've opened. If it's ready to move on,
            # open the next one. Only one un-opened lead gets unlocked per cycle.
            opened   = [c for c in all_chats if c.opening_sent]
            unopened = [c for c in all_chats if not c.opening_sent]

            if unopened:
                # No openings sent yet — fire the first one immediately
                if not opened:
                    try:
                        send_opening(unopened[0], dry_run)
                    except Exception as exc:
                        logger.error("Opening failed for %s: %s", unopened[0].jid, exc)

                # Last opened lead is ready to move on — open the next
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
                    # Log how long until we move on
                    last = opened[-1]
                    if last.last_inbound_time:
                        remaining = config.MOVE_ON_REPLIED_HOURS * 3600 - (time.time() - last.last_inbound_time)
                    else:
                        remaining = config.MOVE_ON_NO_REPLY_HOURS * 3600 - (time.time() - (last.opening_sent_time or time.time()))
                    logger.debug(
                        "Waiting on %s — %.0fm until next opening",
                        last.business["name"], remaining / 60,
                    )

            # ── Poll all opened chats for replies ─────────────────────────────
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