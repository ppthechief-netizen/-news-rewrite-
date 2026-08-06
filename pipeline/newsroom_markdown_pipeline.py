"""Deterministic Markdown post-processing for the newsroom CLI.

Wraps the full implementation in the repo-root ``newsroom_markdown_pipeline.py``
so ``cli/main.py`` can keep a stable import path and ``source_url`` / ``exclusive``
signature while sharing footer, dateline, and style enforcement.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

_ROOT_PATH = Path(__file__).resolve().parent.parent / "newsroom_markdown_pipeline.py"
_SPEC = importlib.util.spec_from_file_location("newsroom_markdown_pipeline_root", _ROOT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Cannot load newsroom pipeline from {_ROOT_PATH}")
_ROOT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ROOT)

# Re-export helpers used by tests / callers
ensure_footer_at_end = _ROOT.ensure_footer_at_end
ensure_yaml_description_length = _ROOT.ensure_yaml_description_length
clamp_meta_description = _ROOT.ensure_yaml_description_length


def apply_deterministic_prefixes(
    draft_md: str,
    outlet: str,
    original_source_text: str,
    today_iso: str,
    source_url: str = "",
    exclusive: bool = False,
) -> str:
    """
    Apply house-style deterministic fixes, then ensure a Footer block with:
      SEO keyword: <one lowercase ASCII word>
      Meta description: <140–160 chars>
    as the final content (plus optional exclusive Credit line).
    """
    verified_json = {
        "exclusive": bool(exclusive),
        "source_url": (source_url or "").strip(),
    }
    return _ROOT.apply_deterministic_prefixes(
        draft_md=draft_md,
        outlet=outlet or "",
        original_source_text=original_source_text or "",
        today_iso=today_iso,
        verified_json=verified_json,
    )
