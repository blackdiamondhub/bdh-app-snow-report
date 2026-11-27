import datetime
import os
from unittest.mock import patch

from django.core.cache import cache
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, tag
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait

from snow_report.forecast import (EFFECTS, WMO, describe, get_current, get_forecast, icon_effect, parse_forecast,
                                  weather_effect)
from snow_report.resort import ICONS as RESORT_ICONS
from snow_report.resort import SNOW_REPORT_URL, parse_resort_html, sanitize_number
from snow_report.views import RESORT_TZ, build_context

FIXTURES = os.path.join(os.path.dirname(__file__), "tests")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return parse_resort_html(f.read())


def open_meteo_payload(days=5):
    def daily(high, low, snow, code):
        times = [(datetime.date(2026, 1, 10) + datetime.timedelta(days=i)).isoformat() for i in range(days)]
        return {"daily": {
            "time": times,
            "weather_code": [code] * days,
            "temperature_2m_max": [high] * days,
            "temperature_2m_min": [low] * days,
            "snowfall_sum": [snow] * days,
            "precipitation_probability_max": [40] * days,
            "uv_index_max": [2.4] * days,
            "sunrise": [f"{t}T07:45" for t in times],
            "sunset": [f"{t}T16:40" for t in times],
        }}
    return [daily(1.0, -6.0, 0.0, 3), daily(-7.0, -12.0, 8.4, 73)]


def fake_now(year, month, day, hour=12):
    return lambda: datetime.datetime(year, month, day, hour, tzinfo=RESORT_TZ)


class SanitizeNumberTests(TestCase):
    def test_double_dash_negative(self):
        self.assertEqual(sanitize_number("--7"), -7.0)

    def test_thousands_separator(self):
        self.assertEqual(sanitize_number("2,080"), 2080.0)

    def test_not_available(self):
        self.assertIsNone(sanitize_number("N/A"))
        self.assertIsNone(sanitize_number(""))
        self.assertIsNone(sanitize_number("-"))


class WinterParseTests(TestCase):
    """weather_bad_data.html is an in-season capture (Feb 27)."""

    def setUp(self):
        self.data = fixture("weather_bad_data.html")

    def test_narrative_and_icon(self):
        self.assertTrue(self.data["narrative"].startswith("Cloudy with sunny breaks"))
        self.assertEqual(self.data["icon"], "fas fa-cloud-sun")
        self.assertEqual(self.data["updated"], "Updated February 27th at 6:43pm")

    def test_new_snow_keeps_missing_values_as_none(self):
        self.assertEqual(
            [(s["period"], s["cm"]) for s in self.data["new_snow"]],
            [("New Snow", 0.0), ("24 Hr", None), ("48 Hr", 0.0), ("7 Days", 24.0)],
        )

    def test_snow_base(self):
        self.assertEqual([(b["name"], b["cm"]) for b in self.data["base"]],
                         [("Mid Mountain", 158.0), ("Alpine", 172.0)])

    def test_temperatures(self):
        self.assertEqual([(t["name"], t["elevation_m"], t["c"]) for t in self.data["temperatures"]], [
            ("Top of the World", 2080.0, -2.0), ("Mid-mountain", 1855.0, 0.0),
            ("Top of Morrisey", 1675.0, 0.0), ("Valley", 1255.0, 3.0),
        ])


class OffSeasonParseTests(TestCase):
    """weather_current.html is an off-season capture: no snow, narrative or forecast."""

    def setUp(self):
        self.data = fixture("weather_current.html")

    def test_no_snow_sections(self):
        self.assertIsNone(self.data["narrative"])
        self.assertEqual(self.data["new_snow"], [])
        self.assertEqual(self.data["base"], [])

    def test_air_quality_kept_out_of_temperatures(self):
        self.assertEqual({t["name"] for t in self.data["temperatures"]},
                         {"Top of the World", "Mid-mountain", "Top of Morrisey", "Valley"})
        self.assertEqual([(a["name"], a["aqhi"]) for a in self.data["air_quality"]], [
            ("Top of Sunburst", 1.0), ("Top of Sundance", 1.0), ("Top of Morrisey", 1.0), ("Valley", 1.0),
        ])

    def test_wind(self):
        self.assertEqual(self.data["wind"][0],
                         {"name": "Top of the World", "elevation_m": 2080.0, "direction": "SSE", "kph": 16.0})
        self.assertEqual(len(self.data["wind"]), 3)


