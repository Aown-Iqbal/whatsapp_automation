"""
leads.py — Load leads from a CSV file or fall back to config.
"""

import csv
import logging
import os

from config import LEADS_CSV, FALLBACK_LEADS

logger = logging.getLogger(__name__)


def load_leads() -> list[dict]:
    if not os.path.exists(LEADS_CSV):
        logger.info("No leads CSV found at '%s', using FALLBACK_LEADS", LEADS_CSV)
        return FALLBACK_LEADS

    leads = []
    with open(LEADS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalise types
            row["running_ads"] = row.get("running_ads", "false").strip().lower() in (
                "true", "1", "yes"
            )
            try:
                row["completion_score"] = int(row.get("completion_score", 0))
            except ValueError:
                row["completion_score"] = 0
            leads.append(row)

    logger.info("Loaded %d leads from %s", len(leads), LEADS_CSV)
    return leads