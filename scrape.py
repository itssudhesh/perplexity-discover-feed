import os
import json
import time
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.perplexity.ai"
DISCOVER_URL = f"{BASE_URL}/discover"


def escape_xml(text):
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


def wrap_cdata(html):
    return "<![CDATA[" + html.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def get_pw_cookies():
    raw = os.environ.get("PPLX_COOKIES", "")
    if not raw:
        print("Warning: PPLX_COOKIES not set")
        return []
    samesite_map = {
        "no_restriction": "None", "lax": "Lax",
        "strict": "Strict", "unspecified": "None"
    }
    skip = {"__cf_bm", "__cflb"}
    cookies = []
    for c in json.loads(raw):
        if c["name"] in skip:
            continue
        pw = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": samesite_map.get(c.get("sameSite", "lax"), "Lax"),
        }
        if c.get("expirationDate"):
            pw["expires"] = int(c["expirationDate"])
        cookies.append(pw)
    print(f"Injected {len(cookies)} cookies")
    return cookies


def scrape_via_network_intercept():
    """
    Load Discover page and capture the XHR/fetch response that
    carries the article list, rather than scraping the DOM.
    """
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1920,1080",
            ]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        context.add_cookies(get_pw_cookies())

        page = context.new_page()

        # Log ALL requests/responses for debugging
        api_responses = []

        def handle_response(response):
            url = response.url
            # Capture anything that looks like a Discover/feed API call
            if any(k in url for k in ["discover", "feed", "trending", "threads", "search/collections"]):
                try:
                    body = response.json()
                    print(f"  [API] {response.status} {url[:80]}")
                    api_responses.append({"url": url, "body": body})
                except Exception:
                    pass

        page.on("response", handle_response)

        print(f"Loading {DISCOVER_URL}")
        page.goto(DISCOVER_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)  # wait for all XHR to fire

        # Scroll to trigger any lazy-load requests
        for _ in range(4):
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            time.sleep(1.5)

        page.screenshot(path="docs/debug.png", full_page=False)
        print(f"Captured {len(api_responses)} API responses")

        # Also dump all response URLs for diagnosis
        print("All captured API URLs:")
        for r in api_responses:
            print(f"  {r['url'][:120]}")

        # Save raw API data for inspection
        os.makedirs("docs", exist_ok=True)
        with open("docs/api_responses.json", "w") as f:
            json.dump(api_responses, f, indent=2, default=str)

        browser.close()

    return api_responses


def parse_articles(api_responses):
    """
    Try to extract article items from captured API responses.
    We'll look for arrays of objects with title/slug fields.
    """
    articles = []
    for resp in api_responses:
        body = resp["body"]
        candidates = []
        if isinstance(body, list):
            candidates = body
        elif isinstance(body, dict):
            for v in body.values():
                if isinstance(v, list) and len(v) > 0:
                    candidates = v
                    break

        for item in candidates:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("text") or ""
            slug = item.get("slug") or item.get("url_slug") or ""
            if title and slug:
                url = f"{BASE_URL}/page/{slug}"
                articles.append({
                    "title": title,
                    "url": url,
                    "description": item.get("snippet") or item.get("description") or "",
                    "image": item.get("image_url") or item.get("thumbnail") or "",
                    "date": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z"),
                })

    print(f"Parsed {len(articles)} articles from API responses")
    return articles


def build_feed(items):
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    rss = '<?xml version="1.0" encoding="UTF-8"?>\n'
    rss += (
        '<rss version="2.0" '
        'xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
        '<channel>\n'
        '<title>Perplexity Discover</title>\n'
        f'<link>{DISCOVER_URL}</link>\n'
        '<description>Perplexity Discover — auto-scraped feed</description>\n'
        f'<lastBuildDate>{now}</lastBuildDate>\n'
    )
    for item in items:
        rss += "<item>\n"
        rss += f"  <title>{escape_xml(item['title'])}</title>\n"
        rss += f"  <link>{escape_xml(item['url'])}</link>\n"
        rss += f"  <guid isPermaLink=\"true\">{escape_xml(item['url'])}</guid>\n"
        rss += f"  <pubDate>{item['date']}</pubDate>\n"
        if item.get("description"):
            rss += f"  <description>{escape_xml(item['description'])}</description>\n"
        if item.get("image"):
            rss += f'  <media:content url="{escape_xml(item["image"])}" medium="image"/>\n'
        rss += "</item>\n"
    rss += "</channel>\n</rss>"

    os.makedirs("docs", exist_ok=True)
    with open("docs/feed.xml", "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Feed written: {len(items)} items → docs/feed.xml")


if __name__ == "__main__":
    api_responses = scrape_via_network_intercept()
    articles = parse_articles(api_responses)
    build_feed(articles)
