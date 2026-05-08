import json
import logging
import subprocess
from subprocess import TimeoutExpired

logger = logging.getLogger(__name__)

WACLI = "wacli"


def get_messages(jid: str, limit: int = 20) -> list[dict]:
    """
    Fetch recent messages for a specific chat JID.
    Returns a list of message dicts ordered newest first.
    Raises RuntimeError on failure.
    """
    try:
        result = subprocess.run(
            [WACLI, "messages", "list", "--chat", jid, "--limit", str(limit), "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except TimeoutExpired:
        raise RuntimeError(f"wacli messages list timed out for {jid}")

    if result.returncode != 0:
        raise RuntimeError(
            f"wacli messages list failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        return data["data"]["messages"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError(f"Unexpected wacli output: {result.stdout[:200]}") from exc


def send_message(jid: str, text: str) -> None:
    """
    Send a single message to a JID.
    Raises RuntimeError on failure.
    """
    try:
        result = subprocess.run(
            [WACLI, "send", "text", "--to", jid, "--message", text],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except TimeoutExpired:
        raise RuntimeError(f"wacli send timed out for {jid}")

    if result.returncode != 0:
        raise RuntimeError(
            f"wacli send failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    logger.info("Sent to %s: %s", jid, text)
