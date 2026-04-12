import os
from dotenv import load_dotenv
load_dotenv()

# ── wacli binary ──────────────────────────────────────────────────────────────
WACLI = "wacli"

# ── Paths ─────────────────────────────────────────────────────────────────────
STATE_DIR = "state"        # folder where per-lead JSON state files live
CSV_PATH  = "leads.csv"    # input CSV: phone,name,facebook,website,running_ads,completion_score

# ── Notify ────────────────────────────────────────────────────────────────────
OWNER_JID = os.getenv("OWNER_JID")

# ── Timing ────────────────────────────────────────────────────────────────────
WAIT_AFTER_REPLY_SECONDS = 20   # wait after first new message before fetching again
POLL_INTERVAL_SECONDS    = 5    # how often the main loop cycles through all leads

# ── Scheduled outreach ────────────────────────────────────────────────────────
# Local timezone offset from UTC (PKT = UTC+5)
LOCAL_TZ_OFFSET_HOURS = 5

# Hours (local time, 24h) at which a new batch of openings may be sent
BATCH_HOURS = [9, 14, 17, 21]

# Max leads to open per batch window
BATCH_SIZE = 10

# ── AI ────────────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY     = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL       = "deepseek-reasoner"
MAX_HISTORY          = 50
MAX_TOOL_ITERATIONS  = 5