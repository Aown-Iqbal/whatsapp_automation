import os
import re
import sys
import time
import random

import pandas as pd
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────

CITY        = "lahore"
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


# ── Facebook page finding ─────────────────────────────────────────────────────

def find_facebook_on_website(website: str, page) -> str | None:
    """Load the business website and look for a Facebook page link."""
    if not website.startswith("http"):
        website = "https://" + website
    try:
        page.goto(website, wait_until="domcontentloaded", timeout=15000)
        for a in page.locator('a[href*="facebook.com"]').all():
            href = (a.get_attribute("href") or "").split("?")[0].rstrip("/")
            if is_valid_facebook_page_url(href):
                return href
    except Exception as e:
        print(f"    Website scrape failed ({website}): {e}")
    return None


def find_facebook_via_search(name: str, page) -> str | None:
    """
    Search Facebook's pages search for the business and return the first result URL.
    Requires a logged-in session via persistent Chromium profile.
    """
    query = f"{clean_name(name)} {CITY}".replace(" ", "%20")
    url = f"https://www.facebook.com/search/pages/?q={query}"

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector('div[role="article"]', timeout=10000)

        for article in page.locator('div[role="article"]').all():
            for a in article.locator("a[href*='facebook.com']").all():
                href = (a.get_attribute("href") or "").split("?")[0].rstrip("/")
                if is_valid_facebook_page_url(href):
                    return href

    except PlaywrightTimeout:
        print("    Facebook search timed out — no results")
    except Exception as e:
        print(f"    Facebook search failed: {e}")

    return None


# ── Facebook page scraping (ID + website) ────────────────────────────────────

def scrape_facebook_page(facebook_url: str, page) -> dict:
    """
    Load the Facebook page and extract:
    - numeric page ID (for Ad Library)
    - website URL (from nofollow noreferrer links, filtering out social links)
    """
    result = {"page_id": None, "website": None}

    try:
        print(f"    Loading Facebook page...")
        page.goto(facebook_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)
        html = page.content()

        # Extract numeric page ID
        for pattern in [
            r'"delegate_page"\s*:\s*{[^}]*"id"\s*:\s*"(\d+)"',
            r'fb://profile/(\d+)',
            r'"pageID"\s*:\s*(\d+)',
        ]:
            m = re.search(pattern, html)
            if m:
                result["page_id"] = m.group(1)
                break

        if not result["page_id"]:
            print("    No numeric page ID found in HTML")

        # Extract website from nofollow noreferrer links, skip social platforms
        for a in page.locator('a[rel="nofollow noreferrer"]').all():
            href = (a.get_attribute("href") or "").strip()
            if not href or not href.startswith("http"):
                continue
            if any(domain in href for domain in SOCIAL_DOMAINS):
                continue
            result["website"] = href
            print(f"    Found website on Facebook: {href}")
            break

    except Exception as e:
        print(f"    Error loading Facebook page: {e}")

    return result


# ── Ad Library scraping ───────────────────────────────────────────────────────

def has_no_ads(page) -> bool:
    return page.get_by_role("heading", name="No ads match your search").count() > 0


