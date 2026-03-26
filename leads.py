"""
leads.py — Load leads from a CSV file or fall back to config.
"""

import csv
import logging
import os
import re

from config import LEADS_CSV, FALLBACK_LEADS

logger = logging.getLogger(__name__)


def phone_to_jid(phone: str) -> str | None:
    """
    Convert a raw phone number to a WhatsApp JID.
    Strips non-digits, ensures it starts with a country code.
    Returns None if the number looks invalid.
    """
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None
    # Pakistani numbers: 03xxxxxxxxx → 923xxxxxxxxx
    if digits.startswith("0") and len(digits) == 11:
        digits = "92" + digits[1:]
    if len(digits) < 10:
        return None
    return f"{digits}@s.whatsapp.net"


def load_leads() -> list[dict]:
    if not os.path.exists(LEADS_CSV):
        logger.info("No leads CSV found at '%s', using FALLBACK_LEADS", LEADS_CSV)
        return FALLBACK_LEADS

    leads = []
    skipped = 0
    with open(LEADS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Use existing jid column, or derive from phone
            jid = row.get("jid", "").strip()
            if not jid:
                jid = phone_to_jid(row.get("phone", ""))
            if not jid:
                logger.warning("Skipping '%s' — no jid or phone", row.get("name", "?"))
                skipped += 1
                continue
            row["jid"] = jid

            # Normalise types
            row["running_ads"] = row.get("running_ads", "false").strip().lower() in (
                "true", "1", "yes"
            )
            try:
                row["completion_score"] = int(row.get("completion_score", 0))
            except ValueError:
                row["completion_score"] = 0

            # Map 'facebook_url' column name (from scraper) to 'facebook'
            if "facebook" not in row and "facebook_url" in row:
                row["facebook"] = row["facebook_url"]

            leads.append(row)

    logger.info("Loaded %d leads from %s (%d skipped)", len(leads), LEADS_CSV, skipped)
    return leads