class LiveResortPageTests(TestCase):
    """Catches Sun Peaks changing their page markup (CI runs this daily)."""

    def test_live_page_still_parses(self):
        import requests
        response = requests.get(SNOW_REPORT_URL, timeout=30)
        response.raise_for_status()
        data = parse_resort_html(response.content)
        self.assertGreaterEqual(len(data["temperatures"]), 3, data)
        for t in data["temperatures"]:
            self.assertTrue(1000 <= t["elevation_m"] <= 2500, t)
            self.assertTrue(-50 <= t["c"] <= 45, t)
        if data["new_snow"]:
            self.assertTrue(data["base"], "snow conditions without a snow base")


class ForecastParseTests(TestCase):
    def test_village_and_alpine_combined(self):
        days = parse_forecast(open_meteo_payload())
        self.assertEqual(len(days), 5)
        first = days[0]
        self.assertEqual(first["date"], datetime.date(2026, 1, 10))
        self.assertEqual((first["icon"], first["label"]), ("fas fa-cloud", "Overcast"))
        self.assertEqual((first["high_c"], first["low_c"]), (1.0, -6.0))
        self.assertEqual((first["alpine_high_c"], first["alpine_snow_cm"]), (-7.0, 8.4))
        self.assertEqual((first["sunrise"], first["sunset"]), ("07:45", "16:40"))

    def test_unknown_weather_code(self):
        payload = open_meteo_payload()
        payload[0]["daily"]["weather_code"][0] = 1234
        self.assertEqual(parse_forecast(payload)[0]["icon"], "fas fa-cloud")


class WeatherEffectTests(TestCase):
    def test_codes(self):
        cases = {(0, True): "sun", (1, False): "stars", (2, True): "partly", (2, False): "clouds",
                 (3, True): "clouds", (45, True): "fog", (53, True): "drizzle", (63, True): "rain",
                 (81, True): "rain", (73, True): "snow", (86, True): "snow", (95, True): "storm",
                 (1234, True): "clouds"}
        for (code, is_day), effect in cases.items():
            self.assertEqual(weather_effect(code, is_day), effect, code)

    def test_every_hero_icon_has_a_matching_background(self):
        # Any icon the hero can show (resort report or forecast) must map to a background of the same weather.
        for icon in set(RESORT_ICONS.values()):
            self.assertIn(icon_effect(icon), EFFECTS, icon)
        for code in WMO:
            for is_day in (True, False):
                expected = weather_effect(code, is_day)
                expected = "rain" if expected == "drizzle" else expected  # same icon for both
                self.assertEqual(icon_effect(describe(code, is_day)[0], is_day), expected, (code, is_day))

    @patch("snow_report.forecast.requests.get")
    def test_fetch_caches_current_conditions(self, get):
        cache.clear()
        payload = open_meteo_payload()
        payload[0]["current"] = {"time": "2026-01-10T20:00", "weather_code": 71, "is_day": 0}
        get.return_value.json.return_value = payload
        get_forecast()
        self.assertEqual(get_current(), {"code": 71, "is_day": False})
        self.assertIn("weather_code,is_day", get.call_args.kwargs["params"]["current"])


