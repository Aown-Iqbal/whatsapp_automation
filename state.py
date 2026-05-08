import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_DIR = os.environ.get("STATE_DIR", "states")

TERMINAL_STATUSES = {"ghosted", "converted", "not_interested"}


def _path(phone: str) -> Path:
    d = Path(STATE_DIR)
    d.mkdir(exist_ok=True)
    return d / f"{phone}.json"


# ── Single-chat CRUD ─────────────────────────────────────────────────────────

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


def create(phone: str, business: dict, campaign_id: str = "") -> dict:
    """Create a fresh state file for a new lead."""
    state = {
        "phone": phone,
        "jid": f"{phone}@s.whatsapp.net",
        "business": business,
        "status": "pending",
        "campaign_id": campaign_id,
        "campaign_start_at": None,
        "last_processed_at": None,
        "session_count": 0,
        "llm_history": [],
    }
    save(state)
    return state


def mark_opened(state: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    state["campaign_start_at"] = now
    state["last_processed_at"] = now
    state["status"] = "active"
    state["session_count"] = state.get("session_count", 0) + 1
    save(state)
    return state


def mark_terminal(state: dict, status: str) -> dict:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"Unknown terminal status: {status!r}")
    state["status"] = status
    state["terminal_at"] = datetime.now(timezone.utc).isoformat()
    save(state)
    logger.info("[%s] Marked as %s", state["phone"], status)
    return state


def mark_paused(state: dict) -> dict:
    state["status"] = "paused"
    save(state)
    logger.info("[%s] Paused", state["phone"])
    return state


def mark_resumed(state: dict) -> dict:
    state["status"] = "active"
    save(state)
    logger.info("[%s] Resumed", state["phone"])
    return state


# ── Bulk queries ─────────────────────────────────────────────────────────────

def _load_all() -> list[dict]:
    """Load all state files from disk."""
    d = Path(STATE_DIR)
    if not d.exists():
        return []
    states = []
    for f in d.glob("*.json"):
        try:
            with open(f) as fh:
                states.append(json.load(fh))
        except (json.JSONDecodeError, KeyError):
            logger.warning("Skipping corrupt state file: %s", f.name)
    return states


def list_by_status(statuses: set[str]) -> list[dict]:
    """Return all chats whose status is in the given set."""
    return [s for s in _load_all() if s.get("status") in statuses]


def list_active() -> list[dict]:
    """Return all chats with status 'active'."""
    return list_by_status({"active"})


def list_pending() -> list[dict]:
    """Return all chats with status 'pending' (not yet contacted)."""
    return list_by_status({"pending"})


def count_by_status(campaign_id: str = "") -> dict[str, int]:
    """Return counts of chats by status, optionally filtered by campaign."""
    counts: dict[str, int] = {}
    for s in _load_all():
        if campaign_id and s.get("campaign_id") != campaign_id:
            continue
        status = s.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def get_unsent_count(campaign_id: str) -> int:
    """Return number of pending leads for a campaign."""
    return sum(1 for s in _load_all()
               if s.get("campaign_id") == campaign_id and s.get("status") == "pending")


def get_active_count(campaign_id: str) -> int:
    """Return number of active conversations for a campaign."""
    return sum(1 for s in _load_all()
               if s.get("campaign_id") == campaign_id and s.get("status") == "active")
