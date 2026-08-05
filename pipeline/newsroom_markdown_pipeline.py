from __future__ import annotations

import re

FOOTER_PATTERN = re.compile(
    r"(?s)---\s*Footer\s*SEO keyword:\s*([a-z]+)\s*Meta description:\s*(.{0,200})"
)


def ensure_footer_at_end(md: str) -> str:
    if "Footer" not in md or "SEO keyword:" not in md or "Meta description:" not in md:
        # Append a deterministic stub; writer/autofix should have produced it already
        md = md.rstrip() + "\n\n---\nFooter\nSEO keyword: news\nMeta description: Concise British-English summary.\n"
    return md


def maybe_add_credit(md: str, source_url: str, exclusive: bool) -> str:
    credit_line = f"Credit: HK01 — original reporting. Source: [HK01]({source_url})"
    if exclusive:
        # ensure it's directly under Footer block
        if credit_line not in md:
            md = md.rstrip() + f"\n{credit_line}\n"
    else:
        # remove any accidental credit
        md = re.sub(r"^Credit: HK01 .*?$", "", md, flags=re.MULTILINE).rstrip() + "\n"
    return md


def clamp_meta_description(md: str) -> str:
    md = re.sub(r"(Meta description:\s*)(.*)", lambda m: m.group(1) + m.group(2)[:160], md)
    return md


def apply_deterministic_prefixes(
    draft_md: str,
    outlet: str,
    original_source_text: str,
    today_iso: str,
    source_url: str,
    exclusive: bool,
) -> str:
    md = draft_md
    md = ensure_footer_at_end(md)
    md = clamp_meta_description(md)
    md = maybe_add_credit(md, source_url=source_url, exclusive=exclusive)
    return md
