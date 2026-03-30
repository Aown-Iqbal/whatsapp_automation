import os

# ── wacli binary ──────────────────────────────────────────────────────────────
WACLI = "wacli"

# ── Paths ─────────────────────────────────────────────────────────────────────
STATE_DIR = "state"        # folder where per-lead JSON state files live
CSV_PATH  = "leads.csv"    # input CSV: phone,name,facebook,website,running_ads,completion_score

# ── Notify ────────────────────────────────────────────────────────────────────
# This number receives alerts when the AI encounters something it can't handle
OWNER_JID = "923708454525@s.whatsapp.net"

# ── Timing ────────────────────────────────────────────────────────────────────
WAIT_AFTER_REPLY_SECONDS = 20   # wait after first new message before fetching again
POLL_INTERVAL_SECONDS    = 5    # how often the main loop cycles through all leads

# ── AI ────────────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY     = "sk-c749d369622646aeaf909f89b33e2648"
DEEPSEEK_MODEL       = "deepseek-chat"
MAX_HISTORY          = 50   # max history messages sent to the model per turn
MAX_TOOL_ITERATIONS  = 5    # max agentic loop iterations per turn (safety cap)

# ── Follow-up scheduler ───────────────────────────────────────────────────────
FOLLOWUP_WAIT_HOURS  = 24   # hours of silence before sending a follow-up
MAX_FOLLOWUPS        = 3    # max follow-up attempts per lead before giving upimport os

# ── wacli binary ──────────────────────────────────────────────────────────────
WACLI = "wacli"

# ── Paths ─────────────────────────────────────────────────────────────────────
STATE_DIR = "state"        # folder where per-lead JSON state files live
CSV_PATH  = "leads.csv"    # input CSV: phone,name,facebook,website,running_ads,completion_score

# ── Notify ────────────────────────────────────────────────────────────────────
# This number receives alerts when the AI encounters something it can't handle
OWNER_JID = "923708454525@s.whatsapp.net"

# ── Timing ────────────────────────────────────────────────────────────────────
WAIT_AFTER_REPLY_SECONDS = 20   # wait after first new message before fetching again
POLL_INTERVAL_SECONDS    = 5    # how often the main loop cycles through all leads

# ── AI ────────────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY     = "sk-c749d369622646aeaf909f89b33e2648"
DEEPSEEK_MODEL       = "deepseek-chat"
MAX_HISTORY          = 50   # max history messages sent to the model per turn
MAX_TOOL_ITERATIONS  = 5    # max agentic loop iterations per turn (safety cap)

# ── Follow-up scheduler ───────────────────────────────────────────────────────
FOLLOWUP_WAIT_HOURS  = 24   # hours of silence before sending a follow-up
MAX_FOLLOWUPS        = 3    # max follow-up attempts per lead before giving up