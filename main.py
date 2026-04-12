import csv
import logging
import time
from datetime import datetime, timedelta, timezone

import ai
import state as state_manager
import whatsapp
from config import (
    BATCH_HOURS,
    BATCH_SIZE,
    CSV_PATH,
    LOCAL_TZ_OFFSET_HOURS,
    POLL_INTERVAL_SECONDS,
    WAIT_AFTER_REPLY_SECONDS,
)

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

LOCAL_TZ = timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS))


# ── CSV loading ───────────────────────────────────────────────────────────────

def normalize_phone(raw: str) -> str:
    """Convert local Pakistani format to international: 03001234567 -> 923001234567"""
    phone = raw.strip().lstrip("+")
    if phone.startswith("0"):
        phone = "92" + phone[1:]
    return phone


def load_leads(path: str) -> list[dict]:
    leads = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            # Support both scraper output and manually prepared CSVs
            facebook = row.get("facebook") or row.get("facebook_url") or ""

            total_ads  = row.get("total_ads", "0") or "0"
            active_ads = row.get("active_ads", "0") or "0"
            running_ads = int(float(active_ads)) > 0

            # Rough presence score from available signals
            score = 0
            if row.get("website", "").strip():  score += 40
            if facebook.strip():                score += 30
            if running_ads:                     score += 30
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


def _now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


# ── Batch scheduling ──────────────────────────────────────────────────────────

def _current_batch_window(now: datetime) -> datetime | None:
    """
    Return the start of the active batch window (local time) if we are currently
    in a batch hour, otherwise return None.

    A batch window is active for the entire hour it starts in. E.g. the 9am
    window is active from 09:00 to 09:59.
    """
    if now.hour in BATCH_HOURS:
        return now.replace(minute=0, second=0, microsecond=0)
    return None


def _openings_in_window(all_states: list[dict], window_start: datetime) -> int:
    """
    Count how many leads were opened during the given batch window (same date,
    same hour, in local time).
    """
    count = 0
    for state in all_states:
        ts_str = state.get("campaign_start_at")
        if not ts_str:
            continue
        ts_local = _parse_ts(ts_str).astimezone(LOCAL_TZ)
        if ts_local.date() == window_start.date() and ts_local.hour == window_start.hour:
            count += 1
    return count


# ── Per-lead processing ───────────────────────────────────────────────────────

def process_lead(state: dict) -> None:
    jid = state["jid"]
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
        logger.info(
            "[%s] Latest msg: %s | Cursor: %s | FromMe: %s",
            state["phone"], latest_ts.isoformat(), cursor_ts.isoformat(), messages[0]["FromMe"],
        )
        if latest_ts <= cursor_ts:
            return

    if not messages[0]["FromMe"]:
        logger.info("[%s] New message detected, waiting %ds...", state["phone"], WAIT_AFTER_REPLY_SECONDS)
        time.sleep(WAIT_AFTER_REPLY_SECONDS)
        try:
            messages = whatsapp.get_messages(jid, limit=20)
        except RuntimeError as exc:
            logger.error("[%s] Failed to re-fetch messages: %s", state["phone"], exc)
            return

    new_messages: list[tuple[datetime, bool, str, str]] = []
    for msg in messages:
        if last_processed_at:
            ts = _parse_ts(msg["Timestamp"])
            if ts <= _parse_ts(last_processed_at):
                break
        text       = msg.get("Text") or msg.get("DisplayText") or ""
        media_type = msg.get("MediaType") or ""
        new_messages.append((_parse_ts(msg["Timestamp"]), msg["FromMe"], text, media_type))

    if not new_messages:
        return

    new_messages.sort(key=lambda x: x[0])
    latest_ts = new_messages[-1][0]

    new_inbound         = [(ts, text, mt) for ts, from_me, text, mt in new_messages if not from_me]
    new_manual_outbound = [(ts, text)     for ts, from_me, text, mt in new_messages if from_me]

    for ts, from_me, text, media_type in new_messages:
        if from_me:
            if text:
                state["llm_history"].append({"role": "assistant", "content": text})
                logger.info("[%s] Human sent (synced to history): %s", state["phone"], text)
        else:
            content = text if text else (f"[{media_type} message]" if media_type else "")
            if content:
                state["llm_history"].append({"role": "user", "content": content})
                logger.info("[%s] Lead said (synced to history): %s", state["phone"], content)

    if is_human_needed:
        if new_messages:
            state["last_processed_at"] = latest_ts.isoformat()
            state_manager.save(state)
        return

    if not new_inbound:
        if new_messages:
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
        logger.warning("[%s] New inbound messages had no extractable content", state["phone"])
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
        logger.info("[%s] Sent %d message(s)", state["phone"], len(result["messages_sent"]))
    else:
        logger.info("[%s] AI chose not to reply", state["phone"])


# ── Opening message ───────────────────────────────────────────────────────────

def send_opening(state: dict) -> None:
    logger.info("[%s] Sending opening message...", state["phone"])
    try:
        result = ai.run_turn(
            jid=state["jid"],
            history=[],
            business=state["business"],
            user_message=None,
        )
    except RuntimeError as exc:
        logger.error("[%s] Failed to generate opening: %s", state["phone"], exc)
        return

    state_manager.mark_opened(state)
    state["llm_history"] = result["history"]
    state_manager.save(state)
    logger.info("[%s] Opening sent", state["phone"])


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    whatsapp.start_sync()
    logger.info("Sync started")

    leads = load_leads(CSV_PATH)
    logger.info("Loaded %d lead(s) from %s", len(leads), CSV_PATH)

    # Separate leads into already-known and brand-new (pending opening)
    active_states: list[dict] = []
    pending_leads: list[dict] = []   # new leads waiting for their batch slot

    for lead in leads:
        phone    = lead["phone"]
        business = lead["business"]
        existing = state_manager.load(phone)

        if existing is None:
            # Create state in pending status — no opening sent yet
            state = state_manager.create(phone, business)
            pending_leads.append(state)
            logger.info("[%s] Queued as pending", phone)
        else:
            active_states.append(existing)
            logger.info("[%s] Resuming (status: %s)", phone, existing.get("status"))

    if not active_states and not pending_leads:
        logger.warning("No leads to process. Check %s", CSV_PATH)
        return

    logger.info(
        "Starting poll loop — %d active, %d pending",
        len(active_states), len(pending_leads),
    )

    while True:
        now = _now_local()
        window = _current_batch_window(now)

        # ── Open pending leads if we are in a batch window with capacity ──────
        if window and pending_leads:
            already_sent = _openings_in_window(active_states, window)
            capacity     = BATCH_SIZE - already_sent

            if capacity > 0:
                to_open      = pending_leads[:capacity]
                pending_leads = pending_leads[capacity:]

                for state in to_open:
                    send_opening(state)
                    active_states.append(state)

                if to_open:
                    logger.info(
                        "Batch %02d:00 — opened %d lead(s), %d still pending",
                        window.hour, len(to_open), len(pending_leads),
                    )

        # ── Poll active leads ─────────────────────────────────────────────────
        for state in active_states:
            try:
                process_lead(state)
            except Exception as exc:
                logger.exception("[%s] Unexpected error: %s", state["phone"], exc)

        time.sleep(POLL_INTERVAL_SECONDS)


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