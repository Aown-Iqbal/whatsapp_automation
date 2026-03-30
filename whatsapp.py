import json
import logging
import subprocess
import time

from config import WACLI

logger = logging.getLogger(__name__)

_sync_process: subprocess.Popen | None = None


# ── Sync process management ───────────────────────────────────────────────────

def start_sync() -> None:
    """Start the wacli sync --follow --ipc background process."""
    global _sync_process
    if _sync_process and _sync_process.poll() is None:
        return  # already running
    logger.debug("Starting wacli sync")
    _sync_process = subprocess.Popen(
        [WACLI, "sync", "--follow", "--ipc"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_sync() -> None:
    """Stop the wacli sync background process (called on shutdown only)."""
    global _sync_process
    if _sync_process and _sync_process.poll() is None:
        logger.debug("Stopping wacli sync")
        _sync_process.terminate()
        _sync_process.wait()
    _sync_process = None


# ── Message retrieval ─────────────────────────────────────────────────────────

def get_messages(jid: str, limit: int = 20) -> list[dict]:
    """
    Fetch recent messages for a specific chat JID.
    Returns a list of message dicts ordered newest first.
    Raises RuntimeError on failure.
    """
    result = subprocess.run(
        [WACLI, "messages", "list", "--chat", jid, "--limit", str(limit), "--json"],
        capture_output=True,
        text=True,
        timeout=15,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"wacli messages list failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        return data["data"]["messages"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError(f"Unexpected wacli output: {result.stdout[:200]}") from exc


# ── Sending ───────────────────────────────────────────────────────────────────

def send_message(jid: str, text: str) -> None:
    """
    Send a single message to a JID.
    No ||| splitting — the AI calls this tool once per message it wants to send.
    Raises RuntimeError on failure.
    """
    result = subprocess.run(
        [WACLI, "send", "text", "--to", jid, "--message", text],
        capture_output=True,
        text=True,
        timeout=15,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"wacli send failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    logger.info("Sent to %s: %s", jid, text)