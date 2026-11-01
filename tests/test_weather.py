"""Weather for planning (coach/weather.py, coach/tools/weather_tools.py).

Facts from Open-Meteo at the profile location, cached for an hour; the
limit from methodology.weather; the training windows from the profile.
Nothing here touches the network: fetch_forecast and geocode_place are
patched on the module.
"""
import json
from datetime import date, datetime, timedelta

import pytest

import coach.fitness as fitness_mod
import coach.parsers as parsers_mod
import coach.planner as planner
import coach.rules as rules
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod
import coach.tools.strength_tools as strength_mod
import coach.tools.weather_tools as weather_tools
import coach.weather as weather
import coach.workout_builder as wb

TODAY = date.today()
NOW = datetime(TODAY.year, TODAY.month, TODAY.day, 9, 0)
D0, D1, D2 = TODAY.isoformat(), (TODAY + timedelta(days=1)).isoformat(), (TODAY + timedelta(days=2)).isoformat()
LOCATION = {'latitude': -33.9, 'longitude': 18.4, 'label': 'Test town'}
WINDOWS = ('05:00-07:00', '18:30-21:30')
CALM = [20.0] * 24
WINDY = [20.0] * 10 + [70.0] * 7 + [20.0] * 7   # gusts 10:00-16:00


@pytest.fixture
def env(data_dir, monkeypatch):
    for mod in (weather, weather_tools, planner, rules, fitness_mod, parsers_mod, wb,
                coaching_mod, planning_mod, strength_mod):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    return data_dir


def _profile(data_dir, location=LOCATION, windows=WINDOWS):
    personal = {'name': 'T', 'resting_hr': 42, 'max_hr': 195}
    if location:
        personal['location'] = dict(location)
    (data_dir / 'athlete.json').write_text(json.dumps({
        'personal': personal, 'life_constraints': {'preferred_training_times': list(windows)},
        'injury_history': [], 'preferences': {}}), encoding='utf-8')


def _payload(start=TODAY, days=5, gusts=None):
    """A compact Open-Meteo payload: `gusts` maps an ISO date to 24 hourly gusts (default calm)."""
    hourly = {k: [] for k in ('time', 'wind_gusts_10m', 'wind_speed_10m', 'wind_direction_10m',
                             'precipitation', 'precipitation_probability', 'temperature_2m', 'weather_code')}
    daily = {k: [] for k in ('time', 'sunrise', 'sunset', 'daylight_duration', 'wind_speed_10m_max',
                            'wind_gusts_10m_max', 'precipitation_sum', 'precipitation_probability_max',
                            'temperature_2m_max', 'temperature_2m_min', 'weather_code')}
    for i in range(days):
        ds = (start + timedelta(days=i)).isoformat()
        day_gusts = (gusts or {}).get(ds) or CALM
        for hour, gust in enumerate(day_gusts):
            hourly['time'].append(f'{ds}T{hour:02d}:00')
            hourly['wind_gusts_10m'].append(gust)
            hourly['wind_speed_10m'].append(round(gust * 0.6, 1))
            hourly['wind_direction_10m'].append(135)
            hourly['precipitation'].append(0.0)
            hourly['precipitation_probability'].append(10)
            hourly['temperature_2m'].append(18.0)
            hourly['weather_code'].append(1)
        daily['time'].append(ds)
        daily['sunrise'].append(f'{ds}T06:30')
        daily['sunset'].append(f'{ds}T18:30')
        daily['daylight_duration'].append(12 * 3600)
        daily['wind_speed_10m_max'].append(round(max(day_gusts) * 0.6, 1))
        daily['wind_gusts_10m_max'].append(max(day_gusts))
        daily['precipitation_sum'].append(0.0)
        daily['precipitation_probability_max'].append(10)
        daily['temperature_2m_max'].append(22.0)
        daily['temperature_2m_min'].append(12.0)
        daily['weather_code'].append(1)
    return {'timezone': 'Africa/Johannesburg', 'hourly': hourly, 'daily': daily}


