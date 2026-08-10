"""CSS selectors and patterns for article extraction."""

BODY_SELECTORS = [
    ".article-grid__content-section",
    ".cmp-article-detail",
    "article [itemprop='articleBody']",
    "article .article-content",
    "main article",
    "article",
    "section.article-content",
]

TITLE_SELECTORS = [
    "meta[property='og:title']",
    "h1",
]

TIME_SELECTORS = [
    "meta[property='article:published_time']",
    "time[datetime]",
]

EXCLUSIVE_PATTERNS = [
    "獨家", "專訪", "專題", "調查", "深度", "記者會直擊", "本報獨訊", "獨家專訪",
    "Exclusive", "Investigative",
]
