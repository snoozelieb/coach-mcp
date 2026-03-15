"""Tests for web_utils.py - fetch_page_text."""

import pytest
import requests
from unittest.mock import patch, Mock

from web_utils import fetch_page_text


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