def _fake_fetch(monkeypatch, payload=None, fail=False):
    calls = []

    def fake(latitude, longitude, days=16, timeout=8):
        calls.append((latitude, longitude, days))
        if fail:
            raise ConnectionError('offline')
        return payload if payload is not None else _payload()

    monkeypatch.setattr(weather, 'fetch_forecast', fake)
    return calls


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------

class TestParseForecast:
    def test_day_facts_calm_block_and_windows(self):
        parsed = weather.parse_forecast(_payload(days=2, gusts={D1: WINDY}), TODAY,
                                        [('05:00', '07:00'), ('18:30', '21:30')],
                                        {'outdoor_ride_gust_limit_kmh': 50})
        calm, gale = parsed['days']
        assert (calm['date'], gale['date']) == (D0, D1)
        assert calm['gust_over_ride_limit'] is False
        assert calm['calm_daylight_block'] == {'start': '07:00', 'end': '19:00', 'hours': 12}
        assert gale['gust_over_ride_limit'] is True
        assert gale['gust_max_kmh'] == 70 and gale['gust_max_hour'] == '10:00' and gale['wind_direction'] == 'SE'
        assert gale['daylight_gust_max_kmh'] == 70
        assert gale['calm_daylight_block'] == {'start': '07:00', 'end': '10:00', 'hours': 3}
        assert gale['sunrise'] == '06:30' and gale['sunset'] == '18:30' and gale['daylight_hours'] == 12.0
        dawn, evening = gale['windows']
        assert dawn['window'] == '05:00-07:00' and dawn['light'] == 'partly' and dawn['daylight_minutes'] == 30
        assert dawn['gust_max_kmh'] == 20   # the gusts start at 10:00
        assert evening['light'] == 'dark' and evening['gust_max_kmh'] == 20
        assert parsed['calmest_day'] == D0 and parsed['windiest_day'] == D1
        assert parsed['limits'] == {'outdoor_ride_gust_limit_kmh': 50}

    def test_night_gusts_do_not_label_the_day(self):
        night = [80.0] * 6 + [20.0] * 13 + [80.0] * 5   # gale before dawn and after dusk (sunset 18:30)
        (day,) = weather.parse_forecast(_payload(days=1, gusts={D0: night}), TODAY, [],
                                        {'outdoor_ride_gust_limit_kmh': 50})['days']
        assert day['gust_max_kmh'] == 80 and day['daylight_gust_max_kmh'] == 20
        assert day['gust_over_ride_limit'] is False

    def test_past_days_are_dropped(self):
        parsed = weather.parse_forecast(_payload(start=TODAY - timedelta(days=1), days=3), TODAY, [], {})
        assert [d['date'] for d in parsed['days']] == [D0, D1]

    def test_limit_from_rules_labels_the_days(self):
        (day,) = weather.parse_forecast(_payload(days=1), TODAY, [], {'outdoor_ride_gust_limit_kmh': 15})['days']
        assert day['gust_over_ride_limit'] is True and day['calm_daylight_block'] is None


# ---------------------------------------------------------------------------
# Profile and methodology inputs
# ---------------------------------------------------------------------------

class TestInputs:
    def test_location_and_windows_from_the_profile(self, env):
        _profile(env)
        assert weather.profile_location()['label'] == 'Test town'
        assert weather.training_windows() == [('05:00', '07:00'), ('18:30', '21:30')]

    def test_bad_location_and_windows_are_ignored(self, env):
        _profile(env, location={'latitude': 'x', 'longitude': 18.4}, windows=('dawn', '5-7', '06:00-07:30'))
        assert weather.profile_location() is None
        assert weather.training_windows() == [('06:00', '07:30')]

    def test_methodology_limit_overrides_the_default(self, env):
        (env / 'methodology.json').write_text(json.dumps({'weather': {'outdoor_ride_gust_limit_kmh': 40}}), encoding='utf-8')
        assert weather.weather_rules()['outdoor_ride_gust_limit_kmh'] == 40
        (env / 'methodology.json').write_text(json.dumps({'weather': {'outdoor_ride_gust_limit_kmh': 'high'}}), encoding='utf-8')
        assert weather.weather_rules()['outdoor_ride_gust_limit_kmh'] == 50


