"""
Facebook Ads Library scraper.
Searches for advertisers by keyword, deduplicates by Facebook page,
checks actual ad counts per business, and outputs a CSV.

Usage: python ads_library_scraper.py "<search query>" --country "<country_code>"
Example: python ads_library_scraper.py "clothing stores" --country "PK"
"""

import csv
import re
import sys
import urllib.parse
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from common import launch_browser, clean_name, is_real_website, SOCIAL_DOMAINS

# ── Config ────────────────────────────────────────────────────────────────────

MAX_SCROLL_RETRIES = 5
SCROLL_WAIT_MS = 2000
MAX_ADS_PER_BUSINESS = 5
FB_PAGE_TIMEOUT = 30000

# Domains that appear on Facebook pages but aren't business websites
NON_BUSINESS_DOMAINS = SOCIAL_DOMAINS + (
    "bing.com", "google.com", "apple.com", "facebook.com",
    "messenger.com", "fb.com", "fb.me", "m.me",
    "l.facebook.com", "lm.facebook.com",
)

# XPath: stable text-based selector for the "See ad details" button
SEE_DETAILS_XPATH = (
    '//div[@role="button" and '
    '(contains(., "See ad details") or contains(., "See summary details"))]'
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_url(url: str) -> str:
    if not url:
        return ""
    try:
        return urllib.parse.unquote(url)
    except Exception:
        return url


def unwrap_facebook_url(href: str) -> str:
    """Extract real URL from a Facebook l.php redirect wrapper."""
    if not href or "l.facebook.com/l.php?u=" not in href:
        return href
    m = re.search(r"l\.php\?u=([^&]+)", href)
    if m:
        return clean_url(m.group(1))
    return href


def _get_outer_card(button):
    """Climb 3 parent levels from the 'See ad details' button to the outer card."""
    card = button
    for _ in range(3):
        parent = card.locator("xpath=..")
        card = parent
    return card


def _extract_card_data(card) -> dict | None:
    """Extract data from a single ad card using stable text/role selectors."""
    try:
        # Page name from profile image (non-empty alt is an accessibility requirement)
        page_img = card.locator('img[alt]:not([alt=""])').first
        page_name = ""
        try:
            if page_img.count() > 0:
                page_name = page_img.get_attribute("alt") or ""
        except Exception:
            pass

        # Page link — the <a> whose text matches the page name
        page_link_href = ""
        for a in card.locator("a").all():
            try:
                text = (a.text_content() or "").strip()
                href = (a.get_attribute("href") or "").strip()
                if text and text == page_name and "facebook.com" in href:
                    page_link_href = href.split("?")[0].rstrip("/")
                    break
            except Exception:
                continue

        # Library ID from a span containing "Library ID:"
        library_id = ""
        for span in card.locator("span").all():
            try:
                txt = span.text_content() or ""
                m = re.search(r"Library ID:\s*(\d+)", txt)
                if m:
                    library_id = m.group(1)
                    break
            except Exception:
                continue

        # Status badge
        status = "Inactive"
        for span in card.locator("span").all():
            try:
                if span.text_content().strip() == "Active":
                    status = "Active"
                    break
            except Exception:
                continue

        # Instagram / Website from target=_blank links
        instagram = ""
        website = ""
        for a in card.locator('a[target="_blank"]').all():
            try:
                href = unwrap_facebook_url(a.get_attribute("href") or "")
                if not href or not href.startswith("http"):
                    continue
                href_clean = href.split("?")[0].rstrip("/")
                if not instagram and "instagram.com" in href_clean:
                    instagram = href_clean
                elif not website and "facebook.com" not in href_clean and is_real_website(href_clean):
                    website = href_clean
            except Exception:
                continue

        return {
            "page_name": page_name,
            "page_url": page_link_href,
            "library_id": library_id,
            "status": status,
            "instagram": instagram,
            "website": website,
        }
    except Exception:
        return None


def _clean_fb_url(href: str) -> str | None:
    """Strip tracking params and unwrap Facebook l.php redirect URLs."""
    if not href:
        return None
    href = href.strip()
    if "l.facebook.com" in href or "lm.facebook.com" in href:
        m = re.search(r'[?&]u=([^&]+)', href)
        if m:
            href = urllib.parse.unquote(m.group(1))
        else:
            return None
    return href.split("?")[0].rstrip("/")


def _scrape_facebook_page(fb_url: str, page) -> dict:
    """Visit a Facebook page and extract: page_id, website, phone, email, instagram.
    Uses XPath selectors keyed on stable text/role attributes — no CSS classes."""
    result = {
        "page_id": "",
        "website": "",
        "phone": "",
        "email": "",
        "instagram": "",
    }

    try:
        page.goto(fb_url, wait_until="load", timeout=FB_PAGE_TIMEOUT)

        # Scroll to trigger lazy-loading of About sections
        page.evaluate("window.scrollBy(0, 800)")
        page.wait_for_timeout(400)

        about_found = False
        for _ in range(2):
            try:
                page.wait_for_selector(
                    '//span[text()="Links"] | //span[text()="Contact info"] | //span[text()="Details"]',
                    timeout=6000,
                )
                about_found = True
                break
            except PlaywrightTimeout:
                page.evaluate("window.scrollBy(0, 600)")
                page.wait_for_timeout(400)

        if not about_found:
            for selector in ['div[role="main"]', 'div[role="article"]']:
                try:
                    page.wait_for_selector(selector, timeout=5000)
                    about_found = True
                    break
                except PlaywrightTimeout:
                    continue

        page.wait_for_timeout(500)
        html = page.content()

        # --- Extract page ID from HTML ---
        for pat in [
            r'"profile_header_renderer"[\s\S]*?"delegate_page"\s*:\s*\{[^}]*"id"\s*:\s*"(\d+)"',
            r'"header_top_row"[\s\S]*?"delegate_page"\s*:\s*\{[^}]*"id"\s*:\s*"(\d+)"',
            r'"delegate_page"\s*:\s*\{[^}]*"id"\s*:\s*"(\d+)"',
            r'fb://profile/(\d+)',
            r'"pageID"\s*:\s*(\d+)',
        ]:
            m = re.search(pat, html)
            if m:
                result["page_id"] = m.group(1)
                break

        # --- XPath selectors for contact fields ---
        xpaths = {
            "website": (
                '//div[@role="list" and @aria-labelledby='
                '//h2[.//span[text()="Links"]]/span/@id]//a'
            ),
            "email": (
                '//div[@role="list" and @aria-labelledby='
                '//h2[.//span[text()="Contact info"]]/span/@id]'
                '//a[starts-with(@href,"mailto:")]'
            ),
            "instagram": (
                '//div[@role="list" and @aria-labelledby='
                '//h2[.//span[text()="Contact info"]]/span/@id]'
                '//a[contains(@href,"instagram.com/")]'
            ),
        }

        for field, xpath in xpaths.items():
            try:
                locator = page.locator(f"xpath={xpath}")
                if locator.count() > 0:
                    value = locator.first.get_attribute("href")
                    if value:
                        value = value.strip()
                        if field == "email" and value.startswith("mailto:"):
                            value = value.replace("mailto:", "").split("?")[0]
                        elif field in ("website", "instagram"):
                            value = _clean_fb_url(value)
                        if value:
                            result[field] = value
            except Exception:
                pass

        # --- Phone: scan text near "Contact info" heading ---
        phone_patterns = [
            r'\+\d{1,3}\s?\d[\d\s\-\(\)]{6,}',
            r'\d{3,5}\s\d[\d\s\-]{5,}',
            r'\+\d{10,15}',
            r'\d{10,12}',
        ]
        try:
            contact_heading = page.locator('//span[text()="Contact info"]')
            if contact_heading.count() > 0:
                container = contact_heading.first
                for _ in range(6):
                    container = container.locator("xpath=..")
                section_text = container.first.inner_text()
                for pat in phone_patterns:
                    m = re.search(pat, section_text)
                    if m:
                        result["phone"] = m.group(0).strip()
                        break
        except Exception:
            pass

        if not result["phone"]:
            try:
                for span in page.locator('span[dir="auto"]').all():
                    try:
                        text = span.inner_text().strip()
                        for pat in phone_patterns:
                            m = re.match(pat, text)
                            if m and len(text) < 25:
                                result["phone"] = m.group(0).strip()
                                break
                        if result["phone"]:
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        # --- Fallback: scan all nofollow links for missing fields ---
        missing = [k for k in ("website", "instagram", "email") if not result[k]]
        if missing:
            for a in page.locator('a[rel="nofollow noreferrer"]').all():
                href = (a.get_attribute("href") or "").strip()
                if not href:
                    continue

                if "email" in missing and href.startswith("mailto:"):
                    result["email"] = href.replace("mailto:", "").split("?")[0].strip()
                    missing.remove("email")
                    continue

                if not href.startswith("http"):
                    continue

                href_clean = _clean_fb_url(href)
                if not href_clean:
                    continue

                if "instagram" in missing and "instagram.com" in href_clean:
                    result["instagram"] = href_clean
                    missing.remove("instagram")
                    continue

                if "website" in missing and not any(d in href_clean for d in NON_BUSINESS_DOMAINS):
                    result["website"] = href_clean
                    missing.remove("website")
                    continue

                if not missing:
                    break

    except Exception as e:
        print(f"    Error loading Facebook page: {e}")
        sys.stdout.flush()

    return result


def _scrape_website_footer(website: str, page) -> dict:
    """Visit a business website and extract Instagram + email from footer links."""
    result = {"instagram": "", "email": ""}

    if not website.startswith("http"):
        website = "https://" + website

    try:
        page.goto(website, wait_until="domcontentloaded", timeout=FB_PAGE_TIMEOUT)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)

        for a in page.locator("a").all():
            href = (a.get_attribute("href") or "").strip()
            if not href:
                continue

            if not result["email"] and href.startswith("mailto:"):
                result["email"] = href.replace("mailto:", "").split("?")[0].strip()
                continue

            if not href.startswith("http"):
                continue

            href_clean = href.split("?")[0].rstrip("/")

            if not result["instagram"] and "instagram.com" in href_clean:
                result["instagram"] = href_clean

        # Fallback: plain-text email in body
        if not result["email"]:
            try:
                body_text = page.locator("body").inner_text()
                m = re.search(
                    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
                    body_text,
                )
                if m:
                    result["email"] = m.group(0)
            except Exception:
                pass

    except Exception as e:
        print(f"    Website scrape failed ({website}): {e}")
        sys.stdout.flush()

    return result


