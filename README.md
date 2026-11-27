# bdh-app-snow-report

Black Diamond Hub app: **Mountain Conditions** for Sun Peaks (tile title; the
app id, package and URL are still `snow_report`). Moved out of
[BlackDiamondHub](https://github.com/blackdiamondhub/BlackDiamondHub) and
installed into the hub through the `blackdiamondhub.plugins` entry point
(see [bdh-sdk](https://github.com/blackdiamondhub/bdh-sdk)).

Page: `/snow_report/`. Units follow the hub's setting; `?units=metric|imperial` (the page's switch) changes only the view.

## Data sources

- **Sun Peaks Resort** conditions page (`resort.py`, cached 10 min): the
  daily narrative, new snow, snow base, temperatures by elevation, wind and
  air quality (AQHI). Sun Peaks drops the narrative and snow sections out of
  season, and the page hides whichever sections are missing.
- **Open-Meteo** (`forecast.py`, cached 30 min, no API key): the 5-day
  forecast for the village (1,255 m) and the alpine (2,080 m).

## Today card background

The Today card has a subtle animated background (clouds, sun, stars, fog,
drizzle, rain, snow or storm). It follows the village's current weather code
from Open-Meteo, falling back to today's forecast code (day or night from
sunrise and sunset). Preview any effect with `?wx=<effect>`, e.g.
`/snow_report/?wx=snow`. The animation stops when the device asks for
reduced motion.

Without a resort report (off-season), the Today card's icon and text also
show current conditions, so they match the background. The whole-day summary
stays in the forecast's Today tile.

## Seasons

- **Ski season:** Sun Peaks is publishing snow data. Shows snowfall, snow
  base and alpine snow in the forecast.
- **Summer** (June–September without snow data): warm theme. Shows UV,
  chance of rain and sunset.
- **Off-season** (any other month without snow data): the winter layout
  without the snow cards, so you can watch the forecast for the first snow.

The homepage tile shows new snow when there is some. Otherwise it shows the
temperature at the top of the mountain.

## Assistant tools

The hub's assistant (e.g. the voice app) can read the snow report through one read-only tool,
offered in every mode (guest, dashboard and owner). The hub only offers it when `snow_report` is
in its `assistant_apps` setting (Settings > Location > Assistant).

| Tool | Params | Answers |
|---|---|---|
| `snow_report` | — | When the resort last updated the report, new snow (overnight, 24 hours, 48 hours, 7 days), snow base (mid mountain, alpine), the resort's conditions summary, temperature at each station, wind up top, and air quality when it's high risk |

Answers are small dicts of speech-ready strings in the hub's units ("4 cm", "-2°C"), with the
update time in lodge time (UTC-7 all year), e.g. "today at 12:17 pm". A report more than 36
hours old carries a `note` that the new snow may be out of date. Off season (no snow sections on
the page) the answer says there's no snow report and still gives temperatures and wind. When the
resort page can't be fetched, the tool fails with a short sentence the assistant can say.
Forecasts are the weather app's job.

## Container (hub API 2)

Since 3.0.0 the app runs in its own container: no broker permissions (it
uses no hub data), `network: outbound` (sunpeaksresort.com, Open-Meteo),
`lifecycle: on_demand`, `memory_mb: 96`. The hub pulls the status tile and
the `stats` provider from the container, waking it if it's stopped.
CI's `image` job (from bdh-sdk's `app-ci.yml`) locks the dependencies
against the hub's app runtime, builds the app's image and smoke-tests it
the way the supervisor runs it.

## Development

CI (`.github/workflows/ci.yml`) uses the reusable `app-ci.yml` from
bdh-sdk. It builds the wheel, installs it next to the hub, then runs
`hub_smoke`, the app tests and the selenium tests. CI also runs daily, because some tests hit the live site this app scrapes.

To release, bump `version` in `pyproject.toml`. When that lands on
`main`, `release.yml` tags it and publishes the wheel, and the release
notes include the hashed line for the hub's `sites/<site>/apps.txt`.
