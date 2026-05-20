import os
import re
import sys
import time
import random
from urllib.parse import unquote

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from common import (
    is_valid_facebook_page_url, is_real_website, clean_name,
    SOCIAL_DOMAINS, launch_browser, ensure_logged_in,
)

FB_PAGE_TIMEOUT = 30000  # generous timeout for slow connections


# ── Website scraping ──────────────────────────────────────────────────────────

def scrape_website_contacts(website: str, page) -> dict:
    """
    Navigate to the business website, scroll to the bottom, and extract
    Facebook, Instagram, and email links from the footer.
    """
    result = {"facebook_url": None, "instagram": None, "email": None}

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

            # Email via mailto: link
            if href.startswith("mailto:") and not result["email"]:
                result["email"] = href.replace("mailto:", "").split("?")[0].strip()
                continue

            if not href.startswith("http"):
                continue

            href_clean = href.split("?")[0].rstrip("/")

            if "facebook.com" in href_clean and not result["facebook_url"]:
                if is_valid_facebook_page_url(href_clean):
                    result["facebook_url"] = href_clean

            if "instagram.com" in href_clean and not result["instagram"]:
                result["instagram"] = href_clean

        # Fallback: scan page text for plain-text emails not wrapped in a link
        if not result["email"]:
            try:
                body_text = page.locator("body").inner_text()
                m = re.search(
                    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
                    body_text
                )
                if m:
                    result["email"] = m.group(0)
            except Exception:
                pass

    except Exception as e:
        print(f"    Website scrape failed ({website}): {e}")

    return result


# ── Facebook page finding ─────────────────────────────────────────────────────

def find_facebook_via_search(name: str, city: str, page) -> str | None:
    """Search Facebook's pages directory for the business."""
    query = f"{clean_name(name)} {city}".replace(" ", "%20")
    url = f"https://www.facebook.com/search/pages/?q={query}"

    try:
        print(f"    Searching: {url}")
        page.goto(url, wait_until="load", timeout=FB_PAGE_TIMEOUT)
        # Wait for search results to actually render (slow connections need more time)
        page.wait_for_selector('div[role="article"]', timeout=20000)
        page.wait_for_timeout(300)  # let any late renders settle

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


# ── Facebook page scraping (XPath-based contact info + page ID) ───────────────

