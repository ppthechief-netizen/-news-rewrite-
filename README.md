# HK01 Cursor Project

A separate Cursor project that scrapes HK01 Hong Kong News, rewrites to British-English Markdown, and conditionally credits HK01 for exclusives.

## Setup

1. `pip install -r requirements.txt`
2. `python -m playwright install chromium`
3. `cp .env.example .env` and set `OPENAI_API_KEY`.
4. In Cursor, add a task or run:

```bash
python -m pipeline.run_once latest --n 10
```

Or poll every **15 minutes** and rewrite every new story (skips already-finalised URLs):

```bash
python runner.py
```

Override with `HK01_INTERVAL_SEC` (seconds) and `HK01_FETCH_N` (how many latest links to check each cycle).

**Copy-ready files:** open `rewrites/copy-ready/` (see `INDEX.md` / `LATEST.md`) to copy finished Markdown.

### If OpenAI returns region / 403 errors

Direct `api.openai.com` calls are blocked in some territories (including Hong Kong). Two options:

1. Set `OPENAI_BASE_URL` in `.env` to a supported gateway, then `HK01_RUN_MODE=rewrite`.
2. Keep `HK01_RUN_MODE=inbox` (default when set locally): the runner only queues `rewrites/inbox/*.json` every 15 minutes; rewrite those drafts in Cursor into `rewrites/copy-ready/`.

### Useful options

```bash
python -m pipeline.run_once latest --n 5 --model gpt-4o-mini
python -m pipeline.run_once latest --n 3 --no-paraphrase-strict
python -m pipeline.run_once latest --n 1 --exclusive-override
python -m pipeline.run_once latest --n 1 --no-exclusive-override
```

## What it does

1. Fetches the latest HK01 Hong Kong News article links (`scraper/hk01_list.py`)
2. Extracts title, body, publish time, tags, and exclusivity signals (`scraper/hk01_article.py`)
3. Writes British-English Markdown via writer → validator → autofix prompts (`prompts/`)
4. Re-runs autofix when originality checks fail (`utils/originality.py`)
5. Applies footer / exclusive Credit line deterministically (`pipeline/`)
6. Saves artifacts under `storage/`

## Output

- `storage/final/*.md` contains SEO YAML, listen button, dateline, body, Footer with SEO keyword + meta description.
- If story is exclusive (heuristics or `--exclusive-override true`), appends:

```text
Credit: HK01 — original reporting. Source: [HK01](<source_url>)
```

- Intermediate artifacts:
  - `storage/json/` — extracted article metadata
  - `storage/drafts/` — writer Markdown
  - `storage/fixed/` — post-autofix Markdown

## Layout

```
cli/main.py                 # Typer entrypoint
scraper/                    # HK01 list + article fetch
utils/                      # dates, originality metrics
pipeline/                   # deterministic Markdown post-process
prompts/                    # writer / validator / autofix templates
storage/                    # runtime artifacts
requirements.txt
.env.example
```

## Flags

- `--n 5` fetch the latest 5.
- `--model gpt-4o-mini` change model.
- `--paraphrase-strict/--no-paraphrase-strict` control originality loop.
- `--exclusive-override true|false` force crediting on/off.

## Notes

- Heuristics for exclusivity look for 「獨家」「專訪」「專題」and similar tokens in title/tags/meta. You can fine-tune in `scraper/selectors.py`.
- The pipeline never stores source text in final output; it uses it only for paraphrase checks.
- Respect hk01.com Terms and robots.txt.

## CI

GitHub Actions runs every 15 minutes (and on manual dispatch) via `.github/workflows/hk01-pipeline.yml`.

1. Add repository secret `OPENAI_API_KEY`.
2. Ensure the default branch allows the `github-actions` bot to push (contents: write).
3. Outputs land in `storage/final`, `storage/fixed`, `storage/json`, and `logs/runner.log`.
