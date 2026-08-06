#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Newsroom pipeline → Markdown automation
Verifier → Writer (Markdown) → Validator → Auto-fix → Re-validate

Outputs (in the same directory as --notes by default):
- verification.json
- article_draft.md
- article_fixed.md (only if fixes applied)
- validator.json

Example:
  export OPENAI_API_KEY=sk-...
  python newsroom_markdown_pipeline.py \
      --notes notes.txt \
      --outlet "South China Morning Post" \
      --pub-date "30th June 2026" \
      --model "gpt-4.1" \
      --temperature 0.2
"""
import os
import sys
import argparse
import json
import re
from datetime import date
from pathlib import Path
from textwrap import dedent
from typing import List, Optional, Tuple

try:
    from openai import OpenAI
except Exception:
    print("Please install the OpenAI SDK: pip install openai", file=sys.stderr)
    raise

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ----------------------------
# Prompt templates
# ----------------------------

HOUSE_STYLE_SYSTEM = dedent("""
You are a senior British-English news writer. Write crisp, neutral, fact-checked copy. Do not hallucinate. Keep it original and plagiarism-free.
""").strip()

VERIFIER_USER_TEMPLATE = dedent("""
Task: Extract and fact‑check all proper nouns from the source below, then return strict JSON with official/standard English spellings and Traditional Chinese forms, plus citations. Search the public web to confirm spellings, prioritising Cantonese personal names and Hong Kong roads/places/stations/institutions.

Scope
- People (Chinese/non‑Chinese), officials, executives, spokespeople.
- Hong Kong roads/streets/places/districts/landmarks/buildings; MTR stations/lines.
- Public bodies/agencies (Police, ICAC, HA, LCSD, TD, LandsD), schools/universities.
- Venues and common locations (e.g., Queen’s Road Central, Connaught Road Central, Nathan Road).

Source priority
1) Official/operators: gov.hk, info.gov.hk, police.gov.hk, td.gov.hk, landsd.gov.hk, geodata.gov.hk / GeoInfo Map, mtr.com.hk, judiciary.hk, legco.gov.hk, hko.gov.hk, ha.org.hk, hku.hk, cuhk.edu.hk, polyu.edu.hk.
2) Major outlets/reference: scmp.com, rthk.hk, thestandard.com.hk, hk01.com. Wikipedia only as last resort.
3) Maps: GeoInfo Map, OpenStreetMap, Google Maps. Prefer official spellings/punctuation.

Rules
- Search to confirm spellings. If multiple variants exist, pick the official form; include alternates in “aliases”.
- Cantonese personal names: return Traditional Chinese, the person’s standard English name (if any), and romanisation using the form found on official/commonly accepted pages. Do not invent transliterations.
- Roads/places/stations: return official English name, Traditional Chinese name, and district; include MTR line if applicable.
- If uncertain, set status "needs_review" and add a reason; do not guess.

Return strict JSON only, matching exactly this schema:
{
  "verified": [
    {
      "type": "person" | "road" | "place" | "station" | "institution" | "building" | "district" | "other",
      "original_mention": "string",
      "english": "official_or_standard_english_form",
      "chinese_trad": "繁體中文名",
      "extra": {
        "district": "if_applicable",
        "mtr_line": "if_station",
        "aliases": ["alt1","alt2"]
      },
      "status": "verified" | "needs_review",
      "sources": [
        {"title":"source title","url":"https://...","cred":"gov|operator|major_outlet|map"}
      ]
    }
  ],
  "unmatched": ["items not confidently verified"],
  "notes": "brief disambiguation if needed"
}

Source text:
{{notes}}
""").strip()

# Writer asks for Markdown with YAML front matter (three titles; no H1; inline dateline; footer)
WRITER_USER_TEMPLATE_MD = dedent("""
You will receive:
1) VERIFIED_JSON with minimal, trusted metadata about the source article.
2) BODY_HINT which is a compact gist derived from the source (do NOT copy).
3) EDITOR_NOTES with house style requirements.
Produce a fresh Markdown article with this exact structure:

---
title_primary: "<SEO Title Option 1>"
title_alt_1: "<SEO Title Option 2>"
title_alt_2: "<SEO Title Option 3>"
description: "<<=160 chars, sentence case, British English>"
---

[tta_listen_btn]

{today_date_ordinal} - (<city where the news occurred>) <ONE-SENTENCE LEDE capturing the core development in 22–30 words.>

<2–5 short paragraphs expanding facts. Always paraphrase; re-order facts; use synonyms. No copied strings ≥ 10 words. Keep quotes ≤ 25 words. British spelling.>

<Add a tight context paragraph (policy, market, legal, safety, or civic angle).>

<If numbers are present, provide a compact bullet list titled “Key figures” with three dashes above and below; else omit this block.>

---
Footer
SEO keyword: <one simple lowercase ASCII word>
Meta description: <<=160 chars, punchy, plain-English one-liner>

Rules:
- British English.
- No attribution to the source unless VERIFIED_JSON.exclusive is true.
- If VERIFIED_JSON.exclusive is true, append one final line under Footer:
  Credit: HK01 — original reporting. Source: <VERIFIED_JSON.source_url>
  The link must be a standard do‑follow Markdown link: [HK01](<VERIFIED_JSON.source_url>).
- Never include the Chinese source text. Do not translate names unless common in English.
- Keep it 60–75% of the source length. Paraphrase, compress, and re-sequence ideas.
- No promotional tone. No opinion. No speculation.
- Headlines: avoid clickbait, use “: ” not “—” for clauses, capitalise only first word and proper nouns.

Inputs:
VERIFIED_JSON:
<<<PASTE_JSON_FROM_VERIFIER_HERE>>>

BODY_HINT:
{body_hint}

EDITOR_NOTES:
{editor_notes}
""").strip()

VALIDATOR_USER_TEMPLATE = dedent("""
You are a strict validator for newsroom copy. Given a DRAFT (Markdown) and VERIFIED_JSON, return STRICT JSON:

{
  "meta_ok": true/false,                  // YAML description ≤160 chars
  "date_style_ok": true/false,
  "time_style_ok": true/false,
  "contains_colon_times": true/false,
  "colon_time_suggestions": ["3:05pm -> 3.05pm"],

  "titles_ok": true/false,                // title_primary + title_alt_1 + title_alt_2 present; ASCII; sentence case
  "title_issues": ["reason…"],

  "structure_ok": true/false,             // YAML → [tta_listen_btn] → dateline lede → body → optional Key figures → Footer
  "british_english_ok": true/false,
  "neutral_tone_ok": true/false,
  "us_style_ok": true/false,
  "british_spelling_ok": true/false,
  "american_spellings_found": ["color -> colour"],
  "quotes_ok": true/false,                // each quote ≤25 words
  "no_copied_strings_ok": true/false,     // no contiguous ≥10-word copy from source
  "all_names_verified": true/false,
  "no_cjk_ok": true/false,
  "no_hallucinations_ok": true/false,     // facts plausible and consistent with VERIFIED_JSON
  "no_source_reference_ok": true/false,   // true if exclusive credit rule satisfied
  "exclusive_credit_ok": true/false,      // if exclusive: Credit line under Footer; else: no attribution
  "shortcode_ok": true/false,             // [tta_listen_btn] present and followed immediately by body (no H1/title)

  "inline_dateline_ok": true/false,       // body’s first non‑empty line begins with ordinal date + city where news occurred
  "dateline_date_ok": true/false,         // equals today in British ordinal
  "dateline_city": "<city>",
  "dateline_expected_city": "<city where news occurred>",

  "body_word_count": 0,
  "body_word_count_ok": true/false,       // 220–520 unless body_hint is a very short brief
  "body_length_ratio": 0.0,
  "body_length_ok": true/false,

  "all_items_have_sources": true/false,

  "footer_ok": true/false,                // Footer block exists with both lines
  "seo_keyword_ok": true/false,           // exactly one ASCII word: ^[a-z0-9]+$
  "meta_footer_ok": true/false,           // Meta description present in footer
  "meta_footer_length_ok": true/false,    // ≤160 chars
  "footer_is_last_ok": true/false,        // nothing after Meta description except optional Credit line / newline

  "flags": ["short issues…"],
  "suggestions": ["minimal edits only…"]
}