class BuildContextTests(TestCase):
    def test_winter_when_snow_data_present(self):
        ctx = build_context(fixture("weather_bad_data.html"), parse_forecast(open_meteo_payload()), "metric")
        self.assertEqual(ctx["season"], "winter")
        self.assertEqual(ctx["hero_icon"], "fas fa-cloud-sun")
        self.assertEqual([b["value"] for b in ctx["base"]], [158, 172])

    @patch("snow_report.views.local_now", fake_now(2026, 7, 15))
    def test_summer_without_snow_data(self):
        ctx = build_context(fixture("weather_current.html"), parse_forecast(open_meteo_payload()), "metric")
        self.assertEqual(ctx["season"], "summer")
        self.assertEqual(ctx["hero_text"], "Overcast")  # falls back to the forecast

    @patch("snow_report.views.local_now", fake_now(2026, 11, 8))
    def test_shoulder_season_without_snow_data(self):
        ctx = build_context(fixture("weather_current.html"), None, "metric")
        self.assertEqual(ctx["season"], "shoulder")

    def test_imperial_conversion(self):
        ctx = build_context(fixture("weather_bad_data.html"), parse_forecast(open_meteo_payload()), "imperial")
        self.assertEqual(ctx["temperatures"][0]["value"], 28)            # -2 °C
        self.assertEqual(ctx["temperatures"][0]["elevation"], "6,824")   # 2080 m
        self.assertEqual([s["value"] for s in ctx["new_snow"]], [0, None, 0, 9.4])
        self.assertEqual([b["value"] for b in ctx["base"]], [62, 68])
        self.assertEqual(ctx["wind"][0]["value"], 7)                     # 11 km/h
        self.assertEqual(ctx["forecast"][0]["snow"], 3.3)                # 8.4 cm

    def test_wind_arrow_points_downwind(self):
        ctx = build_context(fixture("weather_bad_data.html"), None, "metric")
        self.assertEqual([(w["direction"], w["rotation"]) for w in ctx["wind"]][:2], [("S", 0.0), ("WSW", 67.5)])

    def test_stations_ordered_top_down_and_spread_up_the_slope(self):
        ctx = build_context(fixture("weather_current.html"), None, "metric")
        self.assertEqual([t["name"] for t in ctx["temperatures"]][0], "Top of the World")
        self.assertEqual([t["height_pct"] for t in ctx["temperatures"]], [88, 64, 40, 16])

    @patch("snow_report.views.local_now", fake_now(2026, 1, 10, hour=20))
    def test_today_is_the_resort_date_not_utc(self):
        ctx = build_context(None, parse_forecast(open_meteo_payload()), "metric")
        self.assertEqual([d["name"] for d in ctx["forecast"][:2]], ["Today", "Sun"])

    @patch("snow_report.views.local_now", fake_now(2026, 1, 11, hour=1))
    def test_forecast_cached_before_midnight_starts_at_today(self):
        ctx = build_context(None, parse_forecast(open_meteo_payload()), "metric")
        self.assertEqual(ctx["today"]["date"], "Jan 11")
        self.assertEqual(ctx["forecast"][0]["name"], "Today")

    @patch("snow_report.views.local_now", fake_now(2026, 1, 10))
    def test_background_matches_the_resort_report_over_the_model(self):
        # Resort reports flurries while the model says overcast: the snowflake hero gets snow, not clouds.
        resort = dict(fixture("weather_bad_data.html"), icon="fas fa-snowflake")
        ctx = build_context(resort, parse_forecast(open_meteo_payload()), "metric", {"code": 3, "is_day": True})
        self.assertEqual((ctx["hero_icon"], ctx["wx"]), ("fas fa-snowflake", "snow"))
        resort["icon"] = "fas fa-sun"
        ctx = build_context(resort, None, "metric", {"code": 3, "is_day": False})
        self.assertEqual(ctx["wx"], "stars")  # morning report, but it's dark now

    @patch("snow_report.views.local_now", fake_now(2026, 1, 10))
    def test_background_follows_current_conditions(self):
        forecast = parse_forecast(open_meteo_payload())
        self.assertEqual(build_context(None, forecast, "metric", {"code": 75, "is_day": True})["wx"], "snow")
        self.assertEqual(build_context(None, forecast, "metric")["wx"], "clouds")  # daily code 3
        self.assertIsNone(build_context(None, None, "metric")["wx"])

    @patch("snow_report.views.local_now", fake_now(2026, 1, 10, hour=22))
    def test_hero_shows_current_conditions_without_a_resort_report(self):
        forecast = parse_forecast(open_meteo_payload())  # daily code 3: Overcast
        ctx = build_context(fixture("weather_current.html"), forecast, "metric", {"code": 0, "is_day": False})
        self.assertEqual((ctx["hero_icon"], ctx["hero_text"]), ("fas fa-moon", "Clear"))
        self.assertEqual(ctx["forecast"][0]["label"], "Overcast")  # the day summary stays in the forecast
        ctx = build_context(fixture("weather_bad_data.html"), forecast, "metric", {"code": 0, "is_day": False})
        self.assertTrue(ctx["hero_text"].startswith("Cloudy with sunny breaks"))  # resort report wins

    @patch("snow_report.views.local_now", fake_now(2026, 1, 10, hour=22))
    def test_background_falls_back_to_night_after_sunset(self):
        payload = open_meteo_payload()
        payload[0]["daily"]["weather_code"][0] = 0
        self.assertEqual(build_context(None, parse_forecast(payload), "metric")["wx"], "stars")

    def test_everything_unavailable(self):
        ctx = build_context(None, None, "metric")
        self.assertFalse(ctx["resort_ok"])
        self.assertEqual(ctx["temperatures"], [])


class ViewTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch("snow_report.views.get_forecast", side_effect=lambda: parse_forecast(open_meteo_payload()))
    @patch("snow_report.views.get_resort", side_effect=lambda: fixture("weather_bad_data.html"))
    def test_winter_page(self, *_):
        html = self.client.get("/snow_report/").content.decode()
        self.assertIn("Mountain Conditions", html)
        self.assertIn("Ski season", html)
        self.assertIn("Snow base", html)
        self.assertIn("5-day forecast", html)
        # The units switch shows metric selected and flips to imperial.
        self.assertIn('class="mc-units" data-unit="metric"', html)
        self.assertIn('href="?units=imperial"', html)

    @patch("snow_report.views.local_now", fake_now(2026, 7, 15))
    @patch("snow_report.views.get_forecast", return_value=None)
    @patch("snow_report.views.get_resort", side_effect=lambda: fixture("weather_current.html"))
    def test_summer_page_hides_snow_and_missing_forecast(self, *_):
        html = self.client.get("/snow_report/?units=imperial").content.decode()
        self.assertIn("Summer", html)
        self.assertIn("Air quality", html)
        self.assertNotIn("Snow base", html)
        self.assertNotIn("5-day forecast", html)
        self.assertIn("6,824 ft", html)
        self.assertIn('class="mc-units" data-unit="imperial"', html)
        self.assertIn('href="?units=metric"', html)

    @patch("snow_report.views.get_current", return_value={"code": 3, "is_day": True})
    @patch("snow_report.views.get_forecast", return_value=None)
    @patch("snow_report.views.get_resort", side_effect=lambda: fixture("weather_bad_data.html"))
    def test_weather_background_and_preview_override(self, *_):
        # The resort reports partly cloudy while the model says overcast: the resort wins.
        self.assertIn('data-wx="partly"', self.client.get("/snow_report/").content.decode())
        self.assertIn('data-wx="rain"', self.client.get("/snow_report/?wx=rain").content.decode())
        self.assertIn('data-wx="partly"', self.client.get("/snow_report/?wx=<b>").content.decode())

    @patch("snow_report.views.get_forecast", return_value=None)
    @patch("snow_report.views.get_resort", return_value=None)
    def test_sources_down(self, *_):
        response = self.client.get("/snow_report/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Conditions are unavailable", response.content.decode())


