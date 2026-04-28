import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import STATE_DIR

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"ghosted", "converted", "not_interested"}


def _path(phone: str) -> Path:
    d = Path(STATE_DIR)
    d.mkdir(exist_ok=True)
    return d / f"{phone}.json"


def load(phone: str) -> dict | None:
    p = _path(phone)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def save(state: dict) -> None:
    p = _path(state["phone"])
    with open(p, "w") as f:
        json.dump(state, f, indent=2, default=str)


def create(phone: str, business: dict) -> dict:
    """Create a fresh state file for a new lead."""
    state = {
        "phone": phone,
        "jid": f"{phone}@s.whatsapp.net",
        "business": business,
        "status": "pending",
        "campaign_start_at": None,
        "last_processed_at": None,
        "llm_history": [],
    }
    save(state)
    return state


def mark_opened(state: dict) -> dict:
    """
    Called immediately after sending the opening message.
    Sets campaign_start_at and last_processed_at to right now (local Python clock).
    """
    now = datetime.now(timezone.utc).isoformat()
    state["campaign_start_at"] = now
    state["last_processed_at"] = now
    state["status"] = "active"
    save(state)
    return state


def mark_terminal(state: dict, status: str) -> dict:
    """
    Mark a lead as terminal (ghosted, converted, or not_interested).
    Terminal leads are skipped by the poll loop and free up no slot —
    they just stop being processed.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"Unknown terminal status: {status!r}")
    state["status"] = status
    state["terminal_at"] = datetime.now(timezone.utc).isoformat()
    save(state)
    logger.info("[%s] Marked as %s", state["phone"], status)
    return state


def mark_paused(state: dict) -> dict:
    """Pause a lead — the poll loop will skip it but it is not terminal."""
    state["status"] = "paused"
    save(state)
    logger.info("[%s] Paused", state["phone"])
    return state


def mark_resumed(state: dict) -> dict:
    """Resume a paused lead back to active."""
    state["status"] = "active"
    save(state)
    logger.info("[%s] Resumed", state["phone"])
    return state