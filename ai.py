"""
ai.py — Stateless AI calls. Returns structured LLMDecision objects.
History is owned by ChatState, not here.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Literal

import requests

from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, MAX_HISTORY
from prompt import build_system_prompt

logger = logging.getLogger(__name__)

Action = Literal["reply", "ignore", "end_conversation", "request_human"]

SAFE_FALLBACK_ACTION: Action = "request_human"


@dataclass
class LLMDecision:
    action: Action
    reply_text: str
    conversion_detected: bool
    money_talk_detected: bool
    reasoning: str

    @classmethod
    def safe_fallback(cls, reason: str = "parse error") -> "LLMDecision":
        """Used when the LLM response cannot be parsed — escalate to human."""
        return cls(
            action="request_human",
            reply_text="",
            conversion_detected=False,
            money_talk_detected=False,
            reasoning=f"Fallback triggered: {reason}",
        )


# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json(raw: str) -> dict:
    """
    Pull the first JSON object out of raw text.
    LLMs sometimes wrap output in ```json ... ``` even when told not to.
    """
    # Strip markdown fences if present
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find first { ... } block
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No valid JSON found in: {raw[:300]}")


def _validate(data: dict) -> LLMDecision:
    """Validate and coerce the parsed dict into an LLMDecision."""
    valid_actions = {"reply", "ignore", "end_conversation", "request_human"}

    action = data.get("action", "")
    if action not in valid_actions:
        raise ValueError(f"Invalid action: {action!r}")

    return LLMDecision(
        action=action,
        reply_text=str(data.get("reply_text", "")),
        conversion_detected=bool(data.get("conversion_detected", False)),
        money_talk_detected=bool(data.get("money_talk_detected", False)),
        reasoning=str(data.get("reasoning", "")),
    )


# ── Main call ─────────────────────────────────────────────────────────────────

def get_decision(
    business: dict,
    history: list[dict],
    user_text: str,
    retries: int = 3,
    backoff: float = 5.0,
) -> tuple[LLMDecision, list[dict]]:
    """
    Append user_text to a copy of history, call DeepSeek, return
    (LLMDecision, updated_history). Caller saves updated_history.

    On unrecoverable errors returns a safe fallback decision.
    """
    updated_history = history + [{"role": "user", "content": user_text}]

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": build_system_prompt(business)},
            *updated_history[-MAX_HISTORY:],
        ],
        "response_format": {"type": "json_object"},  # DeepSeek supports this
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

            raw = data["choices"][0]["message"]["content"]
            logger.debug("LLM raw response: %s", raw[:500])

            parsed = _extract_json(raw)
            decision = _validate(parsed)

            logger.info(
                "Decision for %s — action=%s conversion=%s | %s",
                business["name"],
                decision.action,
                decision.conversion_detected,
                decision.reasoning,
            )

            # Record the assistant reply in history only if we're actually replying
            if decision.action == "reply" and decision.reply_text:
                parts = [p.strip() for p in decision.reply_text.split("|||") if p.strip()]
                for part in parts:
                    updated_history.append({"role": "assistant", "content": part})

            return decision, updated_history

        except (requests.RequestException, KeyError) as exc:
            last_exc = exc
            wait = backoff * attempt
            logger.warning(
                "DeepSeek attempt %d/%d failed: %s — retrying in %.0fs",
                attempt, retries, exc, wait,
            )
            time.sleep(wait)

        except (ValueError, json.JSONDecodeError) as exc:
            logger.error("Could not parse LLM response: %s", exc)
            return LLMDecision.safe_fallback(str(exc)), updated_history

    logger.error("DeepSeek call failed after %d attempts", retries)
    return LLMDecision.safe_fallback(f"API error: {last_exc}"), updated_history