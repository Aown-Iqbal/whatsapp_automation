import os

# ── wacli binary ──────────────────────────────────────────────────────────────
WACLI = "wacli"

# ── CSV input ─────────────────────────────────────────────────────────────────
# Required columns: name, owner_phone, facebook, website, running_ads,
#                   completion_score
# Optional column:  status  (pending | done | no_reply | error)
#   If the column exists, rows whose status is not "pending" are skipped,
#   letting you safely resume an interrupted run.
CSV_PATH = os.environ.get("CSV_PATH", "businesses.csv")

# ── Timing ────────────────────────────────────────────────────────────────────
WAIT_AFTER_REPLY_SECONDS = 20   # wait after detecting a reply (they may still be typing)
POLL_INTERVAL_SECONDS    = 5    # how often to check for new messages
MULTI_MESSAGE_DELAY      = 2    # pause between split messages (|||)

# How long (seconds) to wait for a reply before giving up on a contact and
# moving to the next one.  Default: 5 minutes.
REPLY_TIMEOUT_SECONDS = int(os.environ.get("REPLY_TIMEOUT_SECONDS", 300))

# ── AI ────────────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL   = "deepseek-chat"
MAX_HISTORY      = 50