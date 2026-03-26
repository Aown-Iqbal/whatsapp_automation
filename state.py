"""
state.py — Per-chat state. Each chat lives in its own JSON file under STATES_DIR.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from config import STATES_DIR

logger = logging.getLogger(__name__)

_states: dict[str, "ChatState"] = {}


@dataclass
class ChatState:
    jid: str
    business: dict

    history: list[dict]          = field(default_factory=list)
    last_seen_id: Optional[str]  = None
    last_inbound_time: Optional[float] = None   # epoch — last time THEY messaged us
    opening_sent_time: Optional[float] = None   # epoch — when we sent the opening

    opening_sent:   bool = False
    followup_sent:  bool = False
    notified_money: bool = False
    dormant:        bool = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _jid_to_filename(jid: str) -> str:
    safe = jid.replace("@", "_").replace(".", "_").replace("/", "_")
    return os.path.join(STATES_DIR, f"{safe}.json")


def _ensure_dir() -> None:
    os.makedirs(STATES_DIR, exist_ok=True)


# ── Public API ────────────────────────────────────────────────────────────────

def get(jid: str) -> Optional[ChatState]:
    return _states.get(jid)


def get_all() -> list[ChatState]:
    return list(_states.values())


def save_one(chat: ChatState) -> None:
    _ensure_dir()
    try:
        with open(_jid_to_filename(chat.jid), "w") as f:
            json.dump(asdict(chat), f, indent=2)
    except Exception:
        logger.exception("Failed to save state for %s", chat.jid)


def save() -> None:
    for chat in _states.values():
        save_one(chat)


def load(leads: list[dict]) -> None:
    _ensure_dir()

    # Load any existing state files
    for filename in os.listdir(STATES_DIR):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(STATES_DIR, filename)
        try:
            with open(path) as f:
                d = json.load(f)
            chat = ChatState(**d)
            _states[chat.jid] = chat
            logger.debug("Restored state: %s", chat.jid)
        except Exception:
            logger.exception("Could not load %s", path)

    logger.info("Restored %d chat state(s) from %s/", len(_states), STATES_DIR)

    # Add new leads not yet in state
    for business in leads:
        jid = business["jid"]
        if jid not in _states:
            logger.info("New lead: %s (%s)", business["name"], jid)
            _states[jid] = ChatState(jid=jid, business=business)
            save_one(_states[jid])


def seconds_since_last_inbound(chat: ChatState) -> Optional[float]:
    if chat.last_inbound_time is None:
        return None
    return time.time() - chat.last_inbound_time


def seconds_since_opening(chat: ChatState) -> Optional[float]:
    if chat.opening_sent_time is None:
        return None
    return time.time() - chat.opening_sent_time