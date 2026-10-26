"""web_utils: the SSRF-safe fetcher and the untrusted-content shape.

No network: DNS resolution and requests.get are monkeypatched.
"""
import socket

import pytest
import requests

import coach.web_utils as web

PUBLIC_IP = '93.184.216.34'


class FakeResponse:
    def __init__(self, status=200, body=b'<html><body><h1>Race Info</h1><p>Details here</p></body></html>',
                 headers=None, encoding='utf-8'):
        self.status_code = status
        self.headers = headers if headers is not None else {'Content-Type': 'text/html; charset=utf-8'}
        self._body = body
        self.encoding = encoding
        self.raw = type('Raw', (), {'_connection': None})()
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error')

    def close(self):
        self.closed = True


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(web.socket, 'getaddrinfo',
                        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC_IP, 443))])


def _get_returning(monkeypatch, responses):
    """requests.get stub that returns the given responses in order and records calls."""
    calls = []
    queue = list(responses)

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(web.requests, 'get', fake_get)
    return calls


# ---------------------------------------------------------------------------
# URL policy
# ---------------------------------------------------------------------------

class TestUrlPolicy:
    def test_http_is_refused(self, public_dns):
        with pytest.raises(web.UnsafeURLError, match='https'):
            web.validate_url('http://example.com/race')

    @pytest.mark.parametrize('host', [
        '127.0.0.1', '10.0.0.5', '192.168.1.10', '172.16.0.1', '169.254.169.254',
        '0.0.0.0', '100.64.0.1', '[::1]', '[::ffff:127.0.0.1]', '[fe80::1]', '[fc00::1]',
    ])
    def test_non_public_literals_are_refused(self, host):
        with pytest.raises(web.UnsafeURLError):
            web.validate_url(f'https://{host}/admin')

    def test_host_resolving_to_a_private_address_is_refused(self, monkeypatch):
        monkeypatch.setattr(web.socket, 'getaddrinfo',
                            lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.0.9', 443))])
        with pytest.raises(web.UnsafeURLError, match='non-public'):
            web.validate_url('https://intranet.example/')

    def test_credentials_in_url_are_refused(self, public_dns):
        with pytest.raises(web.UnsafeURLError, match='credentials'):
            web.validate_url('https://user:pw@example.com/')

    def test_public_url_passes(self, public_dns):
        assert web.validate_url('https://example.com/race') == 'https://example.com/race'

    def test_ip_is_public_unwraps_mapped_addresses(self):
        assert web.ip_is_public('93.184.216.34')
        assert not web.ip_is_public('::ffff:10.0.0.1')
        assert not web.ip_is_public('2002:7f00:0001::1')  # 6to4 of 127.0.0.1


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

class TestFetch:
    def test_returns_the_untrusted_shape_with_stripped_text(self, public_dns, monkeypatch):
        calls = _get_returning(monkeypatch, [FakeResponse()])

        page = web.fetch_untrusted_page('https://example.com/race')

        assert set(page) == {'source_url', 'final_url', 'fetched_at', 'retrieved_text', 'truncated'}
        assert 'Race Info' in page['retrieved_text'] and '<h1>' not in page['retrieved_text']
        assert page['final_url'] == 'https://example.com/race'
        assert page['truncated'] is False
        url, kwargs = calls[0]
        assert kwargs['allow_redirects'] is False and kwargs['stream'] is True
        assert kwargs['timeout'] == 15 and 'User-Agent' in kwargs['headers']

    def test_fetch_page_text_truncates_to_max_chars(self, public_dns, monkeypatch):
        _get_returning(monkeypatch, [FakeResponse(body=b'<p>' + b'A' * 500 + b'</p>')])
        assert len(web.fetch_page_text('https://example.com', max_chars=100)) <= 100

    def test_default_cap_is_the_config_value(self, public_dns, monkeypatch):
        _get_returning(monkeypatch, [FakeResponse(body=b'<p>' + b'X' * 20000 + b'</p>')])
        assert len(web.fetch_page_text('https://example.com')) <= 8000

    def test_skip_tags_are_dropped(self, public_dns, monkeypatch):
        _get_returning(monkeypatch, [FakeResponse(
            body=b'<html><nav>Menu</nav><main>Content</main><footer>Foot</footer></html>')])
        text = web.fetch_page_text('https://example.com')
        assert 'Content' in text and 'Menu' not in text and 'Foot' not in text

    def test_network_and_http_errors_propagate(self, public_dns, monkeypatch):
        def boom(url, **kwargs):
            raise requests.RequestException('Connection refused')
        monkeypatch.setattr(web.requests, 'get', boom)
        with pytest.raises(requests.RequestException, match='Connection refused'):
            web.fetch_page_text('https://down.example.com')

        _get_returning(monkeypatch, [FakeResponse(status=404)])
        with pytest.raises(requests.HTTPError):
            web.fetch_page_text('https://example.com/missing')

    def test_redirect_to_a_private_address_is_refused_before_following(self, public_dns, monkeypatch):
        calls = _get_returning(monkeypatch, [
            FakeResponse(status=302, headers={'Location': 'https://127.0.0.1/admin'}),
            FakeResponse(),
        ])
        with pytest.raises(web.UnsafeURLError):
            web.fetch_untrusted_page('https://example.com/go')
        assert len(calls) == 1  # the private hop was never requested

    def test_redirect_to_http_is_refused(self, public_dns, monkeypatch):
        _get_returning(monkeypatch, [FakeResponse(status=301, headers={'Location': 'http://example.com/x'})])
        with pytest.raises(web.UnsafeURLError, match='https'):
            web.fetch_untrusted_page('https://example.com/go')

    def test_relative_redirect_is_followed_and_reported(self, public_dns, monkeypatch):
        calls = _get_returning(monkeypatch, [
            FakeResponse(status=302, headers={'Location': '/final'}),
            FakeResponse(),
        ])
        page = web.fetch_untrusted_page('https://example.com/start')
        assert page['final_url'] == 'https://example.com/final'
        assert [c[0] for c in calls] == ['https://example.com/start', 'https://example.com/final']

    def test_redirect_loops_stop(self, public_dns, monkeypatch):
        _get_returning(monkeypatch, [FakeResponse(status=302, headers={'Location': 'https://example.com/again'})])
        with pytest.raises(web.UnsafeURLError, match='redirects'):
            web.fetch_untrusted_page('https://example.com/loop')

    def test_non_text_content_is_refused(self, public_dns, monkeypatch):
        _get_returning(monkeypatch, [FakeResponse(headers={'Content-Type': 'application/pdf'})])
        with pytest.raises(web.UnsafeURLError, match='content type'):
            web.fetch_untrusted_page('https://example.com/file.pdf')

    def test_oversized_bodies_are_capped(self, public_dns, monkeypatch):
        huge = b'<p>' + b'Z' * (web.MAX_RESPONSE_BYTES + 100_000) + b'</p>'
        _get_returning(monkeypatch, [FakeResponse(body=huge)])
        page = web.fetch_untrusted_page('https://example.com/huge', max_chars=50)
        assert page['truncated'] is True
        assert len(page['retrieved_text']) == 50

    def test_control_characters_are_stripped(self, public_dns, monkeypatch):
        _get_returning(monkeypatch, [FakeResponse(body=b'<p>ok\x00\x07 fine</p>')])
        assert '\x00' not in web.fetch_page_text('https://example.com')


def test_sanitize_text_cleans_and_caps():
    assert web.sanitize_text('  keep\x00 the\n\tform   tight  ', 12) == 'keep the for'
    assert web.sanitize_text(None, 10) == ''


def test_untrusted_envelope_labels_the_text():
    env = web.untrusted_envelope({'source_url': 'https://a', 'final_url': 'https://b',
                                  'fetched_at': 't', 'retrieved_text': 'x' * 2000})
    assert env['notice'] == web.UNTRUSTED_NOTICE
    assert len(env['retrieved_text']) == 1500
