"""Web search and URL fetch tools."""

from __future__ import annotations

import html.parser
import re

import httpx

from aru.tools._shared import _truncate_output


class _HTMLToText(html.parser.HTMLParser):
    """HTML-to-text converter with improved content extraction."""

    SKIP_TAGS = {"script", "style", "svg", "noscript", "head", "nav", "footer",
                 "iframe", "form", "button", "input", "select", "textarea"}
    BLOCK_TAGS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
                  "li", "tr", "blockquote", "pre", "section", "article",
                  "header", "main", "figcaption", "details", "summary", "dt", "dd"}
    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    LIST_TAGS = {"li"}

    def __init__(self):
        super().__init__()
        self._pieces: list[str] = []
        self._skip_depth = 0
        self._in_pre = False
        self._in_anchor = False
        self._anchor_href = ""

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth:
            return
        elif tag == "pre":
            self._in_pre = True
            self._pieces.append("\n```\n")
        elif tag == "code" and not self._in_pre:
            self._pieces.append("`")
        elif tag == "a":
            self._in_anchor = True
            attrs_dict = dict(attrs)
            self._anchor_href = attrs_dict.get("href", "")
        elif tag in self.HEADING_TAGS:
            level = int(tag[1])
            self._pieces.append(f"\n{'#' * level} ")
        elif tag in self.LIST_TAGS:
            self._pieces.append("\n- ")
        elif tag in self.BLOCK_TAGS:
            self._pieces.append("\n")
        elif tag == "br":
            self._pieces.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif self._skip_depth:
            return
        elif tag == "pre":
            self._in_pre = False
            self._pieces.append("\n```\n")
        elif tag == "code" and not self._in_pre:
            self._pieces.append("`")
        elif tag == "a":
            if self._anchor_href and not self._anchor_href.startswith(("#", "javascript:")):
                self._pieces.append(f" ({self._anchor_href})")
            self._in_anchor = False
            self._anchor_href = ""
        elif tag in self.HEADING_TAGS:
            self._pieces.append("\n")
        elif tag in self.BLOCK_TAGS:
            self._pieces.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            if self._in_pre:
                self._pieces.append(data)
            else:
                self._pieces.append(data)

    def get_text(self) -> str:
        raw = "".join(self._pieces)
        lines = [" ".join(line.split()) if not line.startswith("```") else line
                 for line in raw.splitlines()]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
        return text.strip()


def _html_to_text(html_content: str) -> str:
    parser = _HTMLToText()
    parser.feed(html_content)
    return parser.get_text()


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for information.

    Args:
        query: The search query.
        max_results: Max results to return (default 5).
    """
    results = _ddg_lite_search(query, max_results)
    if not results:
        results = _ddg_html_search(query, max_results)
    if not results:
        return f"No results found for: {query}"
    return "\n\n".join(results)


def _ddg_lite_search(query: str, max_results: int) -> list[str]:
    """Search via DuckDuckGo Lite — minimal HTML, more stable parsing."""
    import re as _re
    import urllib.parse

    try:
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            resp = client.post(
                "https://lite.duckduckgo.com/lite/",
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            resp.raise_for_status()
    except httpx.RequestError:
        return []

    html_text = resp.text
    results = []

    link_pattern = _re.compile(
        r'<a[^>]+class="result-link"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        _re.DOTALL,
    )
    snippet_pattern = _re.compile(
        r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>',
        _re.DOTALL,
    )

    links = link_pattern.findall(html_text)
    snippets = snippet_pattern.findall(html_text)

    for i, (url, title) in enumerate(links[:max_results]):
        title_clean = _re.sub(r"<[^>]+>", "", title).strip()
        snippet_clean = _re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        actual_url = url
        ud_match = _re.search(r"uddg=([^&]+)", url)
        if ud_match:
            actual_url = urllib.parse.unquote(ud_match.group(1))
        results.append(f"{i + 1}. {title_clean}\n   {actual_url}\n   {snippet_clean}")

    return results


def _ddg_html_search(query: str, max_results: int) -> list[str]:
    """Fallback: search via DuckDuckGo HTML version."""
    import re as _re
    import urllib.parse

    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"

    try:
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            resp.raise_for_status()
    except httpx.RequestError:
        return []

    html_text = resp.text
    results = []

    blocks = _re.findall(
        r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html_text, _re.DOTALL,
    )

    for i, (link, title, snippet) in enumerate(blocks[:max_results], 1):
        title_clean = _re.sub(r"<[^>]+>", "", title).strip()
        snippet_clean = _re.sub(r"<[^>]+>", "", snippet).strip()
        actual_url = link
        ud_match = _re.search(r"uddg=([^&]+)", link)
        if ud_match:
            actual_url = urllib.parse.unquote(ud_match.group(1))
        results.append(f"{i}. {title_clean}\n   {actual_url}\n   {snippet_clean}")

    return results


def web_fetch(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return content as text.

    Uses Jina Reader (r.jina.ai) for clean content extraction from HTML pages.
    Falls back to direct fetch with local HTML-to-text conversion if Jina is
    unavailable.

    Args:
        url: The URL to fetch.
        max_chars: Max characters to return (default 8000).
    """
    if not url.endswith((".json", ".txt", ".xml", ".csv", ".pdf")):
        jina_text = _fetch_via_jina(url, max_chars)
        if jina_text:
            return _truncate_output(jina_text, source_tool="web_fetch")

    return _fetch_direct(url, max_chars)


def _fetch_via_jina(url: str, max_chars: int) -> str | None:
    """Fetch URL content via Jina Reader for clean markdown output."""
    jina_url = f"https://r.jina.ai/{url}"
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            resp = client.get(jina_url, headers={
                "Accept": "text/plain",
                "User-Agent": "Mozilla/5.0 (compatible; aru-agent/0.1)",
            })
            if resp.status_code != 200:
                return None
            text = resp.text.strip()
            if not text or len(text) < 50:
                return None
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"
            return text
    except (httpx.RequestError, httpx.HTTPStatusError):
        return None


def _fetch_direct(url: str, max_chars: int) -> str:
    """Direct URL fetch with local HTML-to-text conversion."""
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; aru-agent/0.1)",
                "Accept": "text/html,application/json,text/plain,*/*",
            })
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return f"HTTP error {e.response.status_code}: {e.response.reason_phrase}"
    except httpx.RequestError as e:
        return f"Request error: {e}"

    content_type = resp.headers.get("content-type", "")
    body = resp.text

    if "json" in content_type:
        text = body
    elif "html" in content_type:
        text = _html_to_text(body)
    else:
        text = body

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"
    return _truncate_output(text, source_tool="web_fetch")
