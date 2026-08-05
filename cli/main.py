from __future__ import annotations

import asyncio
import json
import pathlib
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

WRITER_TMPL = (PROMPTS / "WRITER_USER_TEMPLATE_MD.txt").read_text(encoding="utf-8")
VALIDATOR_TMPL = (PROMPTS / "VALIDATOR_USER_TEMPLATE.txt").read_text(encoding="utf-8")
AUTOFIX_TMPL = (PROMPTS / "AUTOFIX_USER_TEMPLATE_MD.txt").read_text(encoding="utf-8")


def ensure_dirs():
    for d in ["raw_html", "json", "drafts", "fixed", "final"]:
        (STORAGE / d).mkdir(parents=True, exist_ok=True)


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
    n: int = typer.Option(3, help="Number of latest HK01 Hong Kong News items to process"),
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
        body_hint = " ".join(ad.body_paragraphs[:6])[:1400]

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
        print(f"✔ Wrote storage/final/{stem}.md  |  Exclusive credit: {ad.exclusive}")


if __name__ == "__main__":
    APP()
