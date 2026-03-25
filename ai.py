import logging
import time

import requests

from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, MAX_HISTORY

logger = logging.getLogger(__name__)

_history: list[dict] = []
_system_prompt: str = ""


# ── Session management ────────────────────────────────────────────────────────

def new_conversation(system_prompt: str) -> None:
    """
    Call this before starting each new contact.
    Clears history and sets the system prompt for the conversation.
    """
    global _history, _system_prompt
    _history = []
    _system_prompt = system_prompt
    logger.debug("Conversation reset, new system prompt loaded.")


# ── History helpers ───────────────────────────────────────────────────────────

def add_user_message(text: str) -> None:
    _history.append({"role": "user", "content": text})


def add_assistant_messages(parts: list[str]) -> None:
    for part in parts:
        _history.append({"role": "assistant", "content": part})


def get_history() -> list[dict]:
    return list(_history)


# ── DeepSeek call ─────────────────────────────────────────────────────────────

def get_reply(user_text: str, retries: int = 3, backoff: float = 5.0) -> str:
    """
    Append user_text to history, call DeepSeek, return the model's reply.
    Retries up to `retries` times on network/server errors.
    """
    if not _system_prompt:
        raise RuntimeError("No system prompt set. Call ai.new_conversation() first.")

    add_user_message(user_text)

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _system_prompt},
            *_history[-MAX_HISTORY:],
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
            return reply

        except (requests.RequestException, KeyError) as exc:
            last_exc = exc
            wait = backoff * attempt
            logger.warning(
                "DeepSeek attempt %d/%d failed: %s — retrying in %.0fs",
                attempt, retries, exc, wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"DeepSeek call failed after {retries} attempts") from last_exc