"""
Scrape foxen.com for knowledge base content.

Uses Selenium headless Chrome to bypass Cloudflare protection.
Discovers pages from sitemap and internal links, extracts content,
converts to markdown, and saves to data/scraped/.

Usage:
    poetry run python scripts/scrape_website.py
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify as md
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "data" / "scraped"

SITE_URL = "https://www.foxen.com"
SITEMAP_URL = f"{SITE_URL}/sitemap.xml"

REQUEST_DELAY = 5  # seconds between page loads (be polite)
CF_WAIT = 8  # seconds to wait for Cloudflare challenge to resolve


def create_driver() -> webdriver.Chrome:
    """Create headless Chrome driver with anti-detection for Cloudflare."""
    opts = Options()
    opts.add_argument("--headless=new")  # newer headless mode, better CF compat
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    opts.binary_location = "/usr/bin/chromium-browser"
    svc = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(options=opts, service=svc)
    # Hide webdriver property from JS detection
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": 'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
    })
    return driver


def fetch_page(driver: webdriver.Chrome, url: str) -> str:
    """Load a page, wait for Cloudflare challenge, return page source."""
    driver.get(url)
    time.sleep(CF_WAIT)

    # If still on CF challenge, wait longer (up to 30s total)
    for _ in range(4):
        if "Just a moment" not in driver.title and "moment" not in driver.page_source[:300].lower():
            break
        time.sleep(5)

    return driver.page_source


def fetch_sitemap() -> list[str]:
    """Fetch URLs from sitemap XML using requests."""
    print("  Fetching sitemap...")
    try:
        response = requests.get(SITEMAP_URL, timeout=20)
        response.raise_for_status()
        root = ET.fromstring(response.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = []
        for loc in root.findall(".//sm:url/sm:loc", ns):
            if loc.text:
                urls.append(loc.text.strip())
        print(f"  Found {len(urls)} URLs from sitemap")
        return sorted(set(urls))
    except Exception as e:
        print(f"  Sitemap fetch failed: {e}")
        return []


def should_scrape(url: str) -> bool:
    """Filter to content pages worth scraping."""
    path = urlparse(url).path.rstrip("/").lower()

    skip_patterns = [
        "/tag/", "/category/", "/author/", "/page/",
        "/feed", "/wp-json", "/wp-content/", "/wp-admin/",
        "/cart", "/checkout", "/my-account",
        "/privacy", "/terms", "/cookie",
        "/login", "/signup", "/register",
        "/search", "/404",
        "/thank-you", "/schedule-a-demo", "/contact-us",
        "/subscribe", "/incident-report-form",
        "/legal/",
    ]
    if any(pat in path for pat in skip_patterns):
        return False

    if any(path.endswith(ext) for ext in [".pdf", ".png", ".jpg", ".xml", ".css", ".js"]):
        return False

    return True


def classify_url(url: str) -> str:
    """Classify URL for output filename category."""
    path = urlparse(url).path.lower()
    if path.startswith("/blog/"):
        return "blog"
    if path.startswith("/resource-center/"):
        return "resource-center"
    if path.startswith("/insights/"):
        return "insights"
    if path.startswith("/properties/"):
        return "properties"
    if path.startswith("/residents/"):
        return "residents"
    if path.startswith("/videos/"):
        return "videos"
    if path.startswith("/webinars/"):
        return "webinars"
    if path.startswith("/customer-spotlights/"):
        return "customer-spotlights"
    if path.startswith("/ebooks/") or path.startswith("/reports/") or path.startswith("/brochures/"):
        return "downloads"
    return "general"


def extract_content(html: str) -> tuple[str, str]:
    """Extract title and main content HTML from page."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content elements
    for tag in soup.find_all(["nav", "header", "footer", "script", "style",
                               "noscript", "iframe", "form"]):
        tag.decompose()

    # Remove by common class/id patterns
    for selector in [
        "[class*='nav']", "[class*='footer']", "[class*='header']",
        "[class*='sidebar']", "[class*='cookie']", "[class*='popup']",
        "[class*='modal']", "[class*='menu']", "[class*='widget']",
        "[class*='cta-']", "[class*='banner']",
    ]:
        for el in soup.select(selector):
            el.decompose()

    # Title
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    elif soup.title:
        title = soup.title.get_text(strip=True)

    # Find main content
    content = None
    for sel in ["article", "main", "[role='main']", ".post-content",
                ".entry-content", ".blog-content", ".page-content",
                ".w-richtext", ".rich-text-block", "#content"]:
        content = soup.select_one(sel)
        if content:
            break

    if not content:
        content = soup.find("body")

    return title, str(content) if content else ""


def html_to_markdown(html: str) -> str:
    """Convert HTML to clean markdown."""
    markdown = md(
        html,
        heading_style="ATX",
        bullets="-",
        strip=["img", "video", "audio", "picture", "source"],
    )
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    return markdown.strip()


def slugify(text: str) -> str:
    """Convert text to filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:80].strip("-")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Foxen Website Scraper")
    print("=" * 60)
    print()

    driver = create_driver()

    try:
        # Step 1: Discover URLs
        print("[Step 1] Discovering URLs...")
        all_urls = fetch_sitemap()
        if not all_urls:
            print("No URLs found. Exiting.")
            return

        # Step 2: Filter
        print()
        print("[Step 2] Filtering URLs...")
        urls_to_scrape = [u for u in all_urls if should_scrape(u)]
        print(f"  Total: {len(urls_to_scrape)}")

        # Step 3: Scrape
        print()
        print("[Step 3] Scraping pages...")
        scraped = 0
        failed = 0

        for i, url in enumerate(urls_to_scrape, 1):
            path = urlparse(url).path.rstrip("/") or "/home"
            print(f"  [{i}/{len(urls_to_scrape)}] {path}")

            try:
                source = fetch_page(driver, url)

                title, content_html = extract_content(source)
                if not content_html:
                    print(f"    No content - skipping")
                    failed += 1
                    continue

                markdown = html_to_markdown(content_html)
                if len(markdown) < 100:
                    print(f"    Too short ({len(markdown)} chars) - skipping")
                    failed += 1
                    continue

                # Build filename
                slug = slugify(path.replace("/", " ").strip()) or "home"
                category = classify_url(url)
                filename = f"{category}-{slug}.md"

                full_content = f"# {title}\n\n*Source: {url}*\n\n---\n\n{markdown}"
                (OUTPUT_DIR / filename).write_text(full_content, encoding="utf-8")
                scraped += 1
                print(f"    Saved: {filename} ({len(markdown)} chars)")

            except Exception as e:
                print(f"    Error: {e}")
                failed += 1

            time.sleep(REQUEST_DELAY)

    finally:
        driver.quit()

    print()
    print("=" * 60)
    print(f"SCRAPING COMPLETE")
    print(f"  Scraped: {scraped}")
    print(f"  Failed/Skipped: {failed}")
    print(f"  Output: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
