"""
reporter.py — Sends structured alert messages to the owner (NOTIFY_JID).

Two alert types:
  - conversion  : lead has shown genuine interest
  - human_needed: LLM cannot handle the query, human must take over
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import config
import whatsapp

logger = logging.getLogger(__name__)


def _ts() -> str:
    return datetime.now().strftime("%d %b %Y  %H:%M")


def _send(text: str, dry_run: bool) -> None:
    if dry_run:
        logger.info("[DRY-RUN] REPORT → %s: %s", config.NOTIFY_JID, text[:200])
        return
    try:
        whatsapp.send_message(config.NOTIFY_JID, text)
    except Exception:
        logger.exception("Failed to send report to owner")


# ── Public API ────────────────────────────────────────────────────────────────

def money_talk(
    jid: str,
    business: dict,
    dry_run: bool = False,
) -> None:
    """Alert the owner that a lead has started asking about pricing/money."""
    name  = business.get("name", jid)
    phone = business.get("phone", jid)

    msg = f"[MONEY TALK] {name}|||Phone: {phone}|||Time: {_ts()}"
    logger.info("Money talk alert — %s", name)
    _send(msg, dry_run)


def conversion(
    jid: str,
    business: dict,
    reasoning: str,
    dry_run: bool = False,
) -> None:
    """
    Alert the owner that a lead has converted (expressed genuine interest).
    """
    name  = business.get("name", jid)
    phone = business.get("phone", jid)

    msg = (
        f"[CONVERSION] {name}|||"
        f"Phone: {phone}|||"
        f"Time: {_ts()}|||"
        f"Signal: {reasoning}"
    )
    logger.info("Conversion alert — %s", name)
    _send(msg, dry_run)


def human_needed(
    jid: str,
    business: dict,
    last_message: str,
    reasoning: str,
    dry_run: bool = False,
) -> None:
    """
    Alert the owner that the LLM cannot handle this lead and a human must step in.
    Automated replies for this lead will be stopped by main.py after this call.
    """
    name  = business.get("name", jid)
    phone = business.get("phone", jid)

    # Truncate long messages so the alert stays readable
    snippet = last_message[:200] + ("..." if len(last_message) > 200 else "")

    msg = (
        f"[HUMAN NEEDED] {name}|||"
        f"Phone: {phone}|||"
        f"Their message: {snippet}|||"
        f"Reason: {reasoning}|||"
        f"Time: {_ts()}"
    )
    logger.info("Human-needed alert — %s", name)
    _send(msg, dry_run)