import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import STATE_DIR

logger = logging.getLogger(__name__)


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
    No wacli fetch needed — no race condition possible.
    """
    now = datetime.now(timezone.utc).isoformat()
    state["campaign_start_at"] = now
    state["last_processed_at"] = now
    state["status"] = "active"
    save(state)
    return state