# ---------------------------------------------------------------------------
# The outlook: cache, fetch, failure
# ---------------------------------------------------------------------------

class TestOutlook:
    def test_no_location_names_the_fix_and_never_fetches(self, env, monkeypatch):
        _profile(env, location=None)
        calls = _fake_fetch(monkeypatch)
        result = weather.outlook(TODAY, NOW)
        assert result['status'] == 'no_location' and 'set_location' in result['hint']
        assert calls == []

    def test_fetch_once_then_the_cache_for_an_hour(self, env, monkeypatch):
        _profile(env)
        calls = _fake_fetch(monkeypatch)
        first = weather.outlook(TODAY, NOW, days=3)
        assert first['status'] == 'ok' and first['stale'] is False
        assert len(first['days']) == 3 and first['source'].startswith('Weather data by Open-Meteo')
        assert calls == [(-33.9, 18.4, 16)]
        assert (env / 'weather_forecast.json').exists()
        second = weather.outlook(TODAY, NOW + timedelta(minutes=30), days=3)
        assert second['status'] == 'ok' and len(calls) == 1
        assert second['fetched_at'] == first['fetched_at']

    def test_cache_expires_after_the_ttl(self, env, monkeypatch):
        _profile(env)
        calls = _fake_fetch(monkeypatch)
        weather.outlook(TODAY, NOW)
        weather.outlook(TODAY, NOW + timedelta(hours=2))
        assert len(calls) == 2

    def test_force_refresh_and_a_new_location_refetch(self, env, monkeypatch):
        _profile(env)
        calls = _fake_fetch(monkeypatch)
        weather.outlook(TODAY, NOW)
        weather.outlook(TODAY, NOW, force_refresh=True)
        assert len(calls) == 2
        _profile(env, location={'latitude': -26.2, 'longitude': 28.0})
        weather.outlook(TODAY, NOW)
        assert calls[-1][:2] == (-26.2, 28.0)

    def test_fetch_failure_falls_back_to_the_stale_cache(self, env, monkeypatch):
        _profile(env)
        _fake_fetch(monkeypatch)
        weather.outlook(TODAY, NOW)
        _fake_fetch(monkeypatch, fail=True)
        result = weather.outlook(TODAY, NOW + timedelta(hours=3))
        assert result['status'] == 'ok' and result['stale'] is True
        assert 'offline' in result['fetch_error']

    def test_fetch_failure_without_a_cache_is_unavailable(self, env, monkeypatch):
        _profile(env)
        _fake_fetch(monkeypatch, fail=True)
        result = weather.outlook(TODAY, NOW)
        assert result['status'] == 'unavailable' and 'offline' in result['error']

    def test_snapshot_block_is_compact_and_never_raises(self, env, monkeypatch):
        _profile(env)
        _fake_fetch(monkeypatch, payload=_payload(days=10, gusts={D1: WINDY}))
        block = weather.snapshot_block(TODAY, NOW)
        assert block['status'] == 'ok' and block['location'] == 'Test town'
        assert len(block['days']) == 7 and 'windows' not in block['days'][0]
        assert block['days'][1]['gust_over_ride_limit'] is True
        assert block['calmest_day'] == D0 and block['limits']['outdoor_ride_gust_limit_kmh'] == 50

        def boom(*args, **kwargs):
            raise RuntimeError('parser bug')

        monkeypatch.setattr(weather, 'outlook', boom)
        assert weather.snapshot_block(TODAY, NOW)['status'] == 'unavailable'


# ---------------------------------------------------------------------------
# Grounding: outdoor rides against the forecast
# ---------------------------------------------------------------------------

