"""
Web utilities for fetching and parsing HTML content.

Provides HTMLStripper (strips HTML tags) and fetch_page_text() helper
used by races(action='research'), research_injury, research_exercise, and
research_sport.
"""
from html.parser import HTMLParser
from io import StringIO

import requests

from .config import HTTP_TIMEOUT_SECONDS, PAGE_TEXT_MAX_CHARS

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

SKIP_TAGS = frozenset(('script', 'style', 'nav', 'footer', 'header', 'aside'))


class HTMLStripper(HTMLParser):
    """Strip HTML tags and extract plain text, skipping script/style/nav elements."""

    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = StringIO()
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip = True

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            self.text.write(data + ' ')

    def get_text(self):
        return self.text.getvalue()


def strip_html(html: str) -> str:
    """Strip HTML tags and return plain text."""
    stripper = HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


def fetch_page_text(url: str, max_chars: int = PAGE_TEXT_MAX_CHARS) -> str:
    """Fetch a URL and return stripped plain text, truncated to max_chars.

    Raises requests.RequestException on network errors.
    """
    response = requests.get(
        url,
        headers=DEFAULT_HEADERS,
        timeout=HTTP_TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    response.raise_for_status()
    return strip_html(response.text)[:max_chars]


def fetch_page_text_validated(url: str, max_chars: int = PAGE_TEXT_MAX_CHARS) -> tuple[str, str]:
    """Fetch URL, return (stripped_text, final_url) for redirect detection.

    Same as fetch_page_text but also returns the final URL after redirects,
    allowing callers to detect when the server redirected to a different page.

    Raises requests.RequestException on network errors.
    """
    response = requests.get(
        url,
        headers=DEFAULT_HEADERS,
        timeout=HTTP_TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    response.raise_for_status()
    return strip_html(response.text)[:max_chars], response.url