def scrape_facebook_contacts(facebook_url: str, page) -> dict:
    """
    Load a Facebook page and extract:
      - ads_library_id (numeric page ID from raw HTML)
      - website, phone, email, instagram (via semantic XPath selectors)
    """
    result = {
        "ads_library_id": None,
        "website": None,
        "phone": None,
        "email": None,
        "instagram": None,
    }

    # Utility/map domains that appear on Facebook pages but aren't business sites
    NON_BUSINESS_DOMAINS = SOCIAL_DOMAINS + (
        "bing.com", "google.com", "apple.com", "facebook.com",
        "messenger.com", "fb.com", "fb.me", "m.me",
        "l.facebook.com", "lm.facebook.com",
    )

    try:
        print(f"    Loading Facebook page...")
        page.goto(facebook_url, wait_until="load", timeout=FB_PAGE_TIMEOUT)

        # Scroll to trigger lazy-loading of About sections, then wait for
        # the specific headings we need (exits the moment they appear).
        page.evaluate("window.scrollBy(0, 800)")
        page.wait_for_timeout(400)

        about_found = False
        for _ in range(2):
            try:
                page.wait_for_selector(
                    '//span[text()="Links"] | //span[text()="Contact info"] | //span[text()="Details"]',
                    timeout=6000
                )
                about_found = True
                break
            except PlaywrightTimeout:
                page.evaluate("window.scrollBy(0, 600)")
                page.wait_for_timeout(400)

        if not about_found:
            # Fallback: wait for any main content
            for selector in ['div[role="main"]', 'div[role="article"]']:
                try:
                    page.wait_for_selector(selector, timeout=5000)
                    about_found = True
                    break
                except PlaywrightTimeout:
                    continue

        if not about_found:
            print("    Page content didn't load — continuing anyway")

        page.wait_for_timeout(500)
        html = page.content()

        # Extract numeric page ID from raw HTML
        for pattern in [
            r'"delegate_page"\s*:\s*{[^}]*"id"\s*:\s*"(\d+)"',
            r'fb://profile/(\d+)',
            r'"pageID"\s*:\s*(\d+)',
        ]:
            m = re.search(pattern, html)
            if m:
                result["ads_library_id"] = m.group(1)
                break

        if not result["ads_library_id"]:
            print("    No numeric page ID found in HTML")

        # Try XPath selectors first (select the element, not the attribute)
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
                            value = _clean_url(value)
                        if value:
                            result[field] = value
                            print(f"    Found {field}: {value}")
            except Exception as e:
                print(f"    XPath failed for {field}: {e}")

        # Phone: try multiple approaches since Facebook markup varies
        phone_patterns = [
            r'\+\d{1,3}\s?\d[\d\s\-\(\)]{6,}',  # +91 87002 98264
            r'\d{3,5}\s\d[\d\s\-]{5,}',           # 0305 2999269
            r'\+\d{10,15}',                         # +918700298264
            r'\d{10,12}',                           # 03052999269
        ]
        try:
            # Method 1: Find Contact info heading, then scan nearby spans
            contact_heading = page.locator('//span[text()="Contact info"]')
            if contact_heading.count() > 0:
                # Walk up to a reasonable container (5 levels up)
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
            # Method 2: Scan every span[dir="auto"] for phone-like content
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

        if not result["phone"]:
            # Method 3: Full body regex fallback
            try:
                body_text = page.locator("body").inner_text()
                for pat in phone_patterns:
                    m = re.search(pat, body_text)
                    if m:
                        result["phone"] = m.group(0).strip()
                        break
            except Exception:
                pass

        if result["phone"]:
            print(f"    Found phone: {result['phone']}")

        # Fallback: scan all nofollow links. Order matters — bing.com/maps links
        # from the Details section appear before the real website in Links, so we
        # must filter out utility/map domains.
        missing = [k for k in ("website", "instagram", "email") if not result[k]]
        if missing:
            for a in page.locator('a[rel="nofollow noreferrer"]').all():
                href = (a.get_attribute("href") or "").strip()
                if not href:
                    continue

                if "email" in missing and href.startswith("mailto:"):
                    result["email"] = href.replace("mailto:", "").split("?")[0].strip()
                    print(f"    Found email (fallback): {result['email']}")
                    missing.remove("email")
                    continue

                if not href.startswith("http"):
                    continue

                href_clean = _clean_url(href)
                if not href_clean:
                    continue

                if "instagram" in missing and "instagram.com" in href_clean:
                    result["instagram"] = href_clean
                    print(f"    Found Instagram (fallback): {href_clean}")
                    missing.remove("instagram")
                    continue

                if "website" in missing and not any(domain in href_clean for domain in NON_BUSINESS_DOMAINS):
                    result["website"] = href_clean
                    print(f"    Found website (fallback): {href_clean}")
                    missing.remove("website")
                    continue

                if not missing:
                    break

    except Exception as e:
        print(f"    Error loading Facebook page: {e}")

    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_url(href: str) -> str | None:
    """Strip tracking params and unwrap Facebook l.php redirect URLs."""
    if not href:
        return None
    href = href.strip()

    # Facebook link redirector — extract the real URL from the 'u' param
    if "l.facebook.com" in href or "lm.facebook.com" in href:
        m = re.search(r'[?&]u=([^&]+)', href)
        if m:
            href = unquote(m.group(1))
        else:
            return None  # can't extract real URL, skip it

    return href.split("?")[0].rstrip("/")


