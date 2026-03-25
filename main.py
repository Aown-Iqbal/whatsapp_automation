"""
main.py — CSV-driven sequential WhatsApp outreach.

CSV required columns:
    name             Business name
    owner_phone      WhatsApp JID  e.g. 923001234567@s.whatsapp.net
    facebook         Facebook page URL
    website          Website URL (can be empty)
    running_ads      true / false  (or 1 / 0)
    completion_score Integer 0-100

Optional column (added automatically on first run):
    status           pending | done | no_reply | error
"""

import csv
import logging
import os
import time
from pathlib import Path

import ai
import whatsapp
from config import (
    CSV_PATH,
    POLL_INTERVAL_SECONDS,
    REPLY_TIMEOUT_SECONDS,
    WAIT_AFTER_REPLY_SECONDS,
)
from prompt import build_system_prompt

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_csv(path: str) -> tuple[list[dict], list[str]]:
    """
    Load the CSV and return (rows, fieldnames).
    Adds a 'status' column defaulting to 'pending' if it doesn't exist.
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if "status" not in fieldnames:
        fieldnames.append("status")
        for row in rows:
            row.setdefault("status", "pending")

    return rows, fieldnames


def save_csv(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    """Write rows back to the CSV, preserving column order."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_business(row: dict) -> dict:
    """Normalise a CSV row into the business dict expected by build_system_prompt."""
    running_ads_raw = str(row.get("running_ads", "false")).strip().lower()
    return {
        "name":             row.get("name", "").strip(),
        "owner_phone":      row.get("owner_phone", "").strip(),
        "facebook":         row.get("facebook", "").strip(),
        "website":          row.get("website", "").strip(),
        "running_ads":      running_ads_raw in ("true", "1", "yes"),
        "completion_score": int(row.get("completion_score", 0) or 0),
    }


# ── Message helpers ───────────────────────────────────────────────────────────

def collect_their_messages(messages: list[dict], since_id: str | None) -> str:
    """
    Walk the message list (newest-first) and return all consecutive inbound
    messages that arrived after since_id as one combined string.
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
    parts = whatsapp.send_message(jid, reply)
    ai.add_assistant_messages(parts)


# ── Single-contact conversation ───────────────────────────────────────────────

def run_conversation(business: dict) -> str:
    """
    Conduct a full conversation with one contact.

    Returns a status string:
        'done'     — conversation ended naturally (inactivity timeout after at
                     least one exchange)
        'no_reply' — they never replied to the opening message
        'error'    — an unrecoverable error occurred
    """
    jid = business["owner_phone"]
    logger.info("━━━ Starting conversation with %s (%s) ━━━", business["name"], jid)

    # Build a fresh prompt + history for this contact
    system_prompt = build_system_prompt(business)
    ai.new_conversation(system_prompt)

    # ── Send opening ──────────────────────────────────────────────────────────
    try:
        opening = ai.get_reply("generate the opening message")
        send_and_record(jid, opening)
        logger.info("Opening sent.")
    except RuntimeError as exc:
        logger.error("Failed to send opening: %s", exc)
        return "error"

    # Snapshot the last message id right after we send
    try:
        messages = whatsapp.get_messages(jid)
        last_seen_id: str | None = messages[0]["MsgID"] if messages else None
    except RuntimeError as exc:
        logger.error("Could not fetch initial messages: %s", exc)
        return "error"

    got_first_reply = False
    idle_since = time.monotonic()   # reset every time we send something

    # ── Poll loop for this contact ────────────────────────────────────────────
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)

        # Check inactivity timeout
        if time.monotonic() - idle_since > REPLY_TIMEOUT_SECONDS:
            if got_first_reply:
                logger.info(
                    "No reply for %ds after last message, moving to next contact.",
                    REPLY_TIMEOUT_SECONDS,
                )
                return "done"
            else:
                logger.info(
                    "No reply to opening after %ds, marking as no_reply.",
                    REPLY_TIMEOUT_SECONDS,
                )
                return "no_reply"

        try:
            messages = whatsapp.get_messages(jid)
        except RuntimeError as exc:
            logger.error("Failed to fetch messages: %s", exc)
            continue

        if not messages:
            continue

        latest = messages[0]

        # Nothing new
        if latest["FromMe"] or latest["MsgID"] == last_seen_id:
            continue

        # New inbound message — wait in case they're still typing
        logger.info("New message detected, waiting %ds…", WAIT_AFTER_REPLY_SECONDS)
        time.sleep(WAIT_AFTER_REPLY_SECONDS)

        try:
            messages = whatsapp.get_messages(jid)
        except RuntimeError as exc:
            logger.error("Failed to re-fetch messages: %s", exc)
            continue

        combined_text = collect_their_messages(messages, last_seen_id)
        if not combined_text:
            logger.warning("Could not extract text, skipping.")
            continue

        last_seen_id = messages[0]["MsgID"]
        got_first_reply = True
        logger.info("They said: %s", combined_text)

        try:
            reply = ai.get_reply(combined_text)
        except RuntimeError as exc:
            logger.error("AI call failed: %s", exc)
            continue

        try:
            send_and_record(jid, reply)
            idle_since = time.monotonic()   # reset timeout after every reply we send
        except RuntimeError as exc:
            logger.error("Failed to send reply: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    csv_path = CSV_PATH

    if not Path(csv_path).exists():
        logger.error("CSV file not found: %s", csv_path)
        raise SystemExit(1)

    rows, fieldnames = load_csv(csv_path)
    total   = len(rows)
    pending = [r for r in rows if r.get("status", "pending") == "pending"]
    logger.info("Loaded %d contacts, %d pending.", total, len(pending))

    whatsapp.start_sync()
    logger.info("wacli sync started.")

    for i, row in enumerate(rows):
        if row.get("status", "pending") != "pending":
            logger.info(
                "Skipping %s (status=%s)", row.get("name", "?"), row.get("status")
            )
            continue

        business = parse_business(row)

        if not business["owner_phone"]:
            logger.warning("Row %d has no owner_phone, skipping.", i)
            row["status"] = "error"
            save_csv(csv_path, rows, fieldnames)
            continue

        status = run_conversation(business)
        row["status"] = status
        save_csv(csv_path, rows, fieldnames)   # persist after every contact

        logger.info(
            "Finished %s → status=%s  (%d/%d done)",
            business["name"], status, i + 1, total,
        )

    logger.info("All contacts processed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted — stopping sync.")
        whatsapp.stop_sync()
    except Exception:
        logger.exception("Fatal error")
        whatsapp.stop_sync()
        raise