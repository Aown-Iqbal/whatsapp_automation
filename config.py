import os
from dotenv import load_dotenv
load_dotenv()

# ── wacli binary ──────────────────────────────────────────────────────────────
WACLI = "wacli"

# ── Paths ─────────────────────────────────────────────────────────────────────
STATE_DIR = "state"
CSV_PATH  = "leads.csv"

# ── Notify ────────────────────────────────────────────────────────────────────
OWNER_JID = os.getenv("OWNER_JID")

# ── Timing ────────────────────────────────────────────────────────────────────
WAIT_AFTER_REPLY_SECONDS     = 20
POLL_INTERVAL_SECONDS        = 5
BATCH_INTER_LEAD_DELAY_MIN   = 60   # seconds between opening messages
BATCH_INTER_LEAD_DELAY_MAX   = 180

# ── AI ────────────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY     = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL       = "deepseek-v4-flash"
MAX_HISTORY          = 50
MAX_TOOL_ITERATIONS  = 5