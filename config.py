import os

# ── wacli binary ──────────────────────────────────────────────────────────────
WACLI = "wacli"

# ── Notifications ─────────────────────────────────────────────────────────────
# Your own WhatsApp JID — gets alerted when a chat turns to money
NOTIFY_JID = os.getenv("NOTIFY_JID", "923001234567@s.whatsapp.net")

# ── Timing ────────────────────────────────────────────────────────────────────
WAIT_AFTER_REPLY_SECONDS  = 20   # pause before replying (they may still be typing)
POLL_INTERVAL_SECONDS     = 5    # how often the main loop checks all chats
MULTI_MESSAGE_DELAY       = 2    # pause between split messages (|||)
MOVE_ON_NO_REPLY_HOURS    = 0.0167     # move to next lead if they never replied
MOVE_ON_REPLIED_HOURS     = 5    # move to next lead after their last reply
STATES_DIR                = "states"  # folder — one JSON file per chat

# ── AI ────────────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = "sk-c749d369622646aeaf909f89b33e2648"
DEEPSEEK_MODEL   = "deepseek-chat"
MAX_HISTORY      = 50

# ── State persistence ─────────────────────────────────────────────────────────
STATE_FILE = "chat_states.json"

# ── Leads ─────────────────────────────────────────────────────────────────────
# Path to a CSV file with columns:
#   jid, name, facebook, website, running_ads, completion_score
LEADS_CSV = "leads.csv"

# Fallback inline leads (used if LEADS_CSV does not exist)
FALLBACK_LEADS = [
    {
        "jid":              "242142018560089@lid",
        "name":             "Haroon's Electronics",
        "facebook":         "https://facebook.com/haroonelectronics",
        "website":          "haroonelectronics.com",
        "running_ads":      False,
        "completion_score": 75,
    },
]

# ── Money-detection keywords ──────────────────────────────────────────────────
MONEY_KEYWORDS = [
    "price", "rate", "charge", "cost", "budget", "fee", "fees",
    "rupee", "rupees", "rs", "payment", "pay", "paid", "invoice",
    "kitna", "kitne", "paisa", "paise", "amount", "package", "packages",
    "how much", "kya rate", "kya charges",
]