Guidance
- titles_ok:
  - YAML must have title_primary, title_alt_1, and title_alt_2 (quoted strings).
  - Each: ASCII only (^[\\\\x20-\\\\x7E]+$), sentence case (first word + proper nouns), use “: ” not “—” for clauses, no clickbait, no emojis/brackets/pipes.
- structure_ok: sections must match the writer template order.
- shortcode_ok: true only if a line containing exactly "[tta_listen_btn]" appears after the YAML block and there is NO "# " H1 anywhere after it.
- inline_dateline_ok: the first non‑empty line after the shortcode must start with: "<TODAY_ORDINAL> - (<city where the news occurred>) " and continue with content on the same line.
- exclusive_credit_ok:
  - If VERIFIED_JSON.exclusive == true, a line exactly under Footer must read:
    Credit: HK01 — original reporting. Source: [HK01](<VERIFIED_JSON.source_url>)
  - Otherwise, no attribution / Credit line may exist.
- body_word_count_ok: body text (after shortcode, before Footer) should be 220–520 words unless BODY_HINT is clearly a very short brief.
- footer_ok: must contain a Footer block with both lines:
  - "SEO keyword: <one word>"
  - "Meta description: <text>"
- seo_keyword_ok: the keyword must be a single lowercase ASCII word (no spaces/hyphens/emoji).

Draft:
<<<DRAFT>>>

VERIFIED_JSON:
<<<PASTE_JSON_FROM_VERIFIER_HERE>>>

BODY_HINT / Original:
<<<ORIGINAL_SOURCE_TEXT>>>
""").strip()

AUTOFIX_USER_TEMPLATE_MD = dedent("""
Task: Auto-fix the DRAFT below to reduce similarity with ORIGINAL_SOURCE_TEXT while preserving facts.

Constraints:
- Keep the Writer template structure untouched.
- Shorten or expand to 60–75% of ORIGINAL_SOURCE_TEXT length.
- Break long sentences; vary syntax; replace phrases with synonyms; re-order paragraphs.
- Maximum verbatim overlap: no string ≥ 10 consecutive words copied.
- Quotes allowed but each ≤ 25 words.
- British English, neutral tone.
- If exclusive flag is true in VERIFIED_JSON, ensure the exact credit line is present; otherwise, remove any credit.

Inputs:
VERIFIED_JSON:
<<<VERIFIED_JSON>>>

ORIGINAL_SOURCE_TEXT:
<<<ORIGINAL_SOURCE_TEXT>>>

