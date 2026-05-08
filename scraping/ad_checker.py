import os
import re
import sys
import time
import random

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from common import launch_browser, ensure_logged_in


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
                container = span.locator(
                    'xpath=ancestor::div[contains(@class, "x1plvlek")]'
                ).first
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

def main():
    if len(sys.argv) != 2:
        print("Usage: python ad_checker.py <csv_file_path>")
        sys.exit(1)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        print(f"Error: CSV file '{csv_path}' not found.")
        sys.exit(1)

    print(f"Processing: {csv_path}")
    df = pd.read_csv(csv_path, dtype={"phone": str})

    # Ensure required columns exist
    for col in ("ads_library_id",):
        if col not in df.columns:
            df[col] = ""
        else:
            df[col] = df[col].fillna("").astype(str)

    for col in ("total_ads", "active_ads"):
        if col not in df.columns:
            df[col] = None

    with sync_playwright() as p:
        context = launch_browser(p)
        page = context.new_page()
        ensure_logged_in(page)

        for idx, row in df.iterrows():
            name = row["name"]
            ads_library_id = row.get("ads_library_id", "")
            ads_library_id = "" if pd.isna(ads_library_id) else str(ads_library_id).strip()

            print(f"\n[{idx+1}/{len(df)}] {name}")

            if not ads_library_id:
                print("  No Ads Library ID — assuming no ads")
                df.at[idx, "total_ads"]  = 0
                df.at[idx, "active_ads"] = 0
                df.to_csv(csv_path, index=False)
                continue

            print(f"  Page ID: {ads_library_id}")
            counts = get_ad_counts(ads_library_id, page)
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


if __name__ == "__main__":
    main()
