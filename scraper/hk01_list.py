from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import async_playwright

HK01_ZONE_URL = "https://www.hk01.com/zone/1/%E6%B8%AF%E8%81%9E"

# Legacy: /article/12345678
# Current: /突發/60378383/slug  or percent-encoded category segment
ARTICLE_RE = re.compile(
    r"(?:https?://(?:www\.)?hk01\.com)?"
    r"/(?:article/(\d{6,})|([^/?#]+)/(\d{6,})(?:/[^?#]*)?)"
)
SKIP_CATEGORIES = {
    "issue",
    "channel",
    "zone",
    "tag",
    "author",
    "search",
    "hot",
    "video",
    "live",
    "topic",
    "s",
}


def _normalize(url: str) -> str:
    parts = urlsplit(urljoin("https://www.hk01.com", url))
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")).rstrip("/")
    return clean


def _article_id(url: str) -> Optional[str]:
    m = ARTICLE_RE.search(url)
    if not m:
        return None
    return m.group(1) or m.group(3)


def _is_article_href(href: str) -> bool:
    if not href:
        return False
    path = href.split("?")[0].split("#")[0]
    m = ARTICLE_RE.search(path)
    if not m:
        return False
    # Groups: (legacy_id) or (category, id)
    if m.group(1):
        return True
    cat = (m.group(2) or "").strip().lower()
    if not cat or cat.isdigit() or cat in SKIP_CATEGORIES:
        return False
    return bool(m.group(3))


def _links_from_next_data(html_or_json: str) -> List[str]:
    """Prefer ordered article URLs from Next.js payload when present."""
    try:
        # Caller may pass raw script text or full HTML
        if '"props"' in html_or_json and html_or_json.strip().startswith("{"):
            data = json.loads(html_or_json)
        else:
            m = re.search(
                r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                html_or_json,
                re.DOTALL,
            )
            if not m:
                return []
            data = json.loads(m.group(1))
    except Exception:
        return []

    found: List[str] = []
    seen: Set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for key in ("canonicalUrl", "url", "shareUrl", "path"):
                val = node.get(key)
                if isinstance(val, str) and _is_article_href(val):
                    full = _normalize(val)
                    # Prefer pretty paths over bare /article/<id> when both exist
                    if full not in seen:
                        seen.add(full)
                        found.append(full)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return found


async def fetch_latest_links(limit: int = 10, timeout_ms: int = 45000) -> List[str]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="zh-HK",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        await page.goto(HK01_ZONE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        html = await page.content()

        # Deduplicate by article id; prefer pretty /category/id/slug over /article/id
        by_id: Dict[str, str] = {}
        order: List[str] = []

        def consider(href: str) -> None:
            if not _is_article_href(href):
                return
            full = _normalize(href)
            aid = _article_id(full)
            if not aid:
                return
            prev = by_id.get(aid)
            pretty = "/article/" not in urlsplit(full).path
            if prev is None:
                by_id[aid] = full
                order.append(aid)
            elif pretty and "/article/" in urlsplit(prev).path:
                by_id[aid] = full

        for href in _links_from_next_data(html):
            consider(href)
            if len(order) >= limit * 2:
                break

        if len(order) < limit:
            anchors = await page.locator("a[href]").all()
            for a in anchors:
                consider((await a.get_attribute("href")) or "")
                if len(order) >= limit * 2:
                    break

        urls = [by_id[aid] for aid in order if aid in by_id][:limit]
        await ctx.close()
        await browser.close()
        return urls