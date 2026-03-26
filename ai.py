"""
ai.py — Stateless AI calls. History is owned by ChatState, not here.
"""

import logging
import time

import requests

from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, MAX_HISTORY, MONEY_KEYWORDS
from prompt import build_system_prompt

logger = logging.getLogger(__name__)


# ── Reply ─────────────────────────────────────────────────────────────────────

def get_reply(
    business: dict,
    history: list[dict],
    user_text: str,
    retries: int = 3,
    backoff: float = 5.0,
) -> tuple[str, list[dict]]:
    """
    Append user_text to a COPY of history, call DeepSeek, return
    (reply_text, updated_history). Caller is responsible for saving updated_history.
    """
    updated_history = history + [{"role": "user", "content": user_text}]

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": build_system_prompt(business)},
            *updated_history[-MAX_HISTORY:],
        ],
    }

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            logger.debug("Token usage: %s", data.get("usage"))
            reply = data["choices"][0]["message"]["content"]

            # Record the assistant reply parts in history
            parts = [p.strip() for p in reply.split("|||") if p.strip()]
            for part in parts:
                updated_history.append({"role": "assistant", "content": part})

            return reply, updated_history

        except (requests.RequestException, KeyError) as exc:
            last_exc = exc
            wait = backoff * attempt
            logger.warning(
                "DeepSeek attempt %d/%d failed: %s — retrying in %.0fs",
                attempt, retries, exc, wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"DeepSeek call failed after {retries} attempts") from last_exc


# ── Money detection ───────────────────────────────────────────────────────────

def contains_money_talk(text: str) -> bool:
    """Return True if the text likely contains pricing / money discussion."""
    lower = text.lower()
    return any(kw in lower for kw in MONEY_KEYWORDS)