class HubUnitsTests(TestCase):
    """The page defaults to the hub's units; ?units= only changes the view."""

    def setUp(self):
        from bdh_sdk import settings
        from bdh_sdk.testing import FakeBroker

        saved = settings._store
        self.broker = FakeBroker(grants=("settings.get",), install=True)
        self.broker.start()
        self.addCleanup(settings.set_store, saved)
        self.addCleanup(self.broker.stop)

    def test_default_units_follow_hub(self):
        from snow_report.views import default_units
        self.broker.settings[("hub", "units")] = "imperial"
        self.assertEqual(default_units(), "imperial")

    def test_broker_refusal_falls_back_to_metric(self):
        from snow_report.views import default_units
        self.broker.grants = []
        self.assertEqual(default_units(), "metric")

    @patch("snow_report.views.get_current", return_value=None)
    @patch("snow_report.views.get_forecast", return_value=None)
    @patch("snow_report.views.get_resort", side_effect=lambda: fixture("weather_current.html"))
    def test_page_uses_hub_units_unless_overridden(self, *_):
        self.broker.settings[("hub", "units")] = "imperial"
        html = self.client.get("/snow_report/").content.decode()
        self.assertIn('class="mc-units" data-unit="imperial"', html)
        html = self.client.get("/snow_report/?units=metric").content.decode()
        self.assertIn('class="mc-units" data-unit="metric"', html)


