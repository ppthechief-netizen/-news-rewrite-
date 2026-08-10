from __future__ import annotations

import asyncio
import json
import pathlib
import re
from datetime import date
from typing import Any, Dict, Optional

import typer
from dotenv import load_dotenv
from openai import OpenAI

from pipeline.newsroom_markdown_pipeline import apply_deterministic_prefixes
from scraper.hk01_article import fetch_article
from scraper.hk01_list import fetch_latest_links
from utils.originality import needs_more_paraphrase
from utils.text import british_ordinal
from utils.urls import url_key

APP = typer.Typer(help="HK01 → British-English newsroom pipeline with exclusivity crediting")
load_dotenv()
client = OpenAI()


@APP.callback()
def _root() -> None:
    """HK01 → British-English newsroom pipeline with exclusivity crediting."""
    pass

BASE = pathlib.Path(__file__).resolve().parent.parent
PROMPTS = BASE / "prompts"
STORAGE = BASE / "storage"
COPY_READY = BASE / "rewrites" / "copy-ready"

WRITER_TMPL = (PROMPTS / "WRITER_USER_TEMPLATE_MD.txt").read_text(encoding="utf-8")
VALIDATOR_TMPL = (PROMPTS / "VALIDATOR_USER_TEMPLATE.txt").read_text(encoding="utf-8")
AUTOFIX_TMPL = (PROMPTS / "AUTOFIX_USER_TEMPLATE_MD.txt").read_text(encoding="utf-8")


def ensure_dirs():
    for d in ["raw_html", "json", "drafts", "fixed", "final"]:
        (STORAGE / d).mkdir(parents=True, exist_ok=True)
    COPY_READY.mkdir(parents=True, exist_ok=True)


def _slug_from_title(title: str, max_len: int = 48) -> str:
    """ASCII slug for copy-ready filenames; falls back to 'article'."""
    ascii_title = title.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_title).strip("-").lower()
    if not slug:
        return "article"
    return slug[:max_len].rstrip("-")


def mirror_copy_ready(stem: str, final_md: str, source_title: str = "") -> pathlib.Path:
    """Write a copy-ready Markdown file under rewrites/copy-ready/."""
    today = date.today().isoformat()
    slug = _slug_from_title(source_title)
    path = COPY_READY / f"{today}-{slug}-{stem}.md"
    path.write_text(final_md, encoding="utf-8")
    # Keep a stable latest pointer (overwrite) for quick open/copy.
    (COPY_READY / "LATEST.md").write_text(final_md, encoding="utf-8")
    index_path = COPY_READY / "INDEX.md"
    line = f"- [{today} · {source_title or stem}]({path.name})\n"
    if index_path.exists():
        existing = index_path.read_text(encoding="utf-8")
        if path.name not in existing:
            index_path.write_text(existing.rstrip() + "\n" + line, encoding="utf-8")
    else:
        index_path.write_text(
            "# Copy-ready HK01 rewrites\n\nOpen any file below and copy the Markdown.\n\n" + line,
            encoding="utf-8",
        )
    return path


def call_llm(
    prompt: str,
    system: Optional[str] = None,
    model: str = "gpt-4o-mini",
    temperature: float = 0.2,
) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    resp = client.chat.completions.create(model=model, messages=messages, temperature=temperature)
    return resp.choices[0].message.content.strip()


def make_verified_json(ad: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entities": [],
        "numbers": [],
        "locations": ["Hong Kong"],
        "outlet": "HK01",
        "source_url": ad["url"],
        "published_iso": ad.get("published_iso"),
        "language": ad.get("language", "zh-Hant"),
        "exclusive": ad.get("exclusive", False),
        "title": ad.get("title", ""),
        "tags": ad.get("tags", []),
    }


def build_writer_prompt(
    verified_json: Dict[str, Any],
    editor_notes: str,
    pub_date: str,
    today_ordinal: str,
    body_hint: str,
) -> str:
    return (
        WRITER_TMPL.replace(
            "<<<PASTE_JSON_FROM_VERIFIER_HERE>>>",
            json.dumps(verified_json, ensure_ascii=False),
        )
        .replace("{body_single_paragraph_then_more}", body_hint)
        .replace("{editor_notes}", editor_notes)
        .replace("{pub_date}", pub_date)
        .replace("{today_ordinal}", today_ordinal)
    )


