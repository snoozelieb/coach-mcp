"""Research tools: model-supplied names are URL-quoted into the fixed hosts'
paths, web sentences are sanitised before they are returned or cached, and
race research comes back in the same untrusted envelope as the rest."""
import json

import pytest

import coach.tools.injury_tools as injury_mod
import coach.tools.race_tools as race_mod
import coach.tools.research_tools as research_mod
import coach.web_utils as web

FETCHED_AT = '2026-01-01T00:00:00+00:00'


def _page(url, text, final_url=None):
    return {'source_url': url, 'final_url': final_url or url, 'fetched_at': FETCHED_AT,
            'retrieved_text': text, 'truncated': False}


EXERCISE_TEXT = (
    'Keep your chest up and brace the core. ' * 3
    + 'Position the feet shoulder width apart with a neutral grip. '
    + 'Lower the weight slowly and drive through the heels. ' * 2
    + 'Avoid rounding the back\x00 which is a common mistake. The target muscle is the quadriceps. ' * 2
)


def _path_of(url: str) -> str:
    return url.split('://', 1)[1].split('/', 1)[1]


class TestExerciseResearch:
    @pytest.fixture
    def fetched(self, sandbox_data_dir, monkeypatch):
        urls = []

        def fake_fetch(url, *args, **kwargs):
            urls.append(url)
            return _page(url, EXERCISE_TEXT)

        monkeypatch.setattr(research_mod, 'fetch_untrusted_page', fake_fetch)
        return urls

    def test_the_name_is_quoted_into_the_fixed_host_path(self, fetched):
        research_mod.research_exercise('split squat/../..?x=1#f', save_to_library=False)
        first = fetched[0]
        assert first.startswith('https://www.muscleandstrength.com/exercises/') and first.endswith('.html')
        assert '/../' not in first and '?' not in _path_of(first) and '#' not in first

    def test_sentences_are_sanitised_before_they_are_returned_or_cached(self, fetched, sandbox_data_dir):
        out = json.loads(research_mod.research_exercise('goblet squat'))
        assert out['cached'] is True
        assert out['untrusted_content']['notice'] == web.UNTRUSTED_NOTICE
        returned = [cue for values in out['form_cues'].values() for cue in values]
        assert returned and all('\x00' not in cue and len(cue) <= 200 for cue in returned)

        library = json.loads((sandbox_data_dir / 'exercise_library.json').read_text(encoding='utf-8'))
        entry = library['goblet squat']
        cached = [cue for values in entry['form_cues'].values() for cue in values]
        assert cached and all('\x00' not in cue and len(cue) <= 200 for cue in cached)
        assert '\x00' not in entry['garmin_note'] and len(entry['garmin_note']) <= 250


class TestSportResearch:
    def test_the_sport_name_is_quoted_into_the_wikipedia_path(self, monkeypatch):
        urls = []

        def fake_fetch(url, *args, **kwargs):
            urls.append(url)
            raise RuntimeError('no page')

        monkeypatch.setattr(research_mod, 'fetch_untrusted_page', fake_fetch)
        research_mod.research_sport('padel/../admin?x=1')
        assert urls and all(u.startswith('https://en.wikipedia.org/wiki/') for u in urls)
        assert all('/../' not in u and '?' not in _path_of(u) for u in urls)


class TestInjuryResearch:
    def test_slugs_are_quoted_into_the_fixed_hosts(self, monkeypatch, sandbox_data_dir):
        urls = []

        def fake_fetch(url, *args, **kwargs):
            urls.append(url)
            raise RuntimeError('no page')

        monkeypatch.setattr(injury_mod, 'fetch_untrusted_page', fake_fetch)
        injury_mod.research_injury("runner's knee/../x?y=1")
        assert urls
        assert all('/../' not in u and '?' not in _path_of(u) for u in urls)


class TestRaceResearch:
    def test_the_envelope_carries_the_fetch_time_and_final_url(self, monkeypatch):
        monkeypatch.setattr(
            race_mod, 'fetch_untrusted_page',
            lambda url, *a, **k: _page(url, 'A 120 km gravel race with 2000 m of climbing.', url + '/info'))
        result = race_mod._research_race(url='https://example.com/race')
        envelope = result['untrusted_content']
        assert envelope['notice'] == web.UNTRUSTED_NOTICE
        assert envelope['fetched_at'] == FETCHED_AT
        assert envelope['final_url'] == 'https://example.com/race/info'
        assert result['detected_info']['distance_km'] == 120