@tag("selenium")
class SnowReportScrollHintTest(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from tests.selenium_helpers import get_chrome_options
        cls.browser = webdriver.Chrome(options=get_chrome_options())
        cls.browser.set_window_size(1280, 700)

    @classmethod
    def tearDownClass(cls):
        cls.browser.quit()
        super().tearDownClass()

    @patch("snow_report.views.get_forecast", side_effect=lambda: parse_forecast(open_meteo_payload()))
    @patch("snow_report.views.get_resort", side_effect=lambda: fixture("weather_bad_data.html"))
    def test_scroll_hint_hides_at_bottom(self, *_):
        # The chevron comes from the hub's shared scroll hint (base.html).
        self.browser.get(f"{self.live_server_url}/snow_report/")
        WebDriverWait(self.browser, 10).until(
            lambda d: d.execute_script("return !!document.querySelector('.hub-scroll-hint.on')"),
            "Scroll hint should show while there is more to scroll",
        )
        self.browser.execute_script("window.scrollTo(0, document.body.scrollHeight); document.body.scrollTop = document.body.scrollHeight;")
        WebDriverWait(self.browser, 5).until(
            lambda d: d.execute_script("return !document.querySelector('.hub-scroll-hint.on')"),
            "Scroll hint should hide after scrolling to bottom",
        )


class HomepageHookTests(TestCase):
    def setUp(self):
        cache.clear()

    def status(self, resort):
        with patch("snow_report.homepage.get_resort", return_value=resort):
            from snow_report.homepage import status
            return status()

    def test_new_snow(self):
        resort = fixture("weather_bad_data.html")
        resort["new_snow"][0]["cm"] = 14.0
        result = self.status(resort)
        self.assertEqual((result.text, result.level), ("14 cm new snow", "on"))

    def test_skips_missing_snow_values(self):
        resort = fixture("weather_bad_data.html")
        resort["new_snow"] = [{"period": "New Snow", "cm": None}, {"period": "24 Hr", "cm": 3.0}]
        self.assertEqual(self.status(resort).text, "3 cm in 24 hr")

    def test_off_season_shows_alpine_temperature(self):
        result = self.status(fixture("weather_current.html"))
        self.assertEqual((result.text, result.level), ("4 °C at Top of the World", "info"))

    def test_unavailable(self):
        self.assertIsNone(self.status(None))

    def test_readings_up_top_mid_and_base(self):
        resort = fixture("weather_current.html")
        resort["temperatures"] = [
            {"name": "Valley", "elevation_m": 1255, "c": 1.4},
            {"name": "Top of the World", "elevation_m": 2080, "c": -6.6},
            {"name": "Mid Mountain", "elevation_m": 1650, "c": -2.0},
        ]
        result = self.status(resort)
        self.assertEqual(
            [(r.label, r.value, r.unit) for r in result.readings],
            [("Up top", -7, "C"), ("Mid", -2, "C"), ("Base", 1, "C")],
        )

    def test_readings_skip_missing_and_repeated_rows(self):
        resort = fixture("weather_bad_data.html")
        resort["new_snow"][0]["cm"] = 5.0
        resort["temperatures"] = [{"name": "Top of the World", "elevation_m": 2080, "c": 3.0}]
        result = self.status(resort)
        self.assertEqual(result.text, "5 cm new snow")
        self.assertEqual([(r.label, r.value) for r in result.readings], [("Up top", 3)])

class ProvidesTests(TestCase):
    NOW = datetime.datetime(2026, 1, 10, 12, tzinfo=RESORT_TZ)

    def stats(self, resort, forecast=None):
        from snow_report.provides import build_stats
        return {s.key: s for s in build_stats(resort, forecast, self.NOW)}

    def test_winter(self):
        stats = self.stats(fixture("weather_bad_data.html"), parse_forecast(open_meteo_payload()))
        self.assertEqual({k: s.value for k, s in stats.items() if s.value}, {
            "temp_top": "\u22122°C", "temp_mid": "0°C", "temp_village": "3°C",
            "snow_48h": "0 cm", "base": "158 cm",
            "wind_top": stats["wind_top"].value,
        })
        self.assertNotIn("snow_24h", stats)  # "24 Hr" is missing in this capture
        self.assertEqual(stats["sunset"].time, datetime.datetime(2026, 1, 10, 16, 40, tzinfo=RESORT_TZ))
        # This morning's sunrise has passed: the next one is tomorrow's.
        self.assertEqual(stats["sunrise"].time, datetime.datetime(2026, 1, 11, 7, 45, tzinfo=RESORT_TZ))
        self.assertEqual((stats["sun"].label, stats["sun"].time), ("Sunset", stats["sunset"].time))

    def test_next_sun_after_sunset_is_sunrise(self):
        from snow_report.provides import build_stats
        evening = datetime.datetime(2026, 1, 10, 20, tzinfo=RESORT_TZ)
        stats = {s.key: s for s in build_stats({}, parse_forecast(open_meteo_payload()), evening)}
        self.assertEqual((stats["sun"].label, stats["sun"].time),
                         ("Sunrise", datetime.datetime(2026, 1, 11, 7, 45, tzinfo=RESORT_TZ)))

    def test_off_season(self):
        stats = self.stats(fixture("weather_current.html"))
        self.assertEqual(stats["wind_top"].value, "16 km/h SSE")
        self.assertFalse({"snow_24h", "snow_48h", "base", "sunrise"} & set(stats))

    def test_declared_keys_cover_provided(self):
        from snow_report.plugin import STAT_KEYS, spec
        declared = {k for k, _ in STAT_KEYS}
        stats = self.stats(fixture("weather_bad_data.html"), parse_forecast(open_meteo_payload()))
        self.assertLessEqual(set(stats), declared)
        self.assertEqual(spec.provides, {"stats": "snow_report.provides.stats"})

    def test_unavailable(self):
        self.assertEqual(self.stats(None), {})


class ManifestTests(TestCase):
    def test_hub_api_2_manifest(self):
        from snow_report.plugin import spec
        m = spec.manifest()
        self.assertEqual(m["api_version"], 2)
        self.assertEqual(m["permissions"], [])
        self.assertEqual((m["network"], m["lifecycle"], m["memory_mb"]), ("outbound", "on_demand", 96))

class AssistantToolTests(TestCase):
    """The assistant's snow report, run the way the hub runs it (FakeBroker sessions)."""

    def setUp(self):
        from bdh_sdk import Stay, settings
        from bdh_sdk.testing import FakeBroker

        saved = settings._store
        self.broker = FakeBroker(grants=("settings.get", "tools.*"), install=True, app_id="snow_report")
        self.broker.start()
        self.addCleanup(settings.set_store, saved)
        self.addCleanup(self.broker.stop)
        self.broker.stay = Stay(guest="Smith")  # a voice session during a stay is in guest mode
        self.resort = fixture("weather_bad_data.html")  # Feb 27, in season
        self.now = datetime.datetime(2026, 2, 28, 16, 0, tzinfo=datetime.timezone.utc)  # 9 am at the lodge
        for target, kwargs in (("snow_report.assistant.get_resort", {"side_effect": lambda: self.resort}),
                               ("snow_report.assistant.utc_now", {"side_effect": lambda: self.now})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, args=None):
        from bdh_sdk import tools as sdk_tools
        from snow_report import assistant

        self.broker.assistant_tools.clear()
        for tool in assistant.tools(mode="guest"):
            self.broker.add_tool(tool, lambda a, t=tool.id: assistant.run_tool(t, a, mode="guest"))
        return sdk_tools.call_tool(sdk_tools.open_session(), "snow_report", args)

    def test_every_mode_gets_one_readonly_lookup(self):
        from bdh_sdk import ALL_MODES, hooks
        from snow_report import assistant
        from snow_report.plugin import spec
        for mode in sorted(ALL_MODES):
            with self.subTest(mode=mode):
                (tool,) = assistant.tools(mode=mode)
                self.assertEqual((tool.id, tool.modes, tool.readonly, tool.confirm),
                                 ("snow_report", ALL_MODES, True, False))
                self.assertLessEqual(len(tool.description), 300)
        listed = hooks.run(spec, "tools", mode="guest")
        self.assertEqual([t["id"] for t in listed], ["snow_report"])

    def test_in_season_report(self):
        result = self.call()
        self.assertTrue(result["ok"], result)
        report = result["result"]
        self.assertEqual(report["updated"], "yesterday at 6:43 pm")
        self.assertEqual(report["new_snow"], {"overnight": "0 cm", "48 hours": "0 cm", "7 days": "24 cm"})
        self.assertEqual(report["base"], {"mid mountain": "158 cm", "alpine": "172 cm"})
        self.assertTrue(report["conditions"].startswith("Cloudy with sunny breaks"))
        self.assertEqual(list(report["temperatures"].items())[0], ("Top of the World", "-2°C"))
        self.assertEqual(report["temperatures"]["Valley"], "3°C")
        self.assertNotIn("note", report)

    def test_imperial_hub(self):
        self.broker.settings[("hub", "units")] = "imperial"
        report = self.call()["result"]
        self.assertEqual(report["base"], {"mid mountain": "62 in", "alpine": "68 in"})
        self.assertEqual(report["temperatures"]["Valley"], "37°F")

    def test_old_report_says_so(self):
        self.now = datetime.datetime(2026, 3, 3, 16, 0, tzinfo=datetime.timezone.utc)
        report = self.call()["result"]
        self.assertEqual(report["updated"], "February 27 at 6:43 pm")
        self.assertIn("out of date", report["note"])

    def test_off_season_keeps_temperatures_and_wind(self):
        from snow_report.assistant import OFF_SEASON
        self.resort = fixture("weather_current.html")
        report = self.call()["result"]
        self.assertEqual(report["snow_report"], OFF_SEASON)
        self.assertNotIn("new_snow", report)
        self.assertEqual(report["wind_up_top"], "16 km/h from the south-southeast")
        self.assertEqual(len(report["temperatures"]), 4)

    def test_page_down(self):
        from snow_report.assistant import UNAVAILABLE
        self.resort = None
        self.assertEqual(self.call(), {"ok": False, "error": UNAVAILABLE})

    def test_updated_stamp_across_new_year(self):
        from snow_report.assistant import parse_updated
        self.assertEqual(parse_updated("Updated December 31st at 7:05am", datetime.datetime(2027, 1, 1, 8)),
                         datetime.datetime(2026, 12, 31, 7, 5))
        self.assertIsNone(parse_updated("Updated sometime", datetime.datetime(2027, 1, 1, 8)))