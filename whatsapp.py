import json
import logging
import subprocess
import time

from config import MULTI_MESSAGE_DELAY, WACLI

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

_sync_process: subprocess.Popen | None = None


# ── Sync process management ───────────────────────────────────────────────────

def start_sync() -> None:
    """Start the wacli sync --follow --ipc background process."""
    global _sync_process
    if _sync_process and _sync_process.poll() is None:
        return  # already running
    logger.debug("Starting wacli sync")
    # Start with pipes to capture initial errors
    _sync_process = subprocess.Popen(
        [WACLI, "sync", "--follow", "--ipc"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Give it a moment to start
    import time
    time.sleep(3)
    # Check if it's still running
    if _sync_process.poll() is not None:
        stdout, stderr = _sync_process.communicate()
        logger.error("wacli sync failed to start. stderr: %s", stderr.decode())
        raise RuntimeError(f"wacli sync failed to start: {stderr.decode()}")
    else:
        # Switch to DEVNULL to avoid pipe blocking
        logger.debug("wacli sync started successfully")
        # Can't change pipes after process starts, but it should be fine
        # as long as we don't let pipes fill up


def stop_sync() -> None:
    """Stop the wacli sync background process (called on shutdown only)."""
    global _sync_process
    if _sync_process and _sync_process.poll() is None:
        logger.debug("Stopping wacli sync")
        _sync_process.terminate()
        try:
            stdout, stderr = _sync_process.communicate(timeout=5)
            if stderr:
                logger.debug("wacli sync stderr on stop: %s", stderr.decode())
        except subprocess.TimeoutExpired:
            _sync_process.kill()
            _sync_process.communicate()
    _sync_process = None


# ── Message retrieval ─────────────────────────────────────────────────────────

def get_messages(jid: str) -> list[dict]:
    """
    Fetch the message list for a given JID.
    Returns a list of message dicts (newest first).
    Raises RuntimeError if wacli returns a non-zero exit code.
    """
    logger.debug("get_messages called for %s", jid)
    result = subprocess.run(
        [WACLI, "messages", "list", "--chat", jid, "--json"],
        capture_output=True,
        text=True,
        timeout=15,
    )

    if result.returncode != 0:
        logger.error("wacli failed for %s: %s", jid, result.stderr.strip())
        raise RuntimeError(
            f"wacli messages list failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        messages = data["data"]["messages"]
        logger.debug("get_messages returned %d messages for %s", len(messages), jid)
        if messages:
            # Check how many messages have IDs
            ids = [msg.get("id") for msg in messages]
            non_none_ids = [id for id in ids if id is not None]
            logger.debug("Message IDs: %d total, %d non-None", len(ids), len(non_none_ids))
            if non_none_ids:
                logger.debug("First non-None ID: %s", non_none_ids[0])
            logger.debug("First message: id=%s, fromMe=%s, text=%.50s...",
                        messages[0].get("id"), messages[0].get("fromMe"),
                        messages[0].get("text") or messages[0].get("body") or "")
        return messages
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("Failed to parse wacli output for %s: %s", jid, exc)
        raise RuntimeError(f"Unexpected wacli output: {result.stdout[:200]}") from exc


# ── Sending ───────────────────────────────────────────────────────────────────

def send_message(jid: str, text: str) -> list[str]:
    """
    Send one or more messages to a JID.
    Text may contain ||| separators — each part is sent as a separate message
    with a short delay between them.
    Returns the list of parts that were sent.
    """
    parts = [p.strip() for p in text.split("|||") if p.strip()]

    for i, part in enumerate(parts):
        result = subprocess.run(
            [WACLI, "send", "text", "--to", jid, "--message", part],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"wacli send failed (exit {result.returncode}): {result.stderr.strip()}"
            )

        logger.info("Sent: %s", part)

        if i < len(parts) - 1:
            time.sleep(MULTI_MESSAGE_DELAY)

    return parts