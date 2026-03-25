"""
state.py — Per-chat state management with JSON persistence.

Each chat is keyed by JID. State survives restarts.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from config import STATE_FILE

logger = logging.getLogger(__name__)


@dataclass
class ChatState:
    jid: str
    business: dict                    # full business dict for this lead

    history: list[dict] = field(default_factory=list)
    last_seen_id: Optional[str] = None
    last_inbound_time: Optional[float] = None  # epoch seconds

    opening_sent: bool = False
    followup_sent: bool = False
    notified_money: bool = False

    # True = we stopped proactively reaching out, but will still reply if they message
    dormant: bool = False


# ── Module-level store ────────────────────────────────────────────────────────

_states: dict[str, ChatState] = {}


def get(jid: str) -> Optional[ChatState]:
    return _states.get(jid)


def get_all() -> list[ChatState]:
    return list(_states.values())


def add(state: ChatState) -> None:
    _states[state.jid] = state
    save()


def save() -> None:
    """Persist all states to disk."""
    try:
        serialisable = {}
        for jid, s in _states.items():
            d = asdict(s)
            serialisable[jid] = d
        with open(STATE_FILE, "w") as f:
            json.dump(serialisable, f, indent=2)
    except Exception:
        logger.exception("Failed to save state")


def load(leads: list[dict]) -> None:
    """
    Load persisted states from disk, then add any new leads that
    aren't in the file yet.
    """
    persisted: dict = {}
    try:
        with open(STATE_FILE) as f:
            persisted = json.load(f)
        logger.info("Loaded %d persisted chat states", len(persisted))
    except FileNotFoundError:
        logger.info("No state file found, starting fresh")
    except Exception:
        logger.exception("Could not load state file, starting fresh")

    # Restore persisted chats
    for jid, d in persisted.items():
        _states[jid] = ChatState(**d)

    # Add new leads not yet in state
    for business in leads:
        jid = business["jid"]
        if jid not in _states:
            logger.info("Adding new lead: %s (%s)", business["name"], jid)
            _states[jid] = ChatState(jid=jid, business=business)

    save()


def seconds_since_last_inbound(state: ChatState) -> Optional[float]:
    if state.last_inbound_time is None:
        return None
    return time.time() - state.last_inbound_time