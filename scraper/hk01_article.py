from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

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


def _detect_exclusive(hay: str) -> bool:
    for pat in EXCLUSIVE_PATTERNS:
        if pat in hay:
            return True
    return False


def _tokens_to_text(tokens: Any) -> str:
    """Flatten HK01 htmlTokens nested lists into plain text."""
    parts: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("content"), str):
                parts.append(node["content"])
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            parts.append(node)

    walk(tokens)
    return _text("".join(parts))


def _publish_iso(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # HK01 uses unix seconds
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
        except Exception:
            return None
    if isinstance(raw, str):
        try:
            return dateparser.parse(raw).isoformat()
        except Exception:
            return None
    return None


def _article_from_next_data(html: str, url: str) -> Optional[ArticleData]:
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        art = data["props"]["initialProps"]["pageProps"]["article"]
    except Exception:
        return None

    paragraphs: List[str] = []
    teaser = art.get("teaser")
    if isinstance(teaser, list):
        for t in teaser:
            tt = _text(str(t))
            if tt:
                paragraphs.append(tt)
    elif isinstance(teaser, str) and teaser.strip():
        paragraphs.append(_text(teaser))

    desc = art.get("description")
    if isinstance(desc, str):
        d = _text(desc)
        if d and d not in paragraphs and not any(d in p for p in paragraphs):
            # Only prepend if teaser missing
            if not paragraphs:
                paragraphs.insert(0, d)

    for block in art.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if block.get("blockType") != "text":
            continue
        t = _tokens_to_text(block.get("htmlTokens"))
        if t and t not in paragraphs:
            paragraphs.append(t)

    authors: List[str] = []
    for a in art.get("authors") or []:
        if isinstance(a, dict):
            name = _text(str(a.get("name") or a.get("publishName") or ""))
        else:
            name = _text(str(a))
        if name and name not in authors:
            authors.append(name)

    tags = []
    for t in art.get("tags") or []:
        if isinstance(t, dict):
            name = _text(str(t.get("name") or t.get("tagName") or ""))
        else:
            name = _text(str(t))
        if name:
            tags.append(name)

    title = _text(str(art.get("title") or art.get("metaTitle") or ""))
    hay = " ".join([title, " ".join(tags), " ".join(paragraphs[:2])])
    exclusive = _detect_exclusive(hay)

    canonical = art.get("canonicalUrl") or art.get("publishUrl") or url
    if isinstance(canonical, str) and canonical.startswith("/"):
        canonical = "https://www.hk01.com" + canonical

    if not title and not paragraphs:
        return None

    return ArticleData(
        url=str(canonical),
        title=title,
        published_iso=_publish_iso(art.get("publishTime") or art.get("lastModifyTime")),
        authors=authors,
        body_paragraphs=paragraphs,
        tags=tags,
        exclusive=exclusive,
        section=(art.get("mainCategory") or {}).get("categoryName")
        if isinstance(art.get("mainCategory"), dict)
        else "Hong Kong",
    )


def _article_from_dom(html: str, url: str) -> ArticleData:
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

    tags = list(
        {
            _text(t.get_text(" "))
            for t in soup.select("a[rel='tag'], .tag, .label, .badge")
            if _text(t.get_text(" "))
        }
    )
    exclusive = _detect_exclusive(
        " ".join(
            [
                title or "",
                " ".join(tags),
                " ".join(
                    m.get("content", "")
                    for m in soup.select("meta[property^='og:'], meta[name^='og:']")
                ),
            ]
        )
    )

    return ArticleData(
        url=url,
        title=title or "",
        published_iso=published_iso,
        authors=authors,
        body_paragraphs=paragraphs,
        tags=tags,
        exclusive=exclusive,
    )


async def fetch_article(url: str, timeout_ms: int = 30000) -> ArticleData:
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
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1200)
        html = await page.content()
        await ctx.close()
        await browser.close()

    from_json = _article_from_next_data(html, url)
    if from_json and from_json.body_paragraphs:
        return from_json
    return _article_from_dom(html, url)