def get_ad_counts(page_id: str, page) -> dict | None:
    url = (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=all&ad_type=all&country=ALL"
        f"&view_all_page_id={page_id}"
    )
    print(f"    Ad Library: {url}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        try:
            page.wait_for_selector('span:has-text("Library ID:")', timeout=10000)
        except PlaywrightTimeout:
            if has_no_ads(page):
                print("    No ads found")
                return {"total": 0, "active": 0}
            print("    Could not load ad cards — skipping")
            page.screenshot(path=f"debug_{page_id}.png")
            return None

        def collect_visible_ads() -> dict:
            spans = page.locator('span:has-text("Library ID:")')
            ads = {}
            for i in range(spans.count()):
                span = spans.nth(i)
                m = re.search(r'Library ID: (\d+)', span.inner_text())
                if not m:
                    continue
                ad_id = m.group(1)
                container = span.locator('xpath=ancestor::div[contains(@class, "x1plvlek")]').first
                if container.count() > 0:
                    ads[ad_id] = container
            return ads

        all_ads = collect_visible_ads()
        previous_count = 0

        for attempt in range(10):
            if len(all_ads) == previous_count:
                print(f"    No new ads after scroll {attempt}, stopping")
                break
            previous_count = len(all_ads)
            print(f"    Scroll {attempt + 1}, ads so far: {len(all_ads)}")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(3000)
            all_ads.update(collect_visible_ads())

        total = len(all_ads)
        active = sum(
            1 for container in all_ads.values()
            if container.locator('span:has-text("Active")').first.count() > 0
            and "Active" in container.locator('span:has-text("Active")').first.inner_text()
        )

        return {"total": total, "active": active}

    except Exception as e:
        print(f"    Error scraping Ad Library: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python ads_library_scraper.py <csv_file_path>")
        sys.exit(1)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        print(f"Error: CSV file '{csv_path}' not found.")
        sys.exit(1)

    print(f"Processing: {csv_path}")
    df = pd.read_csv(csv_path, dtype={"phone": str})

    for col in ("facebook_url", "website", "total_ads", "active_ads"):
        if col not in df.columns:
            df[col] = None

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
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
        page = context.new_page()

        for idx, row in df.iterrows():
            name        = row["name"]
            raw_website = row.get("website", "")
            website     = "" if pd.isna(raw_website) else str(raw_website).strip()

            # Discard junk websites, rescue FB URLs from website field
            if website and "facebook.com" in website and is_valid_facebook_page_url(website):
                print(f"\n[{idx+1}/{len(df)}] {name}")
                print(f"  Website field is a Facebook URL, using directly")
                df.at[idx, "facebook_url"] = website
                website = ""
                df.at[idx, "website"] = ""
            elif not is_real_website(website):
                website = ""

            print(f"\n[{idx+1}/{len(df)}] {name}")

            # ── Step 1: find Facebook page ────────────────────────────────────
            facebook_url = row.get("facebook_url", "")
            facebook_url = "" if pd.isna(facebook_url) else str(facebook_url).strip()

            if facebook_url and not is_valid_facebook_page_url(facebook_url):
                print(f"  Discarding bad existing FB URL: {facebook_url}")
                facebook_url = ""

            if not facebook_url and website:
                print(f"  Checking website: {website}")
                facebook_url = find_facebook_on_website(website, page) or ""
                if facebook_url:
                    print(f"  Found on website: {facebook_url}")

            if not facebook_url:
                print(f"  Searching Facebook...")
                facebook_url = find_facebook_via_search(name, page) or ""
                if facebook_url:
                    print(f"  Found via FB search: {facebook_url}")

            if not facebook_url:
                print("  No Facebook page found — assuming no ads")
                df.at[idx, "total_ads"]  = 0
                df.at[idx, "active_ads"] = 0
                df.to_csv(csv_path, index=False)
                continue

            df.at[idx, "facebook_url"] = facebook_url

            # ── Step 2: load Facebook page — get ID and website ───────────────
            fb_data = scrape_facebook_page(facebook_url, page)

            if fb_data["website"] and not website:
                print(f"  Enriched website from Facebook: {fb_data['website']}")
                df.at[idx, "website"] = fb_data["website"]

            if not fb_data["page_id"]:
                print("  Could not extract numeric ID — skipping Ad Library")
                df.at[idx, "total_ads"]  = 0
                df.at[idx, "active_ads"] = 0
                df.to_csv(csv_path, index=False)
                continue

            print(f"  Page ID: {fb_data['page_id']}")

            # ── Step 3: scrape Ad Library ─────────────────────────────────────
            counts = get_ad_counts(fb_data["page_id"], page)
            if counts:
                df.at[idx, "total_ads"]  = counts["total"]
                df.at[idx, "active_ads"] = counts["active"]
                print(f"  Ads — total: {counts['total']}, active: {counts['active']}")
            else:
                print("  Ad Library scrape failed — leaving blank")

            time.sleep(random.uniform(1.5, 3.0))
            df.to_csv(csv_path, index=False)

        context.close()

    df.to_csv(csv_path, index=False)
    print(f"\nDone. Saved to {csv_path}")
    print(f"Processed {len(df)} businesses")