def _count_ads_for_page(page_id: str, check_page) -> dict:
    """Navigate to the business's Ad Library page and count ads.
    Scrolls only enough to determine if count exceeds MAX_ADS_PER_BUSINESS."""
    url = (
        "https://www.facebook.com/ads/library/"
        "?active_status=active&ad_type=all&country=ALL"
        "&is_targeted_country=false&media_type=all"
        f"&view_all_page_id={page_id}"
    )

    print(f"    => Checking {url}")
    sys.stdout.flush()

    try:
        check_page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"    goto failed: {e}")
        return {"total": 0, "active": 0}

    # Wait for Library ID spans to appear (signals ad cards loaded)
    try:
        check_page.wait_for_selector('span:has-text("Library ID:")', timeout=8000)
    except PlaywrightTimeout:
        return {"total": 0, "active": 0}

    def _visible_ads() -> dict:
        spans = check_page.locator('span:has-text("Library ID:")')
        ads = {}
        for i in range(spans.count()):
            try:
                txt = spans.nth(i).inner_text()
                m = re.search(r"Library ID: (\d+)", txt)
                if not m:
                    continue
                ad_id = m.group(1)
                # Determine active status from nearby text
                container = spans.nth(i).locator(
                    'xpath=ancestor::div[contains(@class, "x1plvlek")]'
                ).first
                if container.count() > 0:
                    ads[ad_id] = "Active" in container.text_content()
                else:
                    ads[ad_id] = True  # assume active if can't determine
            except Exception:
                continue
        return ads

    all_ads = _visible_ads()

    if len(all_ads) <= MAX_ADS_PER_BUSINESS:
        for _ in range(3):
            prev = len(all_ads)
            check_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            check_page.wait_for_timeout(800)
            all_ads.update(_visible_ads())
            if len(all_ads) == prev or len(all_ads) > MAX_ADS_PER_BUSINESS:
                break

    total = len(all_ads)
    active = sum(1 for v in all_ads.values() if v)
    print(f"    => {total} total, {active} active")
    sys.stdout.flush()
    return {"total": total, "active": active}


