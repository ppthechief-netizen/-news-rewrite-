from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from playwright.async_api import async_playwright

from .selectors import BODY_SELECTORS, TITLE_SELECTORS, TIME_SELECTORS, EXCLUSIVE_PATTERNS


@dataclass
class ArticleData:
    url: str
    title: str
    published_iso: Optional[str]
    authors: List[str]
    body_paragraphs: List[str]
    outlet: str = "HK01"
    section: Optional[str] = "Hong Kong"
    language: str = "zh-Hant"
    exclusive: bool = False
    tags: List[str] = field(default_factory=list)


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _extract_with_selectors(soup: BeautifulSoup, selectors: List[str]) -> Optional[str]:
    for sel in selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        if el.name == "meta":
            c = el.get("content")
            if c:
                return _text(c)
        elif el.name == "time":
            c = el.get("datetime") or el.get_text(" ")
            if c:
                return _text(c)
        else:
            t = el.get_text(" ")
            if t:
                return _text(t)
    return None


def _detect_exclusive(soup: BeautifulSoup, title: str) -> bool:
    hay = " ".join([
        title or "",
        " ".join(t.get_text(" ") for t in soup.select("a[rel='tag'], .tag, .label, .badge")),
        " ".join(m.get("content", "") for m in soup.select("meta[property^='og:'], meta[name^='og:']")),
        " ".join(el.get_text(" ") for el in soup.select("h1,h2,h3,.kicker,.subtitle")),
    ])
    for pat in EXCLUSIVE_PATTERNS:
        if pat in hay:
            return True
    return False


async def fetch_article(url: str, timeout_ms: int = 30000) -> ArticleData:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="zh-HK", user_agent="Mozilla/5.0")
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1200)
        html = await page.content()
        await ctx.close()
        await browser.close()

    soup = BeautifulSoup(html, "lxml")

    title = _extract_with_selectors(soup, TITLE_SELECTORS) or ""
    pub_raw = _extract_with_selectors(soup, TIME_SELECTORS)
    published_iso = None
    if pub_raw:
        try:
            published_iso = dateparser.parse(pub_raw).isoformat()
        except Exception:
            published_iso = None

    body_node = None
    for sel in BODY_SELECTORS:
        node = soup.select_one(sel)
        if node:
            body_node = node
            break

    paragraphs: List[str] = []
    if body_node:
        for p in body_node.select("p"):
            t = _text(p.get_text(" "))
            if t and len(t) > 1:
                paragraphs.append(t)

    authors: List[str] = []
    for a_sel in ["[rel='author']", ".author", "a[href*='author']"]:
        for a in soup.select(a_sel):
            t = _text(a.get_text(" "))
            if t and t not in authors:
                authors.append(t)

    tags = list({
        _text(t.get_text(" "))
        for t in soup.select("a[rel='tag'], .tag, .label, .badge")
        if _text(t.get_text(" "))
    })
    exclusive = _detect_exclusive(soup, title)

    return ArticleData(
        url=url,
        title=title or "",
        published_iso=published_iso,
        authors=authors,
        body_paragraphs=paragraphs,
        tags=tags,
        exclusive=exclusive,
    )