DRAFT:
<<<DRAFT>>>
""").strip()

# ----------------------------
# Deterministic style helpers
# ----------------------------

# --- time (colon -> dot) ---
COLON_TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2]):([0-5][0-9])\s*([AaPp][Mm])\b")


def find_colon_times_with_suggestions(text: str):
    suggestions = []
    seen = set()
    for m in COLON_TIME_RE.finditer(text):
        hour_str, minute_str, ampm = m.group(1), m.group(2), m.group(3)
        hour_norm = str(int(hour_str))
        suggestion = f"{hour_norm}.{minute_str}{ampm.lower()}"
        original = m.group(0)
        pair = f"{original} -> {suggestion}"
        if pair not in seen:
            seen.add(pair)
            suggestions.append(pair)
    return (len(suggestions) > 0), suggestions


def replace_colon_times(text: str) -> str:
    def _sub(m):
        hour_norm = str(int(m.group(1)))
        return f"{hour_norm}.{m.group(2)}{m.group(3).lower()}"
    return COLON_TIME_RE.sub(_sub, text)


# --- US style (U.S./USA -> US; hyphenate compounds) ---
US_COMPOUND_WORDS = (
    "based|listed|backed|led|owned|made|built|centric|focused|registered|headquartered|dollar|dollars|only|bound"
)
US_DOT_RE = re.compile(r"\bU\.S\.(?=\W|$)")
USA_RE = re.compile(r"\bUSA\b")
US_SPACE_COMPOUND_RE = re.compile(rf"\bUS\s+({US_COMPOUND_WORDS})\b", re.I)
USDOT_HYPHEN_RE = re.compile(r"\bU\.S\.-", re.I)


def normalise_us_style(text: str):
    replacements = []
    out = USDOT_HYPHEN_RE.sub("US-", text)
    if out != text:
        replacements.append("U.S.- -> US-")

    def _sub_usdot(_m):
        replacements.append("U.S. -> US")
        return "US"
    def _sub_usa(_m):
        replacements.append("USA -> US")
        return "US"

    out2 = US_DOT_RE.sub(_sub_usdot, out)
    out3 = USA_RE.sub(_sub_usa, out2)

    def _hyphenate(m):
        word = m.group(1)
        repl = f"US-{word.lower()}"
        replacements.append(f"US {word.lower()} -> {repl}")
        return repl
    out4 = US_SPACE_COMPOUND_RE.sub(_hyphenate, out3)

    seen = set(); ordered = []
    for r in replacements:
        if r not in seen:
            seen.add(r); ordered.append(r)
    return out4, ordered


# --- American -> British spellings (outside quotes) ---
AMER_TO_BRIT_MAP = {
    r"\bcolor(s?)\b": r"colour\1",
    r"\bfavorite(s?)\b": r"favourite\1",
    r"\bcenter(s?)\b": r"centre\1",
    r"\bmeter(s?)\b": r"metre\1",
    r"\bliter(s?)\b": r"litre\1",
    r"\borganize(r|d|s|ing)?\b": r"organise\1",
    r"\borganization(s)?\b": r"organisation\1",
    r"\banal(y|ys|yz)e(s|d|r|ing)?\b": r"analyse\2",
    r"\bdefense\b": r"defence",
    r"\boffense\b": r"offence",
    r"\blicense\b": r"licence",             # noun
    r"\btrave(l|)ing\b": r"travelling",
    r"\bjewelry\b": r"jewellery",
    r"\bprogram(me)?\b": r"programme",      # non-IT
    r"\bgray\b": r"grey",
    r"\bcurb\b": r"kerb",                   # pavement edge
    r"\bsidewalk\b": r"pavement",
    r"\btire\b": r"tyre",
    r"\bairplane\b": r"aeroplane",
}
QUOTES_RE = re.compile(r"\"[^\"]*\"|'[^']*'")


def replace_american_spellings_british(text: str):
    protected = []
    def _protect(m):
        protected.append(m.group(0))
        return f"@@Q{len(protected)-1}@@"
    tmp = QUOTES_RE.sub(_protect, text)

    replacements = []
    for pat, repl in AMER_TO_BRIT_MAP.items():
        tmp2 = re.sub(pat, repl, tmp, flags=re.I)
        if tmp2 != tmp:
            replacements.append(pat + " -> " + repl)
        tmp = tmp2

    def _unprotect(m):
        idx = int(m.group(0)[3:-2])
        return protected[idx]
    tmp = re.sub(r"@@Q\d+@@", _unprotect, tmp)
    return tmp, list(dict.fromkeys(replacements))


# --- Shortcode placement ---
YAML_BLOCK_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", flags=re.S | re.M)
H1_RE = re.compile(r"(?m)^\#\s+.*$")


def ensure_listen_shortcode(md: str) -> str:
    """
    Ensure a line with exactly [tta_listen_btn] appears immediately after YAML.
    Idempotent. No H1 expected after the shortcode.
    """
    m = YAML_BLOCK_RE.match(md)
    if not m:
        if re.search(r"(?m)^\[tta_listen_btn\]\s*$", md):
            return md
        return "[tta_listen_btn]\n\n" + md

    yaml_end = m.end()
    after_yaml = md[yaml_end:]
    if re.search(r"(?m)^\[tta_listen_btn\]\s*$", after_yaml):
        return md
    return md[:yaml_end] + "[tta_listen_btn]\n\n" + after_yaml.lstrip()


def remove_h1_after_shortcode(md: str) -> str:
    parts = md.split("[tta_listen_btn]", 1)
    if len(parts) != 2:
        return md
    head, tail = parts[0], parts[1]
    tail = H1_RE.sub("", tail).lstrip()
    return head + "[tta_listen_btn]" + ("\n\n" if not tail.startswith("\n") else "") + tail


# --- Title sanitising and YAML helpers ---
ASCII_RE = re.compile(r"[^\x20-\x7E]+")
BRACKETED_RE = re.compile(r"\s*[\[\(\{][^\]\)\}]{0,120}[\]\)\}]\s*")
MULTISPACE_RE = re.compile(r"\s+")
WORD_SPLIT_RE = re.compile(r"\s+")
YAML_TITLES_RE = re.compile(r"(?ms)^---\s*\n(.*?)\n---\s*\n")
SINGLE_TITLE_KEY_RE = re.compile(r'(?m)^\s*title:\s*"(.*?)"\s*$')
TITLE_PRIMARY_RE = re.compile(r'(?m)^\s*title_primary:\s*"(.*?)"\s*$')
TITLE_ALT_1_RE = re.compile(r'(?m)^\s*title_alt_1:\s*"(.*?)"\s*$')
TITLE_ALT_2_RE = re.compile(r'(?m)^\s*title_alt_2:\s*"(.*?)"\s*$')


def _limit_words(s: str, max_words: int = 12) -> str:
    words = WORD_SPLIT_RE.split(s.strip())
    return " ".join(words[:max_words])


def sanitise_title(s: str, max_words: int = 12) -> str:
    s = BRACKETED_RE.sub(" ", s)
    s = s.replace("|", " ").replace("—", ": ").replace("–", ": ").strip()
    s = ASCII_RE.sub(" ", s)
    s = re.sub(r"\s*:\s*", ": ", s)
    s = MULTISPACE_RE.sub(" ", s)
    s = _limit_words(s, max_words=max_words).strip()
    # SEO titles: sentence case, no trailing full stop
    s = s.rstrip(".")
    if s and not s[0].isupper():
        s = s[0].upper() + s[1:]
    return s


def ensure_three_titles_in_yaml(md: str) -> str:
    """
    Ensure YAML has title_primary / title_alt_1 / title_alt_2.
    Migrates legacy titles: list or single title: key if present.
    """
    m = YAML_TITLES_RE.search(md)
    if not m:
        return md
    yaml_block = m.group(1)

    titles = []

    primary = TITLE_PRIMARY_RE.search(yaml_block)
    alt1 = TITLE_ALT_1_RE.search(yaml_block)
    alt2 = TITLE_ALT_2_RE.search(yaml_block)
    if primary or alt1 or alt2:
        for rx in (TITLE_PRIMARY_RE, TITLE_ALT_1_RE, TITLE_ALT_2_RE):
            hit = rx.search(yaml_block)
            if hit:
                titles.append(sanitise_title(hit.group(1)))
            yaml_block = rx.sub("", yaml_block)
        yaml_block = yaml_block.strip()

    single = SINGLE_TITLE_KEY_RE.search(yaml_block)
    if single and not titles:
        seed = sanitise_title(single.group(1))
        titles = [seed]
        yaml_block = SINGLE_TITLE_KEY_RE.sub("", yaml_block).strip()

    list_match = re.search(r'(?ms)^\s*titles:\s*\n(?:\s*-\s*".*?"\s*\n?)+', yaml_block)
    if list_match:
        items = re.findall(r'^\s*-\s*"(.*?)"\s*$', list_match.group(0), flags=re.M)
        titles = [sanitise_title(t) for t in items][:3]
        yaml_block = yaml_block[:list_match.start()] + yaml_block[list_match.end():]

    if not titles:
        titles = [
            "Write a short British-English SEO headline",
            "Provide a concise British-English SEO headline",
            "Offer a compact British-English SEO headline",
        ]
    while len(titles) < 3:
        titles.append(titles[0])

    titles = [sanitise_title(t) for t in titles[:3]]

    # Drop legacy outlet/pub_date/format_rules blocks from YAML if present
    yaml_block = re.sub(r'(?ms)^\s*outlet:\s*.*$', "", yaml_block)
    yaml_block = re.sub(r'(?ms)^\s*pub_date:\s*.*$', "", yaml_block)
    yaml_block = re.sub(r'(?ms)^\s*format_rules:\s*\n(?:\s+\w+:\s*.*\n?)+', "", yaml_block)
    yaml_block = yaml_block.strip()

    title_block = (
        f'title_primary: "{titles[0]}"\n'
        f'title_alt_1: "{titles[1]}"\n'
        f'title_alt_2: "{titles[2]}"\n'
    )
    # Keep description and any other keys
    if yaml_block:
        rebuilt_yaml = title_block + yaml_block
        if not rebuilt_yaml.endswith("\n"):
            rebuilt_yaml += "\n"
    else:
        rebuilt_yaml = title_block

    return md[:m.start(1)] + rebuilt_yaml + md[m.end(1):]


# --- Dateline helpers ---
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def british_ordinal_date(d: date) -> str:
    return f"{ordinal(d.day)} {MONTHS[d.month - 1]} {d.year}"


def choose_dateline_city(outlet: str, text_for_context: str, fallback: str = "London") -> str:
    """
    City rules — use the place where the news occurred:
    - New York if outlet contains Dow Jones
    - Hong Kong if the story is Hong Kong news
    - Otherwise infer from the text (Kyiv, Bangkok, New York, Los Angeles, …), else London
    """
    outlet_l = (outlet or "").lower()
    text_l = (text_for_context or "").lower()
    if "dow jones" in outlet_l:
        return "New York"
    if "hong kong" in text_l or re.search(r"\bHK\b", text_l):
        return "Hong Kong"
    if "kyiv" in text_l or "kiev" in text_l:
        return "Kyiv"
    if "bangkok" in text_l or "suvarnabhumi" in text_l or "thailand" in text_l:
        return "Bangkok"
    if "new york" in text_l or re.search(r"\bwall street\b", text_l):
        return "New York"
    if "los angeles" in text_l or "rancho palos verdes" in text_l:
        return "Los Angeles"
    return fallback


DATELINE_INLINE_PREFIX_RE = re.compile(
    r"^(\d{1,2}(st|nd|rd|th)\s+[A-Z][a-z]+\s+\d{4})\s+-\s+\(([^)]+)\)\s+",
    flags=re.M,
)

FOOTER_SEO_RE = re.compile(r'(?m)^SEO keyword:\s*"?([a-z0-9]+)"?\s*$')
FOOTER_META_RE = re.compile(r'(?m)^Meta description:\s*"?(.{10,300}?)"?\s*$')
FOOTER_BLOCK_RE = re.compile(
    r'(?ms)^\s*Footer\s*\nSEO keyword:.*?\nMeta description:.*?(?:\nCredit:.*?)?(?:\n)?\s*\Z',
    re.I,
)
# Also match two-line footers without a Footer label (legacy)
FOOTER_BLOCK_LOOSE_RE = re.compile(
    r'(?ms)(?:^\s*Footer\s*\n)?SEO keyword:\s*"?[a-z0-9]+"?\s*\nMeta description:\s*.*?(?:\nCredit:.*?)?(?:\n)?\s*\Z',
    re.I,
)
CREDIT_LINE_RE = re.compile(
    r'(?m)^Credit:\s*HK01\s*[—\-]\s*original reporting\.\s*Source:\s*\[HK01\]\((https?://[^)]+)\)\s*$'
)
ATTRIBUTION_HINT_RE = re.compile(
    r'(?im)^(?:Credit:|Source:\s*\[|Reported by\b|Originally published\b)'
)
ASCII_WORD_RE = re.compile(r"^[a-z0-9]+$")
NON_ALPHA_RE = re.compile(r"[^a-z0-9]+")
YAML_DESC_RE = re.compile(r'(?m)^\s*description:\s*"(.*?)"\s*$')


def split_body_and_footer(after_shortcode: str) -> Tuple[str, str]:
    m = re.search(r'(?mi)^\s*Footer\s*$', after_shortcode)
    if m:
        return after_shortcode[:m.start()].rstrip(), after_shortcode[m.start():].strip()
    m2 = re.search(r'(?m)^SEO keyword:\s*', after_shortcode)
    if m2:
        return after_shortcode[:m2.start()].rstrip(), after_shortcode[m2.start():].strip()
    return after_shortcode, ""


def _british_trim_meta(s: str) -> str:
    s = ASCII_RE.sub(" ", s or "")
    s = MULTISPACE_RE.sub(" ", s).strip()
    if len(s) > 160:
        s = s[:160].rstrip()
    return s


def _infer_keyword_from_text(text: str) -> str:
    text = text or ""
    # Drop leading articles so "The Hang Seng" yields hangseng, not sengindex.
    text = re.sub(r"^(?:The|A|An)\s+", "", text.strip())
    for bigram in re.finditer(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-zA-Z]{1,})\b", text):
        first, second = bigram.group(1), bigram.group(2)
        if first.lower() in {"the", "and", "for", "with"}:
            continue
        kw = (first + second).lower()
        kw = NON_ALPHA_RE.sub("", kw)
        if kw and ASCII_WORD_RE.match(kw):
            return kw
    tokens = re.findall(r"[A-Za-z]{4,}", text)
    stop = {
        "after", "before", "about", "found", "woman", "women", "police", "officers",
        "august", "september", "october", "november", "december", "january", "february",
        "march", "april", "june", "july", "that", "this", "their", "there", "were", "called",
        "the", "with", "from", "into", "while", "rose", "fell", "higher", "opened", "index",
    }
    for t in tokens:
        wl = t.lower()
        if wl not in stop:
            kw = NON_ALPHA_RE.sub("", wl)
            if kw:
                return kw
    kw = tokens[0].lower() if tokens else "news"
    kw = NON_ALPHA_RE.sub("", kw)
    return kw if kw else "news"


def _extract_yaml_description(md: str) -> Optional[str]:
    m = YAML_DESC_RE.search(md)
    return m.group(1).strip() if m else None


def _yaml_description(md: str) -> str:
    return _extract_yaml_description(md) or ""


def _fit_meta_length(text: str, low: int = 140, high: int = 160) -> str:
    text = _british_trim_meta(text)
    if len(text) > high:
        return text[:high].rstrip()
    if 0 < len(text) < low:
        pad = " British English news summary."
        while len(text) < low:
            text = (text.rstrip(".") + pad).strip()
            if len(text) >= low:
                break
        if len(text) > high:
            text = text[:high].rstrip()
    return text


def ensure_yaml_description_length(md: str) -> str:
    desc = _yaml_description(md)
    if not desc:
        return md
    fitted = _fit_meta_length(desc)
    if fitted == desc:
        return md
    return YAML_DESC_RE.sub(f'description: "{fitted}"', md, count=1)


def ensure_footer_at_end(md: str, verified_json: Optional[dict] = None) -> str:
    """
    Ensures a Footer block with:
      SEO keyword: <one lowercase ASCII word>
      Meta description: <140–160 chars>
      [optional exclusive Credit line]
    appears as the final content of the document.
    """
    parts = md.split("[tta_listen_btn]", 1)
    body_text = parts[1] if len(parts) == 2 else md
    body_after, old_footer = split_body_and_footer(body_text)
    body_after = body_after.strip()
    first_line = body_after.splitlines()[0].strip() if body_after else ""
    # Prefer content after an inline dateline for keyword inference
    lead_for_kw = first_line
    dm = DATELINE_INLINE_PREFIX_RE.match(first_line + " ")
    if dm:
        lead_for_kw = first_line[len(f"{dm.group(1)} - ({dm.group(3)}) "):].strip() or first_line

    # Prefer YAML description for the footer meta; pad from body if under 140
    yaml_desc = _extract_yaml_description(md) or ""
    meta = _british_trim_meta(yaml_desc)
    if len(meta) < 140:
        pad_src = MULTISPACE_RE.sub(" ", body_after).strip()
        need = 140 - len(meta)
        if pad_src:
            meta = (meta + " " + pad_src).strip() if meta else pad_src
        meta = _british_trim_meta(meta)[:160].rstrip()
        if need and len(meta) < 140:
            meta = _fit_meta_length(meta)
    if len(meta) > 160:
        meta = meta[:160].rstrip()
    if len(meta) < 140:
        meta = _fit_meta_length(meta)

    kw = _infer_keyword_from_text(lead_for_kw)
    if not ASCII_WORD_RE.match(kw):
        kw = "news"

    # Keep an existing keyword only if it still appears as a full word in titles/lead
    existing_kw = FOOTER_SEO_RE.search(md)
    if existing_kw and ASCII_WORD_RE.match(existing_kw.group(1)):
        cand = existing_kw.group(1)
        hay = (lead_for_kw + " " + _yaml_description(md)).lower()
        if re.search(rf"\b{re.escape(cand)}\b", hay) or cand == kw:
            kw = cand if cand == kw or len(cand) >= len(kw) else kw

    # If a footer exists, normalise it; else append a new one (avoid duplicates)
    md_no_footer = FOOTER_BLOCK_LOOSE_RE.sub("", md).rstrip()
    if not md_no_footer.endswith("\n"):
        md_no_footer += "\n"
    footer = f"\n\nFooter\nSEO keyword: {kw}\nMeta description: {meta}\n"

    # Preserve / inject exclusive Credit line when required
    credit_line = None
    existing_credit = CREDIT_LINE_RE.search(old_footer or "") or CREDIT_LINE_RE.search(md)
    if (verified_json or {}).get("exclusive"):
        source_url = (verified_json.get("source_url") or "").strip()
        if existing_credit and (not source_url or existing_credit.group(1) == source_url):
            credit_line = existing_credit.group(0).rstrip()
        elif source_url:
            credit_line = f"Credit: HK01 — original reporting. Source: [HK01]({source_url})"
        elif existing_credit:
            credit_line = existing_credit.group(0).rstrip()
    if credit_line:
        footer += credit_line + "\n"

    return md_no_footer.rstrip() + footer


def ensure_inline_dateline_first_line(md: str, today_str: str, city: str) -> str:
    """
    Ensure the first non-empty line AFTER the shortcode starts with:
    '<today_str> - (<city>) ' followed by content on the same line.
    Preserves any Footer block at the end.
    """
    if "[tta_listen_btn]" not in md:
        return md
    before, after = md.split("[tta_listen_btn]", 1)
    body, footer = split_body_and_footer(after)
    lines = [ln for ln in body.splitlines()]

    while lines and lines[0].strip() == "":
        lines.pop(0)

    if not lines:
        rebuilt = before + "[tta_listen_btn]\n\n"
        if footer:
            rebuilt += footer + "\n"
        return rebuilt

    first = lines[0].strip()
    target_prefix = f"{today_str} - ({city}) "

    if first.startswith(target_prefix):
        if len(first) == len(target_prefix):
            if len(lines) > 1:
                lines[0] = target_prefix + lines[1].lstrip()
                del lines[1]
    else:
        m = DATELINE_INLINE_PREFIX_RE.match(first + " ")
        if m:
            old_prefix = f"{m.group(1)} - ({m.group(3)}) "
            remainder = first[len(old_prefix):].lstrip() if first.startswith(old_prefix) else ""
            if not remainder:
                # Bare dateline line: merge with the next paragraph if present
                if len(lines) > 1:
                    lines[0] = target_prefix + lines[1].lstrip()
                    del lines[1]
                else:
                    lines[0] = target_prefix
            else:
                # Existing inline dateline with content: rewrite prefix only
                lines[0] = target_prefix + remainder
        else:
            lines[0] = target_prefix + first

    rebuilt_body = "\n".join(lines).strip()
    out = before + "[tta_listen_btn]\n\n" + rebuilt_body
    if footer:
        out += "\n\n" + footer
    return out + "\n"


# --- Body length ratio ---
MD_TAGS_RE = re.compile(r"[#>*`\-\[\]()]")


def _strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = MD_TAGS_RE.sub(" ", text)
    return MULTISPACE_RE.sub(" ", text).strip()


def body_text_from_md(md: str) -> str:
    if "[tta_listen_btn]" in md:
        after = md.split("[tta_listen_btn]", 1)[1]
        body, _footer = split_body_and_footer(after)
        return body
    parts = re.split(r"^#\s+.*$", md, maxsplit=1, flags=re.M)
    return parts[1] if len(parts) == 2 else md


def compute_body_length_ratio(original_text: str, draft_md: str) -> Tuple[float, bool]:
    if not original_text or not original_text.strip():
        return (0.0, True)
    body = _strip_markdown(body_text_from_md(draft_md))
    orig = _strip_markdown(original_text)
    if not orig:
        return (0.0, True)
    bw = max(1, len(body.split()))
    ow = max(1, len(orig.split()))
    ratio = round(bw / ow, 2)
    return ratio, (0.60 <= ratio <= 0.75)


def trim_to_target_ratio(draft_md: str, original_text: str, target_low=0.60, target_high=0.75) -> str:
    ratio, ok = compute_body_length_ratio(original_text, draft_md)
    if ok or ratio == 0.0 or ratio < target_low:
        # Too short: do not strip further (deterministic trim only shortens).
        return draft_md

    m = YAML_BLOCK_RE.match(draft_md)
    header = draft_md[:m.end()] if m else ""
    rest = draft_md[m.end():] if m else draft_md

    if re.search(r"(?m)^\[tta_listen_btn\]\s*$", rest.lstrip()):
        sc_line, rest_body = rest.lstrip().split("\n", 1)
        header_plus = header + sc_line + "\n"
    else:
        header_plus = header
        rest_body = rest

    def _rebuild(pre: str, first_line: str, paras: list, footer: str) -> str:
        rebuilt = header_plus + pre + first_line + "\n" + "\n\n".join(paras)
        if footer:
            rebuilt = rebuilt.rstrip() + "\n\n" + footer
        return ensure_listen_shortcode(rebuilt)

    body_only, footer = split_body_and_footer(rest_body)
    h1 = H1_RE.search(body_only)
    if h1:
        h1_start = h1.start()
        pre = body_only[:h1_start]
        body = body_only[h1_start:]
        lines = body.splitlines()
        if not lines:
            return draft_md
        first_line = lines[0]
        paras = "\n".join(lines[1:]).split("\n\n")
        best = draft_md
        while len(paras) > 3:
            trial_paras = paras[:-1]
            test_md = _rebuild(pre, first_line, trial_paras, footer)
            r, ok = compute_body_length_ratio(original_text, test_md)
            if r < target_low:
                break
            paras = trial_paras
            best = test_md
            if ok:
                break
        return best

    paras = body_only.strip().split("\n\n")
    if not paras:
        return draft_md
    best = draft_md
    while len(paras) > 3:
        trial_paras = paras[:-1]
        test_md = header_plus + "\n" + "\n\n".join(trial_paras)
        if footer:
            test_md = test_md.rstrip() + "\n\n" + footer
        test_md = ensure_listen_shortcode(test_md)
        r, ok = compute_body_length_ratio(original_text, test_md)
        if r < target_low:
            break
        paras = trial_paras
        best = test_md
        if ok:
            break
    return best


# --- Pipeline hooks ---

def _first_body_line_after_shortcode(md: str) -> str:
    if "[tta_listen_btn]" not in md:
        return ""
    after = md.split("[tta_listen_btn]", 1)[1]
    body, _ = split_body_and_footer(after)
    for ln in body.splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def _extract_yaml_titles(md: str) -> List[str]:
    titles = []
    for rx in (TITLE_PRIMARY_RE, TITLE_ALT_1_RE, TITLE_ALT_2_RE):
        hit = rx.search(md)
        if hit:
            titles.append(hit.group(1))
    if titles:
        return titles
    # Legacy titles: list
    tm = re.search(r'(?ms)^\s*titles:\s*\n((?:\s*-\s*".*?"\s*\n?)+)', md)
    if not tm:
        return []
    return re.findall(r'^\s*-\s*"(.*?)"\s*$', tm.group(1), flags=re.M)


def _title_issues(titles: List[str]) -> List[str]:
    issues = []
    if len(titles) != 3:
        issues.append(f"expected exactly 3 titles, found {len(titles)}")
    for i, t in enumerate(titles, 1):
        if not re.fullmatch(r"[\x20-\x7E]+", t or ""):
            issues.append(f"title {i}: non-ASCII characters")
        words = [w for w in t.split() if w]
        if len(words) > 12:
            issues.append(f"title {i}: {len(words)} words (max 12)")
        if t.endswith("."):
            issues.append(f"title {i}: should not end with '.'")
        if "—" in t or "–" in t:
            issues.append(f"title {i}: use ': ' not dash for clauses")
        if re.search(r"[\[\]\(\)\{\}\|]", t):
            issues.append(f"title {i}: contains brackets or pipes")
        if t != sanitise_title(t):
            issues.append(f"title {i}: needs sanitising to '{sanitise_title(t)}'")
    return issues


def _parse_footer_fields(md: str) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Returns (seo_keyword, meta_description, footer_is_last_ok).
    Canonical form:
      Footer
      SEO keyword: word
      Meta description: text
      [optional Credit line when exclusive]
    """
    m = re.search(
        r'(?mis)^\s*Footer\s*\nSEO keyword:\s*"?([a-z0-9]+)"?\s*\nMeta description:\s*"?(.*?)"?\s*(?:\nCredit:[^\n]*)?\s*\Z',
        md,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip(), True
    # Legacy two-line footer without label
    m2 = re.search(
        r'(?ms)^SEO keyword:\s*"?([a-z0-9]+)"?\s*\nMeta description:\s*"?(.*?)"?\s*(?:\nCredit:[^\n]*)?\s*\Z',
        md,
    )
    if m2:
        return m2.group(1).strip(), m2.group(2).strip(), True
    m3 = re.search(
        r'(?mis)Footer\s*\nSEO keyword:\s*"?([a-z0-9]+)"?\s*\nMeta description:\s*"?([^\n]*)"?\s*',
        md,
    )
    if not m3:
        return None, None, False
    after = md[m3.end():]
    # Allow optional exclusive Credit line only
    after_stripped = after.strip()
    if after_stripped == "" or CREDIT_LINE_RE.match(after_stripped):
        return m3.group(1).strip(), m3.group(2).strip(), True
    return m3.group(1).strip(), m3.group(2).strip(), False


def body_word_count(draft_md: str) -> int:
    body = _strip_markdown(body_text_from_md(draft_md))
    return len([w for w in body.split() if w])


def is_very_short_brief(original_source_text: str) -> bool:
    words = len([w for w in _strip_markdown(original_source_text or "").split() if w])
    return words > 0 and words < 180


def check_exclusive_credit(draft_md: str, verified_json: Optional[dict]) -> Tuple[bool, bool]:
    """
    Returns (exclusive_credit_ok, no_source_reference_ok).
    """
    verified_json = verified_json or {}
    exclusive = bool(verified_json.get("exclusive"))
    source_url = (verified_json.get("source_url") or "").strip()
    credit_m = CREDIT_LINE_RE.search(draft_md)
    has_credit = credit_m is not None
    # Broader attribution scan excluding the allowed Credit line
    body_and_footer = draft_md
    if has_credit:
        body_and_footer = CREDIT_LINE_RE.sub("", draft_md)
    stray_attr = bool(ATTRIBUTION_HINT_RE.search(body_and_footer))

    if exclusive:
        url_ok = bool(credit_m and (not source_url or credit_m.group(1) == source_url))
        exclusive_ok = has_credit and url_ok and not stray_attr
        return exclusive_ok, exclusive_ok
    # Non-exclusive: no Credit / attribution lines
    ok = (not has_credit) and (not stray_attr)
    return ok, ok


def augment_validator_json_with_deterministic_checks(
    validator_json: dict,
    draft_md: str,
    outlet: str,
    original_source_text: str,
    today_iso: Optional[str] = None,
    verified_json: Optional[dict] = None,
) -> dict:
    flags = list(validator_json.get("flags") or [])
    suggestions = list(validator_json.get("suggestions") or [])

    contains_colon, colon_suggestions = find_colon_times_with_suggestions(draft_md)
    validator_json["contains_colon_times"] = contains_colon
    validator_json["colon_time_suggestions"] = colon_suggestions
    if contains_colon:
        validator_json["time_style_ok"] = False

    # Style checks on body only (YAML may mention U.S./USA in rules)
    yaml_m = YAML_BLOCK_RE.match(draft_md)
    style_scope = draft_md[yaml_m.end():] if yaml_m else draft_md
    _, us_repls = normalise_us_style(style_scope)
    if us_repls:
        validator_json["us_style_ok"] = False
        for r in us_repls:
            if r not in suggestions:
                suggestions.append(r)
    else:
        validator_json.setdefault("us_style_ok", True)

    _, brit_repls = replace_american_spellings_british(style_scope)
    if brit_repls:
        validator_json["british_spelling_ok"] = False
        validator_json["american_spellings_found"] = list(dict.fromkeys(brit_repls))
    else:
        validator_json.setdefault("british_spelling_ok", True)
        validator_json.setdefault("american_spellings_found", [])

    # shortcode_ok: exact shortcode line after YAML, and no H1 after it
    after_yaml = draft_md[yaml_m.end():] if yaml_m else draft_md
    has_sc = bool(re.search(r"(?m)^\[tta_listen_btn\]\s*$", after_yaml))
    no_h1 = True
    if "[tta_listen_btn]" in draft_md:
        tail = draft_md.split("[tta_listen_btn]", 1)[1]
        no_h1 = not bool(H1_RE.search(tail))
    shortcode_ok = has_sc and no_h1
    validator_json["shortcode_ok"] = shortcode_ok
    validator_json["no_h1_after_shortcode"] = no_h1
    if not has_sc:
        flags.append("Missing [tta_listen_btn] after YAML.")
    if not no_h1:
        flags.append("Remove H1 after [tta_listen_btn].")

    # Titles
    titles = _extract_yaml_titles(draft_md)
    title_issues = _title_issues(titles)
    titles_ok = len(title_issues) == 0
    validator_json["titles_ok"] = titles_ok
    validator_json["title_issues"] = title_issues
    validator_json["titles_count"] = len(titles)
    if not titles_ok:
        flags.append("titles must be exactly 3 SEO headlines (title_primary, title_alt_1, title_alt_2)")
        for issue in title_issues:
            if issue not in suggestions:
                suggestions.append(issue)

    # Dateline — city where the news occurred
    today_obj = date.today() if not today_iso else date.fromisoformat(today_iso)
    today_str = british_ordinal_date(today_obj)
    expected_city = choose_dateline_city(outlet, f"{draft_md}\n{original_source_text}")
    first = _first_body_line_after_shortcode(draft_md)
    target_prefix = f"{today_str} - ({expected_city}) "
    m = DATELINE_INLINE_PREFIX_RE.match(first + " ")
    dateline_date_ok = False
    dateline_city = ""
    inline_dateline_ok = False
    if m and len(first) > len(f"{m.group(1)} - ({m.group(3)}) "):
        dateline_city = m.group(3)
        dateline_date_ok = (m.group(1) == today_str)
        inline_dateline_ok = dateline_date_ok and (dateline_city == expected_city) and first.startswith(target_prefix)
    validator_json["inline_dateline_ok"] = inline_dateline_ok
    validator_json["dateline_ok"] = inline_dateline_ok
    validator_json["dateline_date_ok"] = dateline_date_ok
    validator_json["dateline_city"] = dateline_city
    validator_json["dateline_expected_city"] = expected_city
    if not inline_dateline_ok:
        flags.append("Inline dateline missing or incorrect.")
        tip = f'Start first body line with: "{target_prefix}"'
        if tip not in suggestions:
            suggestions.append(tip)

    # Footer checks (Footer label + SEO keyword + Meta description as last block)
    seo_kw, meta_ft, footer_is_last = _parse_footer_fields(draft_md)
    has_footer_label = bool(re.search(r'(?mi)^\s*Footer\s*$', draft_md))
    has_seo_line = bool(FOOTER_SEO_RE.search(draft_md))
    has_meta_line = bool(FOOTER_META_RE.search(draft_md) or re.search(r'(?m)^Meta description:\s*\S', draft_md))
    footer_ok = has_footer_label and has_seo_line and has_meta_line and seo_kw is not None and meta_ft is not None
    seo_keyword_ok = bool(seo_kw) and bool(ASCII_WORD_RE.match(seo_kw or ""))
    meta_footer_ok = meta_ft is not None and meta_ft != ""
    meta_footer_len = len(meta_ft or "")
    meta_footer_length_ok = 0 < meta_footer_len <= 160

    validator_json["footer_ok"] = footer_ok
    validator_json["seo_keyword_ok"] = seo_keyword_ok
    validator_json["meta_footer_ok"] = meta_footer_ok
    validator_json["meta_footer_length_ok"] = meta_footer_length_ok
    validator_json["footer_is_last_ok"] = bool(footer_is_last) if footer_ok else False

    if not footer_ok:
        flags.append("Add Footer with SEO keyword and Meta description.")
    if footer_ok and not seo_keyword_ok:
        flags.append("SEO keyword must be one lowercase ASCII word ([a-z0-9]+).")
        suggestions.append("Use SEO keyword: example")
    if footer_ok and not meta_footer_ok:
        flags.append("Footer Meta description missing.")
    if footer_ok and not meta_footer_length_ok:
        flags.append(f"Footer Meta description length {meta_footer_len} (expected ≤160).")
    if footer_ok and not footer_is_last:
        flags.append("Footer Meta description must be last (optional exclusive Credit line / trailing newline only).")

    # YAML meta description ≤160
    desc = _yaml_description(draft_md)
    if desc:
        validator_json["meta_ok"] = 0 < len(desc) <= 160
        if not validator_json["meta_ok"]:
            flags.append(f"YAML description length {len(desc)} (expected ≤160).")

    # Exclusive credit / attribution
    exclusive_ok, no_src_ok = check_exclusive_credit(draft_md, verified_json)
    validator_json["exclusive_credit_ok"] = exclusive_ok
    validator_json["no_source_reference_ok"] = no_src_ok
    if not exclusive_ok:
        if (verified_json or {}).get("exclusive"):
            flags.append('Add exclusive Credit line under Footer: Credit: HK01 — original reporting. Source: [HK01](<url>)')
        else:
            flags.append("Remove attribution / Credit lines (exclusive is false).")

    # no CJK
    if any("\u4e00" <= c <= "\u9fff" for c in draft_md):
        validator_json["no_cjk_ok"] = False
        flags.append("Remove CJK characters from draft.")

    # Absolute word-count target (220–520) unless body_hint is a very short brief
    wc = body_word_count(draft_md)
    short_brief = is_very_short_brief(original_source_text)
    if short_brief:
        wc_ok = wc > 0
    else:
        wc_ok = 220 <= wc <= 520
    validator_json["body_word_count"] = wc
    validator_json["body_word_count_ok"] = wc_ok
    if not wc_ok:
        flags.append(f"Body word count {wc} (expected 220–520 unless short brief).")

    ratio, ok_ratio = compute_body_length_ratio(original_source_text, draft_md)
    validator_json["body_length_ratio"] = ratio
    # Prefer absolute word-count gate; keep ratio as secondary signal
    validator_json["body_length_ok"] = wc_ok if not short_brief else (ok_ratio or wc_ok)
    if not validator_json["body_length_ok"] and ratio != 0.0 and not short_brief:
        flags.append(f"Body length ratio {ratio} (expected 0.60–0.75)")

    # Deduplicate flags/suggestions while preserving order
    validator_json["flags"] = list(dict.fromkeys(flags))
    validator_json["suggestions"] = list(dict.fromkeys(suggestions))
    return validator_json


def apply_deterministic_prefixes(
    draft_md: str,
    outlet: str,
    original_source_text: str,
    today_iso: Optional[str] = None,
    verified_json: Optional[dict] = None,
) -> str:
    md = draft_md

    # 1) Normalise US/British spellings and times (YAML protected)
    yaml_m = YAML_BLOCK_RE.match(md)
    if yaml_m:
        yaml_block = md[:yaml_m.end()]
        body = md[yaml_m.end():]
        body, _ = normalise_us_style(body)
        body, _ = replace_american_spellings_british(body)
        body = replace_colon_times(body)
        md = yaml_block + body
    else:
        md, _ = normalise_us_style(md)
        md, _ = replace_american_spellings_british(md)
        md = replace_colon_times(md)

    # 2) Ensure shortcode and remove any H1 after it
    md = ensure_listen_shortcode(md)
    md = remove_h1_after_shortcode(md)

    # 3) Titles: force exactly three, sanitised, in YAML
    md = ensure_three_titles_in_yaml(md)
    md = ensure_yaml_description_length(md)

    # 4) Dateline (inline, first line) — city where the news occurred
    today_obj = date.today() if not today_iso else date.fromisoformat(today_iso)
    today_str = british_ordinal_date(today_obj)
    city = choose_dateline_city(outlet, f"{md}\n{original_source_text}")
    md = ensure_inline_dateline_first_line(md, today_str, city)

    # 5) Optional proportion trim
    md = trim_to_target_ratio(md, original_source_text)

    # 6) Idempotent shortcode again
    md = ensure_listen_shortcode(md)
    md = remove_h1_after_shortcode(md)

    # 7) Final inline dateline guard
    md = ensure_inline_dateline_first_line(md, today_str, city)

    # 8) Footer (SEO keyword + Meta description [+ exclusive Credit]) as last block
    md = ensure_footer_at_end(md, verified_json=verified_json)
    return md

# ----------------------------
# LLM helpers
# ----------------------------

def extract_json_from_text(text: str):
    """
    Try to extract a JSON object from a model response.
    Handles plain JSON or fenced code blocks.
    """
    # Try code block with json
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if m:
        candidate = m.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            pass
    # Try first {...} spanning block
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        candidate = text[brace_start:brace_end+1]
        try:
            return json.loads(candidate)
        except Exception:
            pass
    # Last resort: direct parse
    try:
        return json.loads(text)
    except Exception:
        return None

def llm_complete(client, model, system_prompt, user_prompt, temperature=0.2, max_output_tokens=2000):
    """
    Wrapper for chat completion. Adjust to your SDK version as needed.
    """
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_prompt.strip()},
        ],
        max_tokens=max_output_tokens,
    )
    return resp.choices[0].message.content

