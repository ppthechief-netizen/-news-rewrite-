"""Poll HK01 Hong Kong zone and queue new articles as JSON for rewriting.

Usage:
  python -m pipeline.poll_inbox --n 10

Writes to rewrites/inbox/<articleId>.json and skips ids already in
rewrites/inbox, rewrites/copy-ready, or storage/final.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import re
from typing import Set
from urllib.parse import urlsplit

from scraper.hk01_article import fetch_article
from scraper.hk01_list import fetch_latest_links

BASE = pathlib.Path(__file__).resolve().parent.parent
INBOX = BASE / "rewrites" / "inbox"
COPY_READY = BASE / "rewrites" / "copy-ready"
FINAL = BASE / "storage" / "final"

ID_RE = re.compile(r"/(\d{6,})")


def article_key(url: str) -> str:
    m = ID_RE.search(url or "")
    if m:
        return m.group(1)
    u = urlsplit(url)._replace(fragment="", query="").geturl().rstrip("/")
    return hashlib.sha1(u.encode("utf-8")).hexdigest()[:12]


def existing_keys() -> Set[str]:
    keys: Set[str] = set()
    for folder in (INBOX, COPY_READY, FINAL):
        if not folder.exists():
            continue
        for p in folder.iterdir():
            if p.suffix.lower() not in {".json", ".md"}:
                continue
            keys.add(p.stem)
            # copy-ready names: YYYY-MM-DD-slug-<id>
            if "-" in p.stem:
                keys.add(p.stem.rsplit("-", 1)[-1])
            m = ID_RE.search(p.name)
            if m:
                keys.add(m.group(1))
    return keys


async def poll(n: int = 10) -> list[pathlib.Path]:
    INBOX.mkdir(parents=True, exist_ok=True)
    COPY_READY.mkdir(parents=True, exist_ok=True)
    known = existing_keys()
    links = await fetch_latest_links(limit=n)
    saved: list[pathlib.Path] = []
    for url in links:
        key = article_key(url)
        if key in known:
            print(f"skip {key}")
            continue
        ad = await fetch_article(url)
        key = article_key(ad.url) or key
        path = INBOX / f"{key}.json"
        if path.exists() or key in known:
            print(f"skip {key}")
            continue
        payload = {
            "url": ad.url,
            "title": ad.title,
            "published_iso": ad.published_iso,
            "authors": ad.authors,
            "exclusive": ad.exclusive,
            "tags": ad.tags,
            "paragraphs": ad.body_paragraphs,
            "section": ad.section,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        known.add(key)
        saved.append(path)
        print(f"queued {path.name} | {ad.title}")
    if not saved:
        print("No new articles queued.")
    else:
        print(f"Queued {len(saved)} article(s) into {INBOX}")
        # Notify that new source articles are ready for rewrite (popup after
        # rewrite completion is handled by watch_copy_ready_notify).
    return saved


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue new HK01 articles for rewrite")
    parser.add_argument("--n", type=int, default=10, help="Latest links to inspect")
    args = parser.parse_args()
    asyncio.run(poll(n=args.n))


if __name__ == "__main__":
    main()
