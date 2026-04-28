import csv
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import ai
import state as state_manager
from state import TERMINAL_STATUSES
import whatsapp
from config import (
    BATCH_INTER_LEAD_DELAY_MIN,
    BATCH_INTER_LEAD_DELAY_MAX,
    CSV_PATH,
    POLL_INTERVAL_SECONDS,
    WAIT_AFTER_REPLY_SECONDS,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Keep these at INFO so you can see what's actually happening
for name in ("__main__",):
    logging.getLogger(name).setLevel(logging.INFO)

SKIP_STATUSES = TERMINAL_STATUSES | {"paused"}


# ── CSV loading ───────────────────────────────────────────────────────────────

def normalize_phone(raw: str) -> str:
    phone = raw.strip().lstrip("+")
    if phone.startswith("0"):
        phone = "92" + phone[1:]
    return phone


def load_leads(path: str) -> list[dict]:
    leads = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            facebook   = row.get("facebook") or row.get("facebook_url") or ""
            active_ads = row.get("active_ads", "0") or "0"
            running_ads = int(float(active_ads)) > 0

            score = 0
            if row.get("website", "").strip(): score += 40
            if facebook.strip():               score += 30
            if running_ads:                    score += 30
            completion_score = int(row.get("completion_score") or score)

            leads.append({
                "phone": normalize_phone(row["phone"]),
                "business": {
                    "name":             row["name"].strip(),
                    "facebook":         facebook.strip(),
                    "website":          row.get("website", "").strip(),
                    "running_ads":      running_ads,
                    "completion_score": completion_score,
                },
            })
    return leads


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


# ── Per-lead processing ───────────────────────────────────────────────────────

def process_lead(state: dict) -> None:
    jid               = state["jid"]
    last_processed_at = state.get("last_processed_at")
    is_human_needed   = state.get("status") == "human_needed"

    try:
        messages = whatsapp.get_messages(jid, limit=20)
    except RuntimeError as exc:
        logger.error("[%s] Failed to fetch messages: %s", state["phone"], exc)
        return

    if not messages:
        return

    if last_processed_at:
        latest_ts = _parse_ts(messages[0]["Timestamp"])
        cursor_ts = _parse_ts(last_processed_at)
        if latest_ts <= cursor_ts:
            return

    if not messages[0].get("FromMe", False):
        time.sleep(WAIT_AFTER_REPLY_SECONDS)
        try:
            messages = whatsapp.get_messages(jid, limit=20)
        except RuntimeError as exc:
            logger.error("[%s] Failed to re-fetch messages: %s", state["phone"], exc)
            return

    new_messages = []
    for msg in messages:
        if last_processed_at:
            ts = _parse_ts(msg["Timestamp"])
            if ts <= _parse_ts(last_processed_at):
                break
        text       = msg.get("Text") or msg.get("DisplayText") or ""
        media_type = msg.get("MediaType") or ""
        new_messages.append((_parse_ts(msg["Timestamp"]), msg.get("FromMe", False), text, media_type))

    if not new_messages:
        return

    new_messages.sort(key=lambda x: x[0])
    latest_ts = new_messages[-1][0]

    for ts, from_me, text, media_type in new_messages:
        if from_me:
            if text:
                state["llm_history"].append({"role": "assistant", "content": text})
        else:
            content = text if text else (f"[{media_type} message]" if media_type else "")
            if content:
                state["llm_history"].append({"role": "user", "content": content})

    if is_human_needed:
        state["last_processed_at"] = latest_ts.isoformat()
        state_manager.save(state)
        return

    new_inbound = [(ts, text, mt) for ts, from_me, text, mt in new_messages if not from_me]

    if not new_inbound:
        state["last_processed_at"] = latest_ts.isoformat()
        state_manager.save(state)
        return

    parts = []
    for _, text, media_type in new_inbound:
        if text:
            parts.append(text)
        elif media_type:
            parts.append(f"[{media_type} message]")

    combined_text = " ".join(parts).strip()
    if not combined_text:
        state["last_processed_at"] = latest_ts.isoformat()
        state_manager.save(state)
        return

    logger.info("[%s] They said: %s", state["phone"], combined_text)

    try:
        result = ai.run_turn(
            jid=jid,
            history=state["llm_history"],
            business=state["business"],
            user_message=combined_text,
        )
    except RuntimeError as exc:
        logger.error("[%s] AI turn failed: %s", state["phone"], exc)
        return

    state["llm_history"] = result["history"]
    state["last_processed_at"] = datetime.now(timezone.utc).isoformat()

    if result["owner_notified"]:
        state["status"] = "human_needed"

    state_manager.save(state)

    if result["messages_sent"]:
        logger.info("[%s] Sent: %s", state["phone"], " | ".join(result["messages_sent"]))


# ── Opening message ───────────────────────────────────────────────────────────

def send_opening(state: dict) -> None:
    try:
        result = ai.run_turn(
            jid=state["jid"],
            history=[],
            business=state["business"],
            user_message=None,
        )
    except RuntimeError as exc:
        logger.error("[%s] Opening failed: %s", state["phone"], exc)
        return

    state_manager.mark_opened(state)
    state["llm_history"] = result["history"]
    state_manager.save(state)
    logger.info("[%s] Opening sent", state["phone"])


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    whatsapp.start_sync()
    time.sleep(10)  # let wacli sync before we start

    leads = load_leads(CSV_PATH)
    logger.info("Loaded %d lead(s)", len(leads))

    all_states: list[dict] = []

    for lead in leads:
        phone    = lead["phone"]
        business = lead["business"]
        existing = state_manager.load(phone)

        if existing is None:
            state = state_manager.create(phone, business)
            all_states.append(("pending", state))
        else:
            all_states.append((existing.get("status", "active"), existing))

    pending = [s for status, s in all_states if status == "pending"]
    active  = [s for status, s in all_states if status not in ("pending",)]

    logger.info("%d pending, %d resuming", len(pending), len(active))

    # Send openings with random delays between each
    for i, state in enumerate(pending):
        send_opening(state)
        active.append(state)
        if i < len(pending) - 1:
            delay = random.randint(BATCH_INTER_LEAD_DELAY_MIN, BATCH_INTER_LEAD_DELAY_MAX)
            logger.info("Waiting %ds before next opening...", delay)
            time.sleep(delay)

    logger.info("All openings sent, entering poll loop")

    while True:
        for state in active:
            if state.get("status") in SKIP_STATUSES:
                continue
            try:
                process_lead(state)
            except Exception as exc:
                logger.exception("[%s] Unexpected error: %s", state["phone"], exc)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        whatsapp.stop_sync()
    except Exception:
        logger.exception("Fatal error")
        whatsapp.stop_sync()
        raise