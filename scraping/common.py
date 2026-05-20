import re
import sys

# ── Config ────────────────────────────────────────────────────────────────────

PROFILE_DIR = r"C:\Git Gud\chrome_profile"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1280, "height": 800}

# Domains that are not real business websites
JUNK_WEBSITE_DOMAINS = (
    "instagram.com",
    "wa.me",
    "whatsapp.com",
    "oladoc.com",
    "twitter.com",
    "youtube.com",
    "tiktok.com",
    "linkedin.com",
)

# Facebook URL path suffixes that indicate a non-page link
BAD_FB_PATHS = (
    "/reels", "/albums", "/mentions", "/posts", "/photos",
    "/videos", "/events", "/groups", "/share", "/permalink",
)

# Social domains to filter out when scraping website from Facebook About
SOCIAL_DOMAINS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "linkedin.com", "wa.me",
    "whatsapp.com", "snapchat.com",
)


# ── URL validation ────────────────────────────────────────────────────────────

def is_valid_facebook_page_url(url: str) -> bool:
    if not url or "facebook.com" not in url:
        return False
    if url.rstrip("/") in ("https://www.facebook.com", "https://facebook.com",
                           "http://www.facebook.com", "http://facebook.com"):
        return False
    if re.search(r'profile\.php$', url.rstrip("/")):
        return False
    if any(url.rstrip("/").endswith(p) or (p + "/") in url for p in BAD_FB_PATHS):
        return False
    return True


def is_real_website(url: str) -> bool:
    if not url:
        return False
    return not any(domain in url for domain in JUNK_WEBSITE_DOMAINS)


# ── Name cleaning ─────────────────────────────────────────────────────────────

def clean_name(name: str) -> str:
    return name.split("(")[0].split(" - ")[0].strip()


# ── Browser setup ─────────────────────────────────────────────────────────────

def launch_browser(playwright):
    """Create a persistent Chromium context with anti-detection measures."""
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        args=[
            f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}",
            "--disable-blink-features=AutomationControlled",
        ],
        user_agent=USER_AGENT,
        viewport=VIEWPORT,
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return context


def ensure_logged_in(page):
    """Check Facebook login status. Prompt user to log in if running interactively."""
    print("Checking Facebook login status...")
    page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=15000)
    page.wait_for_timeout(2000)

    if page.locator('input[name="email"]').count() > 0:
        if not sys.stdin.isatty():
            raise RuntimeError(
                "Not logged into Facebook and running in non-interactive mode. "
                "Run the script manually in a terminal to log in first."
            )
        print("\n⚠ Not logged into Facebook.")
        print("Please log in manually in the browser window, then press Enter here to continue...")
        input()
        page.wait_for_selector('input[name="email"]', state="hidden", timeout=60000)
        print("Login detected, continuing...\n")
    else:
        print("Already logged in, continuing...\n")