def force_json_completion(client, model, system_prompt, user_prompt, temperature=0.0, retries=2):
    """
    Ensure we get valid JSON back. Retries with a stricter instruction if needed.
    """
    content = llm_complete(client, model, system_prompt, user_prompt, temperature=temperature)
    data = extract_json_from_text(content)
    attempt = 0
    while data is None and attempt < retries:
        attempt += 1
        tightened = user_prompt + "\n\nReturn STRICT JSON only. No prose. If you include code fences, include ONLY the JSON inside."
        content = llm_complete(client, model, system_prompt, tightened, temperature=0.0)
        data = extract_json_from_text(content)
    if data is None:
        raise ValueError("Failed to parse JSON from model response:\n" + content)
    return data, content

# ----------------------------
# Pipeline steps
# ----------------------------

def run_verifier(client, model, notes, temperature=0.2):
    user = VERIFIER_USER_TEMPLATE.replace("{{notes}}", notes)
    data, raw = force_json_completion(
        client, model,
        system_prompt="You are a meticulous fact-checking assistant. Return strict JSON only.",
        user_prompt=user,
        temperature=temperature
    )
    return data, raw

def run_writer_markdown(
    client,
    model,
    verified_json,
    notes,
    outlet,
    pub_date,
    temperature=0.2,
    today_iso: Optional[str] = None,
    editor_notes: str = "",
):
    today_obj = date.today() if not today_iso else date.fromisoformat(today_iso)
    today_str = british_ordinal_date(today_obj)

    user = WRITER_USER_TEMPLATE_MD.replace(
        "<<<PASTE_JSON_FROM_VERIFIER_HERE>>>",
        json.dumps(verified_json, ensure_ascii=False, indent=2)
    )
    user = user.replace("{body_hint}", notes)
    user = user.replace("{editor_notes}", editor_notes or "British English house style; inverted pyramid; ≤160 char meta.")
    user = user.replace("{today_date_ordinal}", today_str)
    md = llm_complete(
        client, model,
        system_prompt=HOUSE_STYLE_SYSTEM,
        user_prompt=user,
        temperature=temperature,
        max_output_tokens=2200
    )
    return md

