import csv
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────

MAX_SCROLL_RETRIES  = 5     # how many times to scroll with no new articles before quitting
SCROLL_WAIT_MS      = 2500  # wait after each scroll
PANEL_TIMEOUT_MS    = 8000  # max wait for info panel to appear after clicking
BETWEEN_CLICKS_MS   = 1500  # pause between clicking articles

# Domains that Google Maps puts in the website field that aren't real websites
JUNK_WEBSITE_DOMAINS = (
    "facebook.com",
    "instagram.com",
    "wa.me",
    "whatsapp.com",
    "oladoc.com",
    "twitter.com",
    "youtube.com",
    "tiktok.com",
    "linkedin.com",
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_phone(raw: str) -> str:
    """Strip non-digit characters except leading +, return as plain string."""
    if not raw:
        return ""
    raw = raw.strip()
    digits = "".join(c for c in raw if c.isdigit() or (c == "+" and raw.index(c) == 0))
    return digits


def clean_website(url: str) -> str:
    """Return empty string if the URL is a social/junk link, else return as-is."""
    if not url:
        return ""
    if any(domain in url for domain in JUNK_WEBSITE_DOMAINS):
        return ""
    return url.strip()


# ── Data extraction ───────────────────────────────────────────────────────────

def extract_panel(page) -> dict:
    """
    Wait for the info panel and extract all fields.
    Returns a dict. Any field not found is an empty string.
    """
    page.wait_for_selector('div[aria-label^="Information for "]', timeout=PANEL_TIMEOUT_MS)
    panel = page.locator('div[aria-label^="Information for "]').first

    name = panel.get_attribute("aria-label").replace("Information for ", "").strip()

    # Address
    address = ""
    addr_btn = panel.locator('button[data-item-id="address"]')
    if addr_btn.count():
        raw = addr_btn.get_attribute("aria-label") or ""
        address = raw.replace("Address:", "").strip().rstrip(" ")

    # Phone — prefer tel: href, fall back to aria-label text
    phone = ""
    phone_anchor = panel.locator('a[href^="tel:"]')
    if phone_anchor.count():
        raw = phone_anchor.first.get_attribute("href").replace("tel:", "").strip()
        phone = clean_phone(raw)
    else:
        phone_btn = panel.locator('button[data-item-id^="phone"]')
        if phone_btn.count():
            raw = (phone_btn.get_attribute("aria-label") or "").replace("Phone:", "").strip()
            phone = clean_phone(raw)

    # Website — filter out junk domains
    website = ""
    site_link = panel.locator('a[data-item-id="authority"]')
    if site_link.count():
        raw = site_link.first.get_attribute("href") or ""
        website = clean_website(raw)

    # Open/closed status
    open_status = ""
    status_el = panel.locator(".ZDu9vd").first
    if status_el.count():
        open_status = status_el.inner_text().strip()

    # Plus code
    plus_code = ""
    plus_btn = panel.locator('button[data-item-id="oloc"]')
    if plus_btn.count():
        raw = plus_btn.get_attribute("aria-label") or ""
        plus_code = raw.replace("Plus code:", "").strip().rstrip(" ")

    # Rating and review count — lives outside the Information div, in div.F7nice
    rating = ""
    review_count = ""
    ratings_div = page.locator("div.F7nice").first
    if ratings_div.count():
        rating_span = ratings_div.locator("span[aria-hidden=true]").first
        if rating_span.count():
            rating = rating_span.inner_text().strip()
        review_span = ratings_div.locator("span[role=img][aria-label*=reviews]").first
        if review_span.count():
            raw = review_span.get_attribute("aria-label") or ""
            review_count = raw.replace("reviews", "").strip().strip("(").strip(")").strip()

    return {
        "name":        name,
        "phone":       phone,
        "address":     address,
        "website":     website,
        "open_status": open_status,
        "plus_code":   plus_code,
        "rating":      rating,
        "review_count": review_count,
    }


# ── Main scraper ──────────────────────────────────────────────────────────────

def scrape_maps(query: str) -> list[dict]:
    results: list[dict] = []
    seen_articles: set[str] = set()   # dedup by article aria-label during scraping
    seen_phones: set[str]   = set()   # dedup by phone at save time

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--start-maximized"]
        )
        context = browser.new_context(
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        print(f"Navigating to: {url}")
        page.goto(url, wait_until="domcontentloaded")

        # Wait for the results feed
        page.wait_for_selector('div[role="feed"]', timeout=15000)
        feed = page.locator('div[role="feed"]')

        scroll_retries = 0

        while True:
            articles = page.locator('div[role="feed"] div[role="article"]').all()
            new_this_round = 0

            for article in articles:
                try:
                    aria = article.get_attribute("aria-label") or ""
                except Exception:
                    continue

                if aria in seen_articles:
                    continue
                seen_articles.add(aria)

                try:
                    article.click()
                    page.wait_for_timeout(BETWEEN_CLICKS_MS)
                    data = extract_panel(page)
                except PlaywrightTimeout:
                    print(f"  Timeout waiting for panel: {aria}")
                    continue
                except Exception as e:
                    print(f"  Error on '{aria}': {e}")
                    continue

                if not data["name"]:
                    continue

                # Deduplicate by phone number (fall back to name if no phone)
                phone_key = data["phone"] or data["name"]
                if phone_key in seen_phones:
                    print(f"  Skipping duplicate: {data['name']}")
                    continue
                seen_phones.add(phone_key)

                results.append(data)
                new_this_round += 1
                print(f"  [{len(results)}] {data['name']} | {data['phone']} | {data['address'][:40]}")

            if new_this_round == 0:
                scroll_retries += 1
                print(f"No new articles (retry {scroll_retries}/{MAX_SCROLL_RETRIES}), scrolling...")
                if scroll_retries >= MAX_SCROLL_RETRIES:
                    print("Done — no more results.")
                    break
            else:
                scroll_retries = 0

            feed.evaluate("el => el.scrollBy(0, 1500)")
            page.wait_for_timeout(SCROLL_WAIT_MS)

        browser.close()

    return results


# ── CSV writer ────────────────────────────────────────────────────────────────

def save_csv(rows: list[dict], query: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = query.replace(" ", "_")[:40]
    filename = f"maps_{slug}_{timestamp}.csv"

    fields = ["name", "phone", "address", "website", "open_status", "plus_code", "rating", "review_count"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved {len(rows)} rows to {filename}")
    return filename


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python maps_scraper.py <search query>")
        print('Example: python maps_scraper.py "electronics shops in lahore"')
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    print(f'Scraping: "{query}"')

    data = scrape_maps(query)
    if data:
        save_csv(data, query)
    else:
        print("No data collected.")