class TestGrounding:
    def test_only_outdoor_rides_on_windy_days_warn(self, env, monkeypatch):
        _profile(env)
        _fake_fetch(monkeypatch, payload=_payload(gusts={D1: WINDY}))
        plan_days = {
            D1: {'planned': [
                {'type': 'cycling', 'duration_mins': 240, 'name': 'Long ride', 'purpose': 'x'},
                {'type': 'running', 'duration_mins': 40, 'purpose': 'x'},       # runs: no wind limit
                {'type': 'wattbike', 'duration_mins': 60, 'purpose': 'x'},      # indoors: no wind
            ]},
            D0: {'planned': {'type': 'cycling', 'duration_mins': 90, 'purpose': 'x'}},  # calm day
        }
        warnings = weather.grounding_warnings(plan_days, TODAY, NOW)
        assert [(w['type'], w['date'], w['session']) for w in warnings] == [('wind_over_ride_limit', D1, 'Long ride')]
        warning = warnings[0]
        assert warning['gust_max_kmh'] == 70 and warning['limit_kmh'] == 50
        assert warning['calmest_day'] == D0 and warning['calm_daylight_block']['hours'] == 3
        assert '07:00-10:00' in warning['message'] and D0 in warning['message']

    def test_no_location_or_failure_means_no_warning(self, env, monkeypatch):
        _profile(env, location=None)
        _fake_fetch(monkeypatch, payload=_payload(gusts={D1: WINDY}))
        plan_days = {D1: {'planned': {'type': 'cycling', 'duration_mins': 240, 'purpose': 'x'}}}
        assert weather.grounding_warnings(plan_days, TODAY, NOW) == []
        _profile(env)
        _fake_fetch(monkeypatch, fail=True)
        assert weather.grounding_warnings(plan_days, TODAY, NOW) == []

    def test_plan_save_carries_the_wind_warning(self, env, monkeypatch):
        _profile(env)
        _fake_fetch(monkeypatch, payload=_payload(gusts={D1: WINDY}))
        result = planning_mod.update_weekly_plan(plan={
            'days': {D1: {'planned': {'type': 'cycling', 'duration_mins': 120, 'intensity': 'easy',
                                      'purpose': 'endurance volume'}}},
            'rationale': 'wind test'})
        assert result['status'] == 'success', result
        assert [w['type'] for w in result['grounding_warnings'] if w['type'] == 'wind_over_ride_limit'] == ['wind_over_ride_limit']


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

class TestTools:
    def test_outlook_tool(self, env, monkeypatch):
        _profile(env)
        _fake_fetch(monkeypatch)
        result = weather_tools.get_weather_outlook(days=2)
        assert result['status'] == 'ok' and len(result['days']) == 2

    def test_set_location_with_coordinates_keeps_the_rest_of_the_profile(self, env):
        _profile(env, location=None)
        result = weather_tools.set_location(latitude=-33.9, longitude=18.4, label='Home')
        assert result['status'] == 'success'
        athlete = json.loads((env / 'athlete.json').read_text(encoding='utf-8'))
        assert athlete['personal']['location'] == {'latitude': -33.9, 'longitude': 18.4, 'label': 'Home'}
        assert athlete['personal']['resting_hr'] == 42
        assert weather.profile_location()['label'] == 'Home'

    def test_set_location_by_place_stores_the_best_match(self, env, monkeypatch):
        _profile(env, location=None)
        monkeypatch.setattr(weather, 'geocode_place', lambda place, timeout=8: [
            {'name': 'Testville', 'admin1': 'Western Cape', 'country': 'South Africa',
             'latitude': -33.93, 'longitude': 18.86, 'timezone': 'Africa/Johannesburg'},
            {'name': 'Testville', 'admin1': 'Texas', 'country': 'United States',
             'latitude': 31.0, 'longitude': -97.0, 'timezone': 'America/Chicago'}])
        result = weather_tools.set_location(place='Testville')
        assert result['status'] == 'success'
        assert result['location'] == {'latitude': -33.93, 'longitude': 18.86,
                                      'label': 'Testville, Western Cape, South Africa',
                                      'timezone': 'Africa/Johannesburg'}
        assert len(result['other_matches']) == 1

    def test_set_location_errors(self, env, monkeypatch):
        _profile(env, location=None)
        assert 'error' in weather_tools.set_location()
        assert 'error' in weather_tools.set_location(latitude=100, longitude=18)
        monkeypatch.setattr(weather, 'geocode_place', lambda place, timeout=8: [])
        assert 'no place matched' in weather_tools.set_location(place='Nowhereville')['error']
