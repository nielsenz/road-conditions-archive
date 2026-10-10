# Road conditions archive

Polls public mountain road and weather sources every 10 minutes via GitHub
Actions and commits only what changed, so git history is the archive. Built to
back live conditions pages for Big Bear, Lake Arrowhead and the Angeles Crest
(SR-2) and to measure how often chain controls and closures actually happen.

## Sources

| Output | Source | Kept |
|---|---|---|
| `data/current/chain_control.json` | Caltrans CWWP2 chain controls, districts 7 and 8 | every segment |
| `data/log/chain_control/YYYY-MM.jsonl` | derived | one line per segment status change |
| `data/current/lane_closures.json` | Caltrans CWWP2 lane closures, districts 7 and 8 | SR-2, 18, 38, 39, 138, 173, 189, 330 |
| `data/current/highway_text.json` | roads.dot.ca.gov highway information | same routes; includes unplanned full closures |
| `data/current/nws/<place>.json` | api.weather.gov grid, forecast, active alerts | Big Bear Lake, Lake Arrowhead |
| `data/current/resorts/big-bear-mountain-resort.json` | bigbearmountainresort.com/hours (server-rendered) | text of `<main>`: Snow Summit, Snow Valley, Bear Mountain open/closed and daily hours |
| `data/health.json` | derived | per-source ok/failing, written only on state change |

All timestamps are UTC. Caltrans publishes Pacific local times; they are converted.

## Notes

- Caltrans' server returns HTTP 500 to curl's default User-Agent. The collector sends its own.
- Some district 7 chain records carry a longitude in the status field. They are kept with
  `status: null` and the original in `raw_status`, and never logged as changes.
- Chain status changes are only seen at poll time. `status_changed_at` is Caltrans' own
  timestamp, but a change and its reversal inside one poll interval are missed.
- Resort lift and trail status is not collected. The resort page renders it client-side from
  MtnPowder's API using the resort's own bearer token; using that token would mean borrowing a
  credential that isn't ours. Liftie is behind a Cloudflare challenge, and Mountain High's
  robots.txt disallows all crawling. snow-valley.com now redirects to Big Bear Mountain Resort.
- A run exits non-zero only when a source newly fails, so an outage produces one failed run.
- The first log lines (`from: null`) are the baseline, not real changes.

## Local

```sh
uv run --no-project --python 3.12 python -m unittest discover -s tests
uv run --no-project --python 3.12 python collect.py
```

Standard library only.
