#!/usr/bin/env python3
"""
WhatsApp sync daemon — keeps wacli sync running and detects new inbound messages.

Usage:
  python daemon.py start [--foreground]
  python daemon.py stop
  python daemon.py status
  python daemon.py restart
"""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [daemon] %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("daemon")

PID_FILE = "daemon.pid"
PENDING_FILE = "pending_replies.json"
STATE_DIR = os.environ.get("STATE_DIR", "states")
POLL_INTERVAL = 15  # seconds between reply checks
TRIGGER_COOLDOWN = 180  # minimum seconds between Claude Code triggers
# wacli only works inside WSL, so prefix with 'wsl' when running on Windows
WACLI = ["wsl", "wacli"] if sys.platform == "win32" else ["wacli"]
# Claude CLI — also needs wsl on Windows
CLAUDE_CLI = ["wsl", "claude"] if sys.platform == "win32" else ["claude"]


# ── PID file helpers ─────────────────────────────────────────────────────────

def read_pid() -> int | None:
    try:
        return int(Path(PID_FILE).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def write_pid(pid: int) -> None:
    Path(PID_FILE).write_text(str(pid))


def remove_pid() -> None:
    Path(PID_FILE).unlink(missing_ok=True)


def is_running() -> bool:
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # signal 0 just checks existence
        return True
    except OSError:
        remove_pid()
        return False


# ── wacli helpers ────────────────────────────────────────────────────────────

def start_wacli_sync() -> subprocess.Popen:
    """Spawn wacli sync --follow --ipc as a background subprocess."""
    logger.info("Starting wacli sync --follow --ipc")
    return subprocess.Popen(
        WACLI + ["sync", "--follow", "--ipc"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def get_messages_raw(jid: str, limit: int = 5) -> list[dict]:
    """Call wacli messages list and return parsed message list."""
    try:
        result = subprocess.run(
            WACLI + ["messages", "list", "--chat", jid, "--limit", str(limit), "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning("wacli messages list failed for %s: %s", jid, result.stderr.strip())
            return []
        data = json.loads(result.stdout)
        return data.get("data", {}).get("messages", [])
    except Exception as exc:
        logger.warning("wacli messages list error for %s: %s", jid, exc)
        return []


# ── State scanning ───────────────────────────────────────────────────────────

def load_active_states() -> list[dict]:
    """Load all state files with status 'active'."""
    d = Path(STATE_DIR)
    if not d.exists():
        return []
    states = []
    for f in d.glob("*.json"):
        try:
            state = json.loads(f.read_text())
            if state.get("status") == "active":
                states.append(state)
        except (json.JSONDecodeError, KeyError):
            logger.warning("Skipping corrupt state file: %s", f.name)
    return states


def parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


# ── Reply detection ──────────────────────────────────────────────────────────

def check_replies() -> dict:
    """
    Scan all active chats for new inbound messages.
    Returns a dict keyed by phone number with pending reply info.
    """
    pending: dict = {}
    active_states = load_active_states()

    if not active_states:
        return pending

    for state in active_states:
        jid = state["jid"]
        phone = state["phone"]
        last_processed = state.get("last_processed_at")

        messages = get_messages_raw(jid, limit=5)
        if not messages:
            continue

        new_inbound = []
        for msg in messages:
            if msg.get("FromMe", False):
                continue
            msg_ts = parse_ts(msg["Timestamp"])
            if last_processed:
                cursor_ts = parse_ts(last_processed)
                if msg_ts <= cursor_ts:
                    break
            text = msg.get("Text") or msg.get("DisplayText") or ""
            media_type = msg.get("MediaType") or ""
            new_inbound.append({
                "timestamp": msg["Timestamp"],
                "text": text,
                "media_type": media_type,
            })

        if new_inbound:
            new_inbound.reverse()  # chronological order
            pending[phone] = {
                "phone": phone,
                "jid": jid,
                "new_count": len(new_inbound),
                "latest_at": new_inbound[-1]["timestamp"],
                "preview": new_inbound[-1].get("text", f"[{new_inbound[-1].get('media_type', 'media')}]")[:100],
            }

    return pending


def write_pending(pending: dict) -> None:
    """Atomically write pending replies file."""
    if not pending:
        # Don't create the file if nothing pending
        return
    tmp = Path(PENDING_FILE + ".tmp")
    try:
        tmp.write_text(json.dumps(pending, indent=2), encoding="utf-8")
        tmp.replace(PENDING_FILE)
    except Exception as exc:
        logger.error("Failed to write pending file: %s", exc)
        tmp.unlink(missing_ok=True)


def trigger_claude(pending: dict) -> None:
    """Fire-and-forget invocation of Claude Code to process pending replies."""
    chat_list = ", ".join(
        f"{p['phone']} ({p['new_count']} new)" for p in pending.values()
    )
    prompt = (
        f"Process pending WhatsApp replies. "
        f"Read pending_replies.json, decide responses to each lead, "
        f"send replies via wsl wacli, update state files, then clear pending_replies.json. "
        f"Chats with new messages: {chat_list}"
    )
    logger.info("Triggering Claude Code for %d chat(s)", len(pending))
    try:
        subprocess.Popen(
            CLAUDE_CLI + ["-p", prompt],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.error("Failed to trigger Claude Code: %s", exc)


# ── Main loop ────────────────────────────────────────────────────────────────

_sync_proc: subprocess.Popen | None = None
_shutdown: bool = False
_last_trigger_time: float = 0


def handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s, shutting down...", signum)
    _shutdown = True


def run_foreground():
    global _sync_proc, _shutdown, _last_trigger_time

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    write_pid(os.getpid())
    _sync_proc = start_wacli_sync()

    logger.info("Daemon running (pid %d), polling every %ds", os.getpid(), POLL_INTERVAL)

    while not _shutdown:
        # Health check: restart wacli sync if it died
        if _sync_proc and _sync_proc.poll() is not None:
            logger.warning("wacli sync process died (exit %s), restarting...", _sync_proc.returncode)
            _sync_proc = start_wacli_sync()

        # Check for new replies
        try:
            pending = check_replies()
            if pending:
                count = sum(p["new_count"] for p in pending.values())
                logger.info("Found %d new message(s) across %d chat(s)", count, len(pending))
                write_pending(pending)
                # Trigger Claude Code immediately (with cooldown to avoid spam)
                now = time.time()
                if now - _last_trigger_time >= TRIGGER_COOLDOWN:
                    trigger_claude(pending)
                    _last_trigger_time = now
                else:
                    logger.info("Skipping trigger (cooldown, next in %.0fs)",
                                TRIGGER_COOLDOWN - (now - _last_trigger_time))
        except Exception as exc:
            logger.error("Reply check failed: %s", exc)

        time.sleep(POLL_INTERVAL)

    # Cleanup
    logger.info("Stopping daemon...")
    if _sync_proc and _sync_proc.poll() is None:
        _sync_proc.terminate()
        try:
            _sync_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _sync_proc.kill()
            _sync_proc.wait()
    remove_pid()
    logger.info("Daemon stopped")


# ── CLI ──────────────────────────────────────────────────────────────────────

def cmd_start(foreground: bool = False) -> None:
    if is_running():
        logger.error("Daemon is already running (pid %d)", read_pid())
        sys.exit(1)

    if foreground:
        run_foreground()
    else:
        # Detach: spawn a new Python process running this same script in foreground
        script = Path(__file__).resolve()
        proc = subprocess.Popen(
            [sys.executable, str(script), "start", "--foreground"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        logger.info("Daemon started (pid %d)", proc.pid)
        # Give it a moment to write its PID file
        time.sleep(1)
        if is_running():
            logger.info("Daemon is running (pid %d)", read_pid())
        else:
            logger.error("Daemon failed to start")


def cmd_stop() -> None:
    pid = read_pid()
    if pid is None:
        logger.info("Daemon is not running")
        return
    logger.info("Stopping daemon (pid %d)...", pid)
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(15):
            time.sleep(0.5)
            if not is_running():
                logger.info("Daemon stopped")
                return
        # Force kill if still alive
        logger.warning("Daemon didn't stop, force killing...")
        os.kill(pid, signal.SIGKILL)
        remove_pid()
    except OSError:
        remove_pid()
        logger.info("Daemon was already dead")


def cmd_status() -> None:
    pid = read_pid()
    if pid and is_running():
        print(f"Daemon is running (pid {pid})")
        # Show pending file status
        pf = Path(PENDING_FILE)
        if pf.exists():
            try:
                pending = json.loads(pf.read_text())
                chats = len(pending)
                msgs = sum(p["new_count"] for p in pending.values())
                print(f"Pending replies: {msgs} message(s) across {chats} chat(s)")
            except Exception:
                print("Pending file exists but is unreadable")
        else:
            print("No pending replies")
    else:
        print("Daemon is not running")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python daemon.py [start|stop|status|restart]")
        print("       python daemon.py start --foreground  (run in foreground)")
        sys.exit(1)

    cmd = sys.argv[1]
    foreground = "--foreground" in sys.argv

    if cmd == "start":
        cmd_start(foreground=foreground)
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "status":
        cmd_status()
    elif cmd == "restart":
        cmd_stop()
        time.sleep(2)
        cmd_start(foreground=foreground)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