def save(path: pathlib.Path, content: str):
    path.write_text(content, encoding="utf-8")


@APP.command()
def latest(
    n: int = typer.Option(10, help="Number of latest HK01 Hong Kong News items to process"),
    model: str = typer.Option("gpt-4o-mini"),
    paraphrase_strict: bool = typer.Option(
        True, help="If true, re-run AUTOFIX until originality thresholds pass"
    ),
    exclusive_override: Optional[bool] = typer.Option(
        None, help="Force exclusive credit on/off"
    ),
):
    ensure_dirs()
    links = asyncio.run(fetch_latest_links(limit=n))
    # Skip URLs we've already finalised
    existing = {p.stem for p in (STORAGE / "final").glob("*.md")}
    links = [u for u in links if url_key(u) not in existing]
    today = date.today()
    today_ord = british_ordinal(today)

    if not links:
        print("No new HK01 articles to process.")
        return

    for url in links:
        ad = asyncio.run(fetch_article(url))
        if exclusive_override is not None:
            ad.exclusive = bool(exclusive_override)

        ad_json = {
            "url": ad.url,
            "title": ad.title,
            "published_iso": ad.published_iso,
            "authors": ad.authors,
            "paragraphs": ad.body_paragraphs,
            "outlet": ad.outlet,
            "language": ad.language,
            "exclusive": ad.exclusive,
            "tags": ad.tags or [],
        }
        stem = url_key(ad.url)
        save(STORAGE / "json" / f"{stem}.json", json.dumps(ad_json, ensure_ascii=False, indent=2))

        editor_notes = (
            "Rewrite into British English; neutral, concise. No copied strings ≥ 10 words. "
            "Limit quotes to ≤25 words each. If VERIFIED_JSON.exclusive is true, add the HK01 credit line as instructed."
        )

        verified = make_verified_json(ad_json)
        # Fuller gist so rewrites cover material points (still paraphrase-only).
        body_hint = "\n\n".join(ad.body_paragraphs)[:4500]

        writer_prompt = build_writer_prompt(
            verified_json=verified,
            editor_notes=editor_notes,
            pub_date=ad.published_iso or today.isoformat(),
            today_ordinal=today_ord,
            body_hint=body_hint or "Summary forthcoming.",
        )
        draft_md = call_llm(writer_prompt, model=model)
        save(STORAGE / "drafts" / f"{stem}.md", draft_md)

        # Validator
        validator_in = VALIDATOR_TMPL + "\n\n" + draft_md
        validated_md = call_llm(validator_in, model=model)
        # Autofix with originality pressures
        original_src_text = "\n".join(ad.body_paragraphs)
        autofix_prompt = (
            AUTOFIX_TMPL.replace("<<<VERIFIED_JSON>>>", json.dumps(verified, ensure_ascii=False))
            .replace("<<<ORIGINAL_SOURCE_TEXT>>>", original_src_text)
            .replace("<<<DRAFT>>>", validated_md)
        )
        fixed_md = call_llm(autofix_prompt, model=model)

        # Loop for originality if needed
        if paraphrase_strict:
            tries = 0
            while tries < 2 and needs_more_paraphrase(original_src_text, fixed_md):
                autofix_prompt = (
                    AUTOFIX_TMPL.replace(
                        "<<<VERIFIED_JSON>>>", json.dumps(verified, ensure_ascii=False)
                    )
                    .replace("<<<ORIGINAL_SOURCE_TEXT>>>", original_src_text)
                    .replace("<<<DRAFT>>>", fixed_md)
                )
                fixed_md = call_llm(autofix_prompt, model=model, temperature=0.4)
                tries += 1

        final_md = apply_deterministic_prefixes(
            draft_md=fixed_md,
            outlet="HK01",
            original_source_text=original_src_text,
            today_iso=today.isoformat(),
            source_url=ad.url,
            exclusive=ad.exclusive,
        )

        save(STORAGE / "fixed" / f"{stem}.md", fixed_md)
        save(STORAGE / "final" / f"{stem}.md", final_md)
        copy_path = mirror_copy_ready(stem, final_md, source_title=ad.title)
        print(
            f"✔ Wrote storage/final/{stem}.md + {copy_path.relative_to(BASE)}  |  "
            f"Exclusive credit: {ad.exclusive}"
        )


if __name__ == "__main__":
    APP()
