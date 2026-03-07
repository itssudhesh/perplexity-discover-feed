import os
import re
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


def get_cards(page):
    """Extract article cards from the Discover landing page."""
    print(f"Loading {DISCOVER_URL}")
    page.goto(DISCOVER_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)

    # Scroll to trigger lazy-loaded cards
    for _ in range(5):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        time.sleep(0.8)

    # Wait until at least one discover card link is present
    try:
        page.wait_for_selector("a[href*='/discover/']", timeout=20000)
    except Exception:
        print("Warning: timed out waiting for cards — continuing anyway")

    cards = page.query_selector_all("a[href*='/discover/you/']")
    print(f"Found {len(cards)} cards")

    results = []
    seen = set()

    for card in cards:
        href = card.get_attribute("href") or ""
        if not href or href in seen or "/discover/" not in href:
            continue
        seen.add(href)

        full_url = BASE_URL + href if href.startswith("/") else href

        # Title
        title_el = card.query_selector("div[data-testid='thread-title']")
        title = title_el.inner_text().strip() if title_el else ""

        # Description (line-clamp-6 prose div)
        desc_el = card.query_selector("div.prose.font-sans.text-base.text-foreground")
        description = desc_el.inner_text().strip() if desc_el else ""

        # Image
        img_el = card.query_selector("img[src*='cloudinary']")
        image_url = img_el.get_attribute("src") if img_el else ""

        # Time
        time_el = card.query_selector("span.truncate")
        published_rel = time_el.inner_text().strip() if time_el else ""

        # Source domains from favicon img alts (e.g. "techcrunch.com favicon")
        favicon_imgs = card.query_selector_all("img[alt*='favicon']")
        source_domains = []
        for fi in favicon_imgs:
            alt = fi.get_attribute("alt") or ""
            domain = alt.replace(" favicon", "").strip()
            if domain and domain not in source_domains:
                source_domains.append(domain)

        if title:
            results.append({
                "url": full_url,
                "title": title,
                "description": description,
                "image": image_url,
                "published_rel": published_rel,
                "source_domains": source_domains,
            })

    return results


def get_article_details(page, card):
    """Visit article page and extract full body + source links."""
    url = card["url"]
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # --- Title ---
        title_el = page.query_selector("h2 span.rounded-md")
        title = title_el.inner_text().strip() if title_el else card["title"]

        # --- Subtitle / description ---
        desc_el = page.query_selector("div.mt-md.font-sans.text-base.text-foreground")
        description = desc_el.inner_text().strip() if desc_el else card["description"]

        # --- Cover image (og:image is most reliable) ---
        img_meta = page.query_selector("meta[property='og:image']")
        image_url = img_meta.get_attribute("content") if img_meta else card["image"]

        # --- Body: collect all prose sections ---
        # Each section is a div.prose.dark:prose-invert.inline containing h2/p tags
        body_sections = page.query_selector_all("div.prose.dark\\:prose-invert.inline")
        body_html = ""
        for section in body_sections:
            cleaned_html = page.evaluate("""(el) => {
                let clone = el.cloneNode(true);
                // Remove inline citation badges
                clone.querySelectorAll('span.citation').forEach(e => e.remove());
                // Remove stock ticker inline elements
                clone.querySelectorAll('span.select-none.align-middle').forEach(e => e.remove());
                // Remove citation-nbsp spans
                clone.querySelectorAll('span.citation-nbsp').forEach(e => e.remove());
                return clone.innerHTML;
            }""", section)
            body_html += f'<div class="section">{cleaned_html}</div>\n'

        # --- Source links from the top source card grid ---
        source_links = []
        source_anchors = page.query_selector_all("a[rel='noopener'][target='_blank']")
        for a in source_anchors:
            href = a.get_attribute("href") or ""
            if not href.startswith("http"):
                continue
            # Get title from aria-label on nearest parent span
            source_title = page.evaluate(
                "(el) => { let s = el.closest('span[aria-label]'); return s ? s.getAttribute('aria-label') : ''; }",
                a
            )
            # Fallback: inner title div
            if not source_title:
                title_div = a.query_selector("div.font-sans.text-xs.font-medium.text-foreground")
                source_title = title_div.inner_text().strip() if title_div else ""

            # Domain from favicon alt
            fav = a.query_selector("img[alt*='favicon']")
            domain = (fav.get_attribute("alt") or "").replace(" favicon", "").strip() if fav else ""

            existing_urls = [s["url"] for s in source_links]
            if href not in existing_urls:
                source_links.append({
                    "url": href,
                    "title": source_title,
                    "domain": domain,
                })

        return {
            "url": url,
            "title": title,
            "description": description,
            "image": image_url,
            "body_html": body_html,
            "source_links": source_links,
            "published_rel": card["published_rel"],
            "source_domains": card["source_domains"],
        }

    except Exception as e:
        print(f"  Error on {url}: {e}")
        return {**card, "body_html": "", "source_links": []}


def build_rss_item(item):
    lines = ["<item>"]
    lines.append(f"  <title>{escape_xml(item['title'])}</title>")
    lines.append(f"  <link>{escape_xml(item['url'])}</link>")
    lines.append(f"  <guid isPermaLink=\"true\">{escape_xml(item['url'])}</guid>")
    lines.append(f"  <pubDate>{datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate>")

    if item.get("description"):
        lines.append(f"  <description>{escape_xml(item['description'])}</description>")

    if item.get("image"):
        lines.append(f'  <media:content url="{escape_xml(item["image"])}" medium="image"/>')

    # Build content:encoded — subtitle + body + sources appendix
    content_parts = []

    if item.get("description"):
        content_parts.append(f"<p><em>{item['description']}</em></p>")

    if item.get("body_html"):
        content_parts.append(item["body_html"])

    if item.get("source_links"):
        content_parts.append("<hr/><h3>Sources</h3><ul>")
        for s in item["source_links"]:
            label = s["title"] or s["domain"] or s["url"]
            content_parts.append(f'<li><a href="{s["url"]}">{label}</a></li>')
        content_parts.append("</ul>")
    elif item.get("source_domains"):
        content_parts.append(
            "<hr/><p><strong>Sources:</strong> " + ", ".join(item["source_domains"]) + "</p>"
        )

    if content_parts:
        lines.append(f"  <content:encoded>{wrap_cdata(chr(10).join(content_parts))}</content:encoded>")

    lines.append("</item>")
    return "\n".join(lines)


def build_feed(items):
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    rss = '<?xml version="1.0" encoding="UTF-8"?>\n'
    rss += (
        '<rss version="2.0" '
        'xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
        '<channel>\n'
        '<title>Perplexity Discover</title>\n'
        f'<link>{DISCOVER_URL}</link>\n'
        '<description>Perplexity Discover — auto-scraped feed</description>\n'
        f'<lastBuildDate>{now}</lastBuildDate>\n'
    )
    for item in items:
        rss += build_rss_item(item) + "\n"
    rss += "</channel>\n</rss>"

    os.makedirs("docs", exist_ok=True)
    with open("docs/feed.xml", "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Feed written: {len(items)} items → docs/feed.xml")


if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        listing_page = context.new_page()
        cards = get_cards(listing_page)
        listing_page.close()

        items = []
        detail_page = context.new_page()
        for card in cards:
            print(f"Fetching: {card['url']}")
            article = get_article_details(detail_page, card)
            items.append(article)
            time.sleep(0.5)
        detail_page.close()
        browser.close()

    build_feed(items)
