import csv
import logging
import time
from datetime import datetime, timezone

import ai
import state as state_manager
import whatsapp
from config import (
    CSV_PATH,
    FOLLOWUP_WAIT_HOURS,
    MAX_FOLLOWUPS,
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


# ── CSV loading ───────────────────────────────────────────────────────────────

def load_leads(path: str) -> list[dict]:
    """
    Load leads from CSV. Expected columns:
        phone, name, facebook, website, running_ads, completion_score

    Example row:
        923001234567,Ali's Electronics,https://facebook.com/aliselectronics,aliselectronics.com,false,72
    """
    leads = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            leads.append({
                "phone": row["phone"].strip(),
                "business": {
                    "name":             row["name"].strip(),
                    "facebook":         row.get("facebook", "").strip(),
                    "website":          row.get("website", "").strip(),
                    "running_ads":      row.get("running_ads", "false").strip().lower() == "true",
                    "completion_score": int(row.get("completion_score", 0)),
                },
            })
    return leads


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> datetime:
    """Parse wacli's ISO 8601 UTC timestamp string into a timezone-aware datetime."""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

# ── Follow-up scheduler ───────────────────────────────────────────────────────

def _maybe_send_followup(state: dict) -> None:
    """
    Called when the poll loop finds nothing new for a lead.
    Sends an AI-generated follow-up if:
      - lead is still active (not human_needed or converted)
      - follow-up cap hasn't been reached
      - enough time has passed since last_inbound_at
    """
    if state.get("status") != "active":
        return

    followup_count = state.get("followup_count", 0)
    if followup_count >= MAX_FOLLOWUPS:
        return

    # Use last_inbound_at as the silence clock; fall back to campaign_start_at
    # for leads who never replied at all (last_inbound_at set to campaign open time).
    reference_ts = state.get("last_inbound_at") or state.get("campaign_start_at")
    if not reference_ts:
        return

    elapsed_hours = (
        datetime.now(timezone.utc) - _parse_ts(reference_ts)
    ).total_seconds() / 3600

    if elapsed_hours < FOLLOWUP_WAIT_HOURS:
        return

    logger.info(
        "[%s] Follow-up #%d due (%.1fh of silence)",
        state["phone"], followup_count + 1, elapsed_hours,
    )

    # Transient trigger — not persisted to history as a user message.
    # Tells the AI to try a fresh angle without repeating itself.
    trigger = (
        "Lead ne abhi tak reply nahi di. "
        "Ek gentle follow-up bhejo — naya angle try karo, "
        "same cheez repeat mat karo."
    )

    try:
        result = ai.run_turn(
            jid=state["jid"],
            history=state["llm_history"],
            business=state["business"],
            user_message=trigger,
        )
    except RuntimeError as exc:
        logger.error("[%s] Follow-up AI turn failed: %s", state["phone"], exc)
        return

    state["llm_history"]      = result["history"]
    state["followup_count"]   = followup_count + 1
    state["last_processed_at"] = datetime.now(timezone.utc).isoformat()

    if result["owner_notified"]:
        state["status"] = "human_needed"

    state_manager.save(state)

    if result["messages_sent"]:
        logger.info(
            "[%s] Follow-up #%d sent (%d message(s))",
            state["phone"], state["followup_count"], len(result["messages_sent"]),
        )
    else:
        logger.warning("[%s] Follow-up AI turn produced no messages", state["phone"])


# ── Per-lead processing ───────────────────────────────────────────────────────

def process_lead(state: dict) -> None:
    """
    Check a single lead for new messages (inbound or manual outbound) and act.

    - human_needed leads: still sync history so the AI has full context when
      control is handed back — just don't trigger the AI to reply.
    - active leads: sync manual outbound messages into history, then trigger
      the AI if there are new inbound messages.
    """
    jid = state["jid"]
    last_processed_at = state.get("last_processed_at")
    is_human_needed   = state.get("status") == "human_needed"

    # ── Fetch recent messages for this chat ───────────────────────────────────
    try:
        messages = whatsapp.get_messages(jid, limit=20)
    except RuntimeError as exc:
        logger.error("[%s] Failed to fetch messages: %s", state["phone"], exc)
        return

    if not messages:
        return

    # ── Quick check: anything newer than our cursor at all? ──────────────────
    if last_processed_at:
        latest_ts = _parse_ts(messages[0]["Timestamp"])
        cursor_ts = _parse_ts(last_processed_at)
        logger.info(
            "[%s] Latest msg: %s | Cursor: %s | FromMe: %s",
            state["phone"], latest_ts.isoformat(), cursor_ts.isoformat(), messages[0]["FromMe"],
        )
        if latest_ts <= cursor_ts:
            # Nothing new — check if a follow-up is due instead
            _maybe_send_followup(state)
            return

    # ── If the newest message is inbound, wait for them to finish typing ──────
    if not messages[0]["FromMe"]:
        logger.info("[%s] New message detected, waiting %ds...", state["phone"], WAIT_AFTER_REPLY_SECONDS)
        time.sleep(WAIT_AFTER_REPLY_SECONDS)

        try:
            messages = whatsapp.get_messages(jid, limit=20)
        except RuntimeError as exc:
            logger.error("[%s] Failed to re-fetch messages: %s", state["phone"], exc)
            return

    # ── Collect ALL new messages since the cursor (inbound + manual outbound) ─
    new_messages: list[tuple[datetime, bool, str, str]] = []

    for msg in messages:  # newest first
        if last_processed_at:
            ts = _parse_ts(msg["Timestamp"])
            if ts <= _parse_ts(last_processed_at):
                break

        text       = msg.get("Text") or msg.get("DisplayText") or ""
        media_type = msg.get("MediaType") or ""
        new_messages.append((_parse_ts(msg["Timestamp"]), msg["FromMe"], text, media_type))

    if not new_messages:
        return

    # Sort chronologically (wacli returns newest first)
    new_messages.sort(key=lambda x: x[0])
    latest_ts = new_messages[-1][0]

    # ── Separate inbound from manual outbound ─────────────────────────────────
    new_inbound         = [(ts, text, mt) for ts, from_me, text, mt in new_messages if not from_me]
    new_manual_outbound = [(ts, text)     for ts, from_me, text, mt in new_messages if from_me]

    # ── Track when the lead last spoke ───────────────────────────────────────
    # This is what the follow-up timer runs against — not last_processed_at,
    # which moves every time the AI sends something.
    if new_inbound:
        state["last_inbound_at"] = new_inbound[-1][0].isoformat()

    # ── Sync ALL new messages into history in chronological order ────────────
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

    # ── human_needed: sync only, no AI trigger ────────────────────────────────
    if is_human_needed:
        if new_messages:
            state["last_processed_at"] = latest_ts.isoformat()
            state_manager.save(state)
        return

    # ── Nothing inbound to reply to ───────────────────────────────────────────
    if not new_inbound:
        if new_messages:
            state["last_processed_at"] = latest_ts.isoformat()
            state_manager.save(state)
        return

    # ── Build combined inbound text for the AI ────────────────────────────────
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

    # ── Run AI turn ───────────────────────────────────────────────────────────
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

    # ── Update and persist state ──────────────────────────────────────────────
    state["llm_history"] = result["history"]

    # Cursor set to NOW so the AI's own outbound messages are behind it on
    # the next poll and never re-synced as manual messages.
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
    """Send the opening message to a new lead and initialise their state cursor."""
    logger.info("[%s] Sending opening message...", state["phone"])

    try:
        result = ai.run_turn(
            jid=state["jid"],
            history=[],
            business=state["business"],
            user_message=None,  # signals opening turn
        )
    except RuntimeError as exc:
        logger.error("[%s] Failed to generate opening: %s", state["phone"], exc)
        return

    # Cursor set to right now — safe, no wacli fetch, no race condition.
    # mark_opened also sets last_inbound_at to now so the follow-up clock
    # starts from the moment we first reached out.
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

    # ── Initialise new leads and send openings ────────────────────────────────
    active_states: list[dict] = []

    for lead in leads:
        phone    = lead["phone"]
        business = lead["business"]

        existing = state_manager.load(phone)

        if existing is None:
            state = state_manager.create(phone, business)
            send_opening(state)
            active_states.append(state)
        else:
            active_states.append(existing)
            logger.info("[%s] Resuming existing conversation (status: %s)", phone, existing.get("status"))

    if not active_states:
        logger.warning("No leads to process. Check %s", CSV_PATH)
        return

    # ── Poll loop ─────────────────────────────────────────────────────────────
    logger.info("Starting poll loop for %d lead(s)...", len(active_states))
    while True:
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