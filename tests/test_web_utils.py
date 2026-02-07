"""Tests for web_utils.py - HTMLStripper, strip_html, fetch_page_text."""

import pytest
import requests
from unittest.mock import patch, Mock

from web_utils import HTMLStripper, strip_html, fetch_page_text, SKIP_TAGS


# ---------------------------------------------------------------------------
# HTMLStripper class
# ---------------------------------------------------------------------------

class TestHTMLStripper:
    """Tests for the HTMLStripper parser."""

    def test_strips_basic_html_tags(self):
        stripper = HTMLStripper()
        stripper.feed("<p>Hello <b>world</b></p>")
        text = stripper.get_text()
        assert "Hello" in text
        assert "world" in text
        assert "<p>" not in text
        assert "<b>" not in text

    def test_skips_script_content(self):
        stripper = HTMLStripper()
        stripper.feed("<div>Visible</div><script>var x = 1;</script><p>Also visible</p>")
        text = stripper.get_text()
        assert "Visible" in text
        assert "Also visible" in text
        assert "var x" not in text

    def test_skips_style_content(self):
        stripper = HTMLStripper()
        stripper.feed("<style>.foo { color: red; }</style><p>Content</p>")
        text = stripper.get_text()
        assert "Content" in text
        assert "color" not in text

    def test_skips_nav_content(self):
        stripper = HTMLStripper()
        stripper.feed("<nav>Menu Item 1</nav><main>Main Content</main>")
        text = stripper.get_text()
        assert "Main Content" in text
        assert "Menu Item" not in text

    def test_skips_footer_content(self):
        stripper = HTMLStripper()
        stripper.feed("<p>Body</p><footer>Copyright 2025</footer>")
        text = stripper.get_text()
        assert "Body" in text
        assert "Copyright" not in text

    def test_skips_header_content(self):
        stripper = HTMLStripper()
        stripper.feed("<header>Site Logo</header><article>Article text</article>")
        text = stripper.get_text()
        assert "Article text" in text
        assert "Site Logo" not in text

    def test_skips_aside_content(self):
        stripper = HTMLStripper()
        stripper.feed("<aside>Sidebar widget</aside><div>Main area</div>")
        text = stripper.get_text()
        assert "Main area" in text
        assert "Sidebar widget" not in text

    def test_all_skip_tags_present(self):
        """Verify the SKIP_TAGS constant matches expectations."""
        expected = {'script', 'style', 'nav', 'footer', 'header', 'aside'}
        assert SKIP_TAGS == expected

    def test_handles_nested_tags(self):
        stripper = HTMLStripper()
        stripper.feed("<div><p><span>Deep <em>nested</em> text</span></p></div>")
        text = stripper.get_text()
        assert "Deep" in text
        assert "nested" in text
        assert "text" in text
        assert "<" not in text

    def test_handles_empty_input(self):
        stripper = HTMLStripper()
        stripper.feed("")
        text = stripper.get_text()
        assert text == ""

    def test_handles_no_tags(self):
        stripper = HTMLStripper()
        stripper.feed("Just plain text with no tags")
        text = stripper.get_text()
        assert "Just plain text with no tags" in text

    def test_text_after_skip_tag_is_visible(self):
        """Content after a closed skip tag should be included."""
        stripper = HTMLStripper()
        stripper.feed("<script>hidden</script><p>visible</p>")
        text = stripper.get_text()
        assert "visible" in text
        assert "hidden" not in text

    def test_multiple_skip_tags(self):
        stripper = HTMLStripper()
        stripper.feed(
            "<nav>nav stuff</nav>"
            "<p>keep this</p>"
            "<footer>foot stuff</footer>"
            "<p>and this</p>"
            "<style>css</style>"
        )
        text = stripper.get_text()
        assert "keep this" in text
        assert "and this" in text
        assert "nav stuff" not in text
        assert "foot stuff" not in text
        assert "css" not in text


# ---------------------------------------------------------------------------
# strip_html function
# ---------------------------------------------------------------------------

class TestStripHtml:
    """Tests for the strip_html() convenience function."""

    def test_strips_tags_and_returns_text(self):
        result = strip_html("<h1>Title</h1><p>Paragraph</p>")
        assert "Title" in result
        assert "Paragraph" in result
        assert "<h1>" not in result
        assert "<p>" not in result

    def test_skips_script_content(self):
        result = strip_html("<script>alert('xss')</script><div>Safe text</div>")
        assert "Safe text" in result
        assert "alert" not in result

    def test_empty_string(self):
        result = strip_html("")
        assert result == ""

    def test_returns_string(self):
        result = strip_html("<p>test</p>")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# fetch_page_text function
# ---------------------------------------------------------------------------

class TestFetchPageText:
    """Tests for fetch_page_text() with mocked HTTP requests."""

    @patch("web_utils.requests.get")
    def test_returns_stripped_text_from_html_response(self, mock_get):
        mock_response = Mock()
        mock_response.text = "<html><body><h1>Race Info</h1><p>Details here</p></body></html>"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = fetch_page_text("https://example.com/race")

        assert "Race Info" in result
        assert "Details here" in result
        assert "<h1>" not in result
        mock_get.assert_called_once()
        mock_response.raise_for_status.assert_called_once()

    @patch("web_utils.requests.get")
    def test_truncates_to_max_chars(self, mock_get):
        long_text = "A" * 500
        mock_response = Mock()
        mock_response.text = f"<p>{long_text}</p>"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = fetch_page_text("https://example.com", max_chars=100)

        assert len(result) <= 100

    @patch("web_utils.requests.get")
    def test_default_max_chars_uses_config_value(self, mock_get):
        # Build HTML whose stripped text exceeds PAGE_TEXT_MAX_CHARS (8000)
        huge_text = "X" * 20000
        mock_response = Mock()
        mock_response.text = f"<p>{huge_text}</p>"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = fetch_page_text("https://example.com")

        # strip_html adds a trailing space per data segment so length is text + 1
        # but truncation to 8000 should still apply
        assert len(result) <= 8000

    @patch("web_utils.requests.get")
    def test_strips_skip_tags_from_response(self, mock_get):
        mock_response = Mock()
        mock_response.text = (
            "<html><nav>Menu</nav>"
            "<main>Content</main>"
            "<footer>Foot</footer></html>"
        )
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = fetch_page_text("https://example.com")

        assert "Content" in result
        assert "Menu" not in result
        assert "Foot" not in result

    @patch("web_utils.requests.get")
    def test_request_exception_propagates(self, mock_get):
        mock_get.side_effect = requests.RequestException("Connection refused")

        with pytest.raises(requests.RequestException, match="Connection refused"):
            fetch_page_text("https://down.example.com")

    @patch("web_utils.requests.get")
    def test_http_error_propagates(self, mock_get):
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
        mock_get.return_value = mock_response

        with pytest.raises(requests.HTTPError, match="404 Not Found"):
            fetch_page_text("https://example.com/missing")

    @patch("web_utils.requests.get")
    def test_passes_correct_request_params(self, mock_get):
        mock_response = Mock()
        mock_response.text = "<p>ok</p>"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        fetch_page_text("https://example.com/page")

        call_kwargs = mock_get.call_args
        assert call_kwargs[0][0] == "https://example.com/page"
        assert "User-Agent" in call_kwargs[1]["headers"]
        assert call_kwargs[1]["timeout"] == 15
        assert call_kwargs[1]["allow_redirects"] is True
