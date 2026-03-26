import os

# ── wacli binary ──────────────────────────────────────────────────────────────
# Use an absolute path or leave as "wacli" if it's on your PATH.
WACLI = "wacli"
TARGET_JID = "242142018560089@lid"

# ── Timing ────────────────────────────────────────────────────────────────────
WAIT_AFTER_REPLY_SECONDS = 20   # how long to wait after detecting a reply
POLL_INTERVAL_SECONDS    = 5    # how often to check for new messages
MULTI_MESSAGE_DELAY      = 2    # pause between split messages (|||)

# ── AI ────────────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "YOUR_API_KEY_HERE")
DEEPSEEK_MODEL   = "deepseek-chat"
MAX_HISTORY      = 50           # how many messages of history to send to the model

# ── Business data (dummy — will come from CSV in production) ──────────────────
BUSINESS = {
    "name":             "Haroon's Electronics",
    "owner_phone":      TARGET_JID,
    "facebook":         "https://facebook.com/haroonelectronics",
    "website":          "haroonelectronics.com",
    "running_ads":      False,
    "completion_score": 75,
}