def run_validator(client, model, draft_md, outlet, pub_date, temperature=0.0, original_source_text: str = "", verified_json: Optional[dict] = None):
    user = VALIDATOR_USER_TEMPLATE.replace("<<<DRAFT>>>", draft_md)
    user = user.replace("<<<ORIGINAL_SOURCE_TEXT>>>", original_source_text or "")
    user = user.replace(
        "<<<PASTE_JSON_FROM_VERIFIER_HERE>>>",
        json.dumps(verified_json or {}, ensure_ascii=False, indent=2),
    )
    user = user.replace("{{outlet}}", outlet or "").replace("{{pub_date}}", pub_date or "")
    data, raw = force_json_completion(
        client, model,
        system_prompt="You are a strict copy‑desk JSON validator. Respond with JSON only.",
        user_prompt=user,
        temperature=temperature
    )
    return data, raw

def run_autofix_markdown(client, model, draft_md, outlet, pub_date, temperature=0.0, original_source_text: str = "", verified_json: Optional[dict] = None):
    verified_blob = json.dumps(verified_json or {}, ensure_ascii=False, indent=2)
    user = AUTOFIX_USER_TEMPLATE_MD.replace("<<<DRAFT>>>", draft_md)
    user = user.replace("<<<ORIGINAL_SOURCE_TEXT>>>", original_source_text or "")
    user = user.replace("<<<VERIFIED_JSON>>>", verified_blob)
    user = user.replace("<<<PASTE_JSON_FROM_VERIFIER_HERE>>>", verified_blob)
    user = user.replace("{outlet}", outlet or "").replace("{pub_date}", pub_date or "")
    fixed_md = llm_complete(
        client, model,
        system_prompt="You are a precise newsroom auto-fixer. Reduce source similarity while preserving facts and template structure. Return Markdown only.",
        user_prompt=user,
        temperature=temperature,
        max_output_tokens=2200
    )
    return fixed_md

# ----------------------------
# File helpers
# ----------------------------

def default_paths(notes_path: Path):
    base = notes_path.with_suffix("")  # strip .txt etc.
    return {
        "verification": base.parent / "verification.json",
        "draft_md": base.parent / "article_draft.md",
        "fixed_md": base.parent / "article_fixed.md",
        "validator": base.parent / "validator.json",
    }

# ----------------------------
# CLI and main
# ----------------------------

def main():
    ap = argparse.ArgumentParser(description="Newsroom Markdown pipeline (Verifier → Writer → Validator → Auto-fix)")
    ap.add_argument("--notes", required=True, help="Path to a UTF-8 text file with your source notes")
    ap.add_argument("--outlet", required=True, help="Attribution outlet, e.g., 'South China Morning Post'")
    ap.add_argument("--pub-date", required=True, help="Attribution date in ordinal format, e.g., '30th June 2026'")
    ap.add_argument("--editor-notes", default=None, help="Path to editor/house-style notes (default: notes.md beside --notes)")
    ap.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4.1"), help="OpenAI model (default: gpt-4.1)")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--out-dir", default=None, help="Optional output directory for artifacts")
    ap.add_argument("--no-autofix", action="store_true", help="Disable Auto-fix even if validator flags issues")
    ap.add_argument("--today", default=None, help="Override today as ISO date YYYY-MM-DD for dateline checks")
    args = ap.parse_args()

    notes_path = Path(args.notes).expanduser().resolve()
    if not notes_path.exists():
        print(f"Notes file not found: {notes_path}", file=sys.stderr)
        sys.exit(1)

    with open(notes_path, "r", encoding="utf-8") as f:
        notes = f.read().strip()

    editor_notes_path = Path(args.editor_notes).expanduser().resolve() if args.editor_notes else (notes_path.parent / "notes.md")
    if editor_notes_path.exists():
        editor_notes = editor_notes_path.read_text(encoding="utf-8").strip()
    else:
        editor_notes = ""

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Error: OPENAI_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    out_paths = default_paths(notes_path)
    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_paths = {
            "verification": out_dir / "verification.json",
            "draft_md": out_dir / "article_draft.md",
            "fixed_md": out_dir / "article_fixed.md",
            "validator": out_dir / "validator.json",
        }

    client = OpenAI()

    # Step 1: Verifier
    print("=== Step 1: Verifier (JSON) ===")
    verified_json, _ = run_verifier(client, args.model, notes, temperature=args.temperature)
    out_paths["verification"].write_text(json.dumps(verified_json, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {out_paths['verification']}")

    # Step 2: Writer (Markdown)
    print("=== Step 2: Writer (Markdown) ===")
    draft_md = run_writer_markdown(
        client, args.model, verified_json, notes,
        outlet=args.outlet, pub_date=args.pub_date,
        temperature=args.temperature,
        today_iso=getattr(args, "today", None),
        editor_notes=editor_notes,
    )

    # Deterministic placement and fixes before validation
    original_source_text = notes
    draft_md = apply_deterministic_prefixes(
        draft_md,
        outlet=args.outlet or "",
        original_source_text=original_source_text,
        today_iso=getattr(args, "today", None),
        verified_json=verified_json,
    )
    out_paths["draft_md"].write_text(draft_md, encoding="utf-8")
    print(f"Saved: {out_paths['draft_md']}")

    # Step 3: Validator
    print("=== Step 3: Validator (JSON) ===")
    validator_json, _ = run_validator(
        client, args.model, draft_md,
        outlet=args.outlet, pub_date=args.pub_date, temperature=0.0,
        original_source_text=original_source_text,
        verified_json=verified_json,
    )
    validator_json = augment_validator_json_with_deterministic_checks(
        validator_json,
        draft_md=draft_md,
        outlet=args.outlet or "",
        original_source_text=original_source_text,
        today_iso=getattr(args, "today", None),
        verified_json=verified_json,
    )
    out_paths["validator"].write_text(json.dumps(validator_json, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {out_paths['validator']}")

    needs_fix = any([
        not validator_json.get("meta_ok", False),
        not validator_json.get("date_style_ok", False),
        not validator_json.get("time_style_ok", False),
        validator_json.get("contains_colon_times", False),
        not validator_json.get("us_style_ok", False),
        not validator_json.get("british_spelling_ok", True),
        not validator_json.get("quotes_ok", False),
        not validator_json.get("all_names_verified", False),
        not validator_json.get("no_cjk_ok", True),
        not validator_json.get("all_items_have_sources", True),
        not validator_json.get("no_source_reference_ok", True),
        not validator_json.get("exclusive_credit_ok", True),
        not validator_json.get("shortcode_ok", False),
        not validator_json.get("inline_dateline_ok", False),
        not validator_json.get("dateline_ok", False),
        not validator_json.get("titles_ok", True),
        not validator_json.get("footer_ok", True),
        not validator_json.get("seo_keyword_ok", True),
        not validator_json.get("meta_footer_ok", True),
        not validator_json.get("meta_footer_length_ok", True),
        not validator_json.get("footer_is_last_ok", True),
        not validator_json.get("body_length_ok", True),
        not validator_json.get("body_word_count_ok", True),
        not validator_json.get("structure_ok", True),
        not validator_json.get("no_copied_strings_ok", True),
        not validator_json.get("no_hallucinations_ok", True),
    ])

    if needs_fix and not args.no_autofix:
        print("=== Step 4: Auto-fix (style-only) ===")
        pre_md = apply_deterministic_prefixes(
            draft_md,
            outlet=args.outlet or "",
            original_source_text=original_source_text,
            today_iso=getattr(args, "today", None),
        )

        fixed_md = run_autofix_markdown(
            client, args.model, pre_md,
            outlet=args.outlet, pub_date=args.pub_date, temperature=0.0,
            original_source_text=original_source_text,
            verified_json=verified_json,
        )

        # Make absolutely sure shortcode, titles, and inline dateline are correct post-model
        fixed_md = ensure_listen_shortcode(fixed_md)
        fixed_md = remove_h1_after_shortcode(fixed_md)
        fixed_md = ensure_three_titles_in_yaml(fixed_md)
        today_obj = date.today() if not getattr(args, "today", None) else date.fromisoformat(args.today)
        today_str = british_ordinal_date(today_obj)
        city = choose_dateline_city(args.outlet or "", f"{fixed_md}\n{original_source_text}")
        fixed_md = ensure_inline_dateline_first_line(fixed_md, today_str, city)
        fixed_md = ensure_footer_at_end(fixed_md, verified_json=verified_json)

        draft_md = fixed_md
        out_paths["fixed_md"].write_text(fixed_md, encoding="utf-8")
        print(f"Saved: {out_paths['fixed_md']}")

        # Re-validate fixed version
        print("=== Re-validate after Auto-fix ===")
        re_val_json, _ = run_validator(
            client, args.model, fixed_md,
            outlet=args.outlet, pub_date=args.pub_date, temperature=0.0,
            original_source_text=original_source_text,
            verified_json=verified_json,
        )
        re_val_json = augment_validator_json_with_deterministic_checks(
            re_val_json,
            draft_md=fixed_md,
            outlet=args.outlet or "",
            original_source_text=original_source_text,
            today_iso=getattr(args, "today", None),
            verified_json=verified_json,
        )
        out_paths["validator"].write_text(json.dumps(re_val_json, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Updated validator: {out_paths['validator']}")
    else:
        if needs_fix:
            print("Validator flagged issues, but --no-autofix was provided. Please review article_draft.md manually.")
        else:
            print("All checks passed on first pass. No Auto-fix needed.")

if __name__ == "__main__":
    main()