# ── Main scraper ──────────────────────────────────────────────────────────────

def scrape_ads_library(query: str, country: str = "PK", max_results: int = 10) -> list[dict]:
    """Search Facebook Ads Library, verify actual ad counts per business,
    and return only businesses with <= MAX_ADS_PER_BUSINESS ads."""

    encoded = urllib.parse.quote(query)
    url = (
        "https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&country={country}"
        "&is_targeted_country=false&media_type=all"
        f"&q={encoded}&search_type=keyword_exact_phrase"
        "&sort_data[mode]=relevancy_monthly_grouped&sort_data[direction]=desc"
    )

    print(f"Ad Library URL: {url}")

    results: list[dict] = []
    seen_page_names: set[str] = set()
    checked_page_ids: set[str] = set()
    card_index = 0
    scroll_retries = 0
    skipped_over_limit = 0

    with sync_playwright() as p:
        context = launch_browser(p)
        page = context.new_page()
        check_page = context.new_page()

        print(f"Loading: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Wait for the first "See ad details" button to appear
        try:
            page.wait_for_selector(f"xpath={SEE_DETAILS_XPATH}", timeout=15000)
        except PlaywrightTimeout:
            if page.get_by_role("heading", name="No ads match your search").count() > 0:
                print("No ads found for this query.")
            else:
                print("Could not load ad cards.")
            check_page.close()
            context.close()
            return []

        while len(results) < max_results:
            # Find all card wrappers via the "See ad details" buttons
            buttons = page.locator(f"xpath={SEE_DETAILS_XPATH}").all()
            cards = [_get_outer_card(b) for b in buttons]

            while card_index < len(cards) and len(results) < max_results:
                card = cards[card_index]
                card_index += 1

                data = _extract_card_data(card)
                if not data or not data["page_name"]:
                    continue

                page_name = clean_name(data["page_name"])
                fb_url = data["page_url"]

                if not page_name or page_name in seen_page_names:
                    continue
                seen_page_names.add(page_name)

                if not fb_url:
                    print(f"  No Facebook URL for {page_name} -- skipping")
                    sys.stdout.flush()
                    continue

                # --- Step 1: Scrape Facebook page for page ID + socials ---
                print(f"  Checking {page_name}")
                sys.stdout.flush()

                fb_data = _scrape_facebook_page(fb_url, check_page)
                page_id = fb_data["page_id"]

                if not page_id:
                    print(f"  No numeric page ID for {page_name} ({fb_url}) -- skipping")
                    sys.stdout.flush()
                    continue

                if page_id in checked_page_ids:
                    continue
                checked_page_ids.add(page_id)

                # Merge: card data is fallback, Facebook data takes priority
                instagram = fb_data["instagram"] or data["instagram"]
                website = fb_data["website"] or data["website"]
                email = fb_data["email"]
                phone = fb_data["phone"]

                # --- Step 2: If we got a website from Facebook, scrape it for gaps ---
                if website and (not instagram or not email):
                    print(f"    Scraping website: {website}")
                    sys.stdout.flush()
                    site_data = _scrape_website_footer(website, check_page)
                    if not instagram and site_data["instagram"]:
                        instagram = site_data["instagram"]
                    if not email and site_data["email"]:
                        email = site_data["email"]

                # --- Step 3: Check ad counts ---
                print(f"    page_id={page_id}")
                sys.stdout.flush()

                counts = _count_ads_for_page(page_id, check_page)
                total_ads = counts["total"]
                active_ads = counts["active"]

                if total_ads > MAX_ADS_PER_BUSINESS:
                    skipped_over_limit += 1
                    print(f"  X {page_name}: {total_ads} ads (> {MAX_ADS_PER_BUSINESS}) -- skip")
                    sys.stdout.flush()
                    continue

                ads_library_url = (
                    "https://www.facebook.com/ads/library/"
                    "?active_status=active&ad_type=all&country=ALL"
                    "&is_targeted_country=false&media_type=all"
                    f"&view_all_page_id={page_id}"
                )

                biz = {
                    "name": page_name,
                    "facebook_url": fb_url,
                    "instagram": instagram,
                    "website": website,
                    "email": email,
                    "phone": phone,
                    "ads_library_id": page_id,
                    "ads_library_url": ads_library_url,
                    "total_ads": total_ads,
                    "active_ads": active_ads,
                }

                results.append(biz)
                print(f"  [OK] [{len(results)}/{max_results}] {page_name}: {total_ads} ads -- saved")
                sys.stdout.flush()

            if len(results) >= max_results:
                break

            # Single scroll to load another batch
            prev_button_count = len(buttons)
            page.evaluate("window.scrollBy(0, 2000)")
            page.wait_for_timeout(SCROLL_WAIT_MS)
            new_buttons = page.locator(f"xpath={SEE_DETAILS_XPATH}").all()

            if len(new_buttons) == prev_button_count:
                scroll_retries += 1
                if scroll_retries >= 3:
                    print("No more cards loading -- stopping.")
                    break
            else:
                scroll_retries = 0

        check_page.close()
        context.close()

    if skipped_over_limit > 0:
        print(f"\nSkipped {skipped_over_limit} businesses with >{MAX_ADS_PER_BUSINESS} ads")
    print(f"Collected {len(results)} businesses with <= {MAX_ADS_PER_BUSINESS} ads")

    return results


# ── Output ────────────────────────────────────────────────────────────────────

def save_csv(rows: list[dict], query: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = query.replace(" ", "_")[:40]
    filename = f"adslibrary_{slug}_{timestamp}.csv"

    fields = [
        "name", "facebook_url", "instagram", "website", "email",
        "phone", "ads_library_id", "ads_library_url", "total_ads", "active_ads",
    ]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved {len(rows)} businesses to {filename}")
    return filename


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print('Usage: python ads_library_scraper.py "<search query>" [--country "<code>"] [--max N]')
        print('Example: python ads_library_scraper.py "clothing stores" --country "PK" --max 10')
        sys.exit(1)

    query = sys.argv[1]
    country = "PK"
    max_results = 10
    for i, arg in enumerate(sys.argv):
        if arg == "--country" and i + 1 < len(sys.argv):
            country = sys.argv[i + 1].upper()
        if arg == "--max" and i + 1 < len(sys.argv):
            max_results = int(sys.argv[i + 1])

    print(f'Searching Ad Library for: "{query}" in {country} (max {max_results})')

    rows = scrape_ads_library(query, country, max_results=max_results)
    if rows:
        filename = save_csv(rows, query)
        print(f"Output: {filename}")
    else:
        print("No businesses found.")


if __name__ == "__main__":
    main()
