from __future__ import annotations

import re
from typing import List, Set

from playwright.async_api import async_playwright

HK01_ZONE_URL = "https://www.hk01.com/zone/1/%E6%B8%AF%E8%81%9E"
ARTICLE_RE = re.compile(r"^/article/\d+(?:$|[?#])|^https?://www\.hk01\.com/article/\d+")


async def fetch_latest_links(limit: int = 10, timeout_ms: int = 25000) -> List[str]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="zh-HK", user_agent="Mozilla/5.0")
        page = await ctx.new_page()
        await page.goto(HK01_ZONE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1500)
        anchors = await page.locator("a").all()
        urls: List[str] = []
        seen: Set[str] = set()
        for a in anchors:
            href = (await a.get_attribute("href")) or ""
            if not href:
                continue
            if ARTICLE_RE.search(href):
                if href.startswith("/"):
                    href = "https://www.hk01.com" + href
                if href not in seen:
                    seen.add(href)
                    urls.append(href)
            if len(urls) >= limit:
                break
        await ctx.close()
        await browser.close()
        return urls