def _has_value(val) -> bool:
    """Check if a value is non-empty."""
    if val is None:
        return False
    if isinstance(val, float) and pd.isna(val):
        return False
    return bool(str(val).strip())


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: python social_scraper.py <csv_file_path> --city <city_name>")
        sys.exit(1)

    csv_path = sys.argv[1]

    city = "Faisalabad"  # default
    for i, arg in enumerate(sys.argv):
        if arg == "--city" and i + 1 < len(sys.argv):
            city = sys.argv[i + 1]
            break
    if not os.path.exists(csv_path):
        print(f"Error: CSV file '{csv_path}' not found.")
        sys.exit(1)

    print(f"Processing: {csv_path}")
    df = pd.read_csv(csv_path, dtype={"phone": str})

    # Ensure required columns exist
    for col in ("facebook_url", "website", "instagram"):
        if col not in df.columns:
            df[col] = ""
        else:
            df[col] = df[col].fillna("").astype(str)

    for col in ("email", "ads_library_id", "phone"):
        if col not in df.columns:
            df[col] = ""
        else:
            df[col] = df[col].fillna("").astype(str)

    with sync_playwright() as p:
        context = launch_browser(p)
        page = context.new_page()
        ensure_logged_in(page)

        for idx, row in df.iterrows():
            name        = row["name"]
            raw_website = row.get("website", "")
            website     = "" if pd.isna(raw_website) else str(raw_website).strip()

            # Rescue FB URLs mistakenly stored in website field
            if website and "facebook.com" in website and is_valid_facebook_page_url(website):
                print(f"\n[{idx+1}/{len(df)}] {name}")
                print(f"  Website field is a Facebook URL, moving to facebook_url")
                df.at[idx, "facebook_url"] = website
                website = ""
                df.at[idx, "website"] = ""
            elif not is_real_website(website):
                website = ""

            print(f"\n[{idx+1}/{len(df)}] {name}")

            # ── Step 1: Scrape website for contacts (if website exists) ────────
            if website:
                print(f"  Scraping website: {website}")
                site_data = scrape_website_contacts(website, page)

                # Save whatever we found, FB will fill in gaps
                if site_data.get("facebook_url"):
                    df.at[idx, "facebook_url"] = site_data["facebook_url"]
                    print(f"  Found Facebook on website: {site_data['facebook_url']}")
                if site_data.get("instagram"):
                    df.at[idx, "instagram"] = site_data["instagram"]
                    print(f"  Found Instagram on website: {site_data['instagram']}")
                if site_data.get("email"):
                    df.at[idx, "email"] = site_data["email"]
                    print(f"  Found email on website: {site_data['email']}")

            # ── Step 2: Find Facebook page URL if we don't have one yet ────────
            facebook_url = row.get("facebook_url", "")
            facebook_url = "" if pd.isna(facebook_url) else str(facebook_url).strip()

            if facebook_url and not is_valid_facebook_page_url(facebook_url):
                print(f"  Discarding bad existing FB URL: {facebook_url}")
                facebook_url = ""

            # Re-check from df since website scrape may have set it
            if not facebook_url:
                df_fb = df.at[idx, "facebook_url"]
                if df_fb and str(df_fb).strip():
                    facebook_url = str(df_fb).strip()
                    if not is_valid_facebook_page_url(facebook_url):
                        facebook_url = ""

            if not facebook_url:
                print(f"  Searching Facebook for page...")
                facebook_url = find_facebook_via_search(name, city, page) or ""
                if facebook_url:
                    print(f"  Found via FB search: {facebook_url}")

            if not facebook_url:
                print("  No Facebook page found — skipping")
                df.to_csv(csv_path, index=False)
                continue

            df.at[idx, "facebook_url"] = facebook_url

            # ── Step 3: Scrape Facebook page for page ID + missing contacts ────
            # Always visit — fills in gaps that the website didn't have
            fb_data = scrape_facebook_contacts(facebook_url, page)

            if fb_data["ads_library_id"]:
                df.at[idx, "ads_library_id"] = fb_data["ads_library_id"]
                print(f"  Ads Library ID: {fb_data['ads_library_id']}")

            # Fill in missing fields from Facebook
            if fb_data["website"] and not _has_value(df.at[idx, "website"]):
                df.at[idx, "website"] = fb_data["website"]
                print(f"  Website from Facebook: {fb_data['website']}")

            if fb_data["instagram"] and not _has_value(df.at[idx, "instagram"]):
                df.at[idx, "instagram"] = fb_data["instagram"]
                print(f"  Instagram from Facebook: {fb_data['instagram']}")

            if fb_data["email"] and not _has_value(df.at[idx, "email"]):
                df.at[idx, "email"] = fb_data["email"]
                print(f"  Email from Facebook: {fb_data['email']}")

            if fb_data["phone"]:
                df.at[idx, "phone"] = fb_data["phone"]
                print(f"  Phone from Facebook: {fb_data['phone']}")

            # ── Step 4: If we got a new website from FB, scrape it for gaps ────
            new_website = df.at[idx, "website"]
            if new_website and new_website != website and is_real_website(new_website):
                missing = any(
                    not _has_value(df.at[idx, col])
                    for col in ("instagram", "email")
                )
                if missing:
                    print(f"  Scraping newly found website: {new_website}")
                    site_data = scrape_website_contacts(new_website, page)
                    if site_data.get("instagram") and not _has_value(df.at[idx, "instagram"]):
                        df.at[idx, "instagram"] = site_data["instagram"]
                        print(f"  Instagram from new website: {site_data['instagram']}")
                    if site_data.get("email") and not _has_value(df.at[idx, "email"]):
                        df.at[idx, "email"] = site_data["email"]
                        print(f"  Email from new website: {site_data['email']}")

            time.sleep(random.uniform(0.5, 1.5))
            df.to_csv(csv_path, index=False)

        context.close()

    df.to_csv(csv_path, index=False)
    print(f"\nDone. Saved to {csv_path}")
    print(f"Processed {len(df)} businesses")


if __name__ == "__main__":
    main()
