#!/usr/bin/env python3
"""Archive mountain road and weather conditions. Python standard library only.

Each run fetches every source, writes normalized snapshots to data/current/,
appends chain-control status changes to data/log/, and records source health
in data/health.json. Files are rewritten only with stable content, so git
history is the archive and an unchanged run produces no commit.

Exit status is 1 only when a source has newly started failing, so a long
outage produces one failed workflow run rather than one per poll.
"""
from __future__ import annotations

import datetime as dt
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import tempfile
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
PACIFIC = ZoneInfo('America/Los_Angeles')
# Caltrans' nginx returns 500 to curl's default User-Agent; any other UA works.
USER_AGENT = 'road-conditions-archive/0.1 (+https://github.com/nielsenz/road-conditions-archive)'
CALTRANS = 'https://cwwp2.dot.ca.gov/data'

# Chain-control feeds are small and entirely mountain routes: keep every segment.
CHAIN_DISTRICTS = ['d7', 'd8']
# Lane-closure feeds are large and statewide-ish: keep only routes the sites cover.
CLOSURE_ROUTES = {
    'd7': {'SR-2', 'SR-39'},
    'd8': {'SR-2', 'SR-18', 'SR-38', 'SR-138', 'SR-173', 'SR-189', 'SR-330'},
}
HIGHWAY_TEXT_ROUTES = ['2', '18', '38', '39', '138', '173', '189', '330']
NWS_LOCATIONS = {
    'big-bear-lake': (34.2439, -116.9114),
    'lake-arrowhead': (34.2483, -117.1892),
}
NWS_GRID_FIELDS = ['snowfallAmount', 'snowLevel', 'probabilityOfPrecipitation',
                   'quantitativePrecipitation', 'temperature', 'windGust']
CHAIN_STATUS = re.compile(r'^R-[0-3]$')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def fetch(url, accept='application/json'):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': accept})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch_json(url, accept='application/json'):
    return json.loads(fetch(url, accept))


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write('\n')
        temporary = Path(f.name)
    temporary.replace(path)


def pacific_to_utc(date, time):
    """Caltrans dates/times are Pacific local; blank or 'Not Reported' -> None."""
    if not date or not time or 'Not' in date:
        return None
    local = dt.datetime.fromisoformat(f'{date}T{time}').replace(tzinfo=PACIFIC)
    return local.astimezone(dt.timezone.utc).isoformat(timespec='seconds')


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def caltrans_records(payload):
    # {"data": [{"cc": {...}}, ...]}: unwrap the single feed-named key.
    return [next(iter(item.values())) for item in payload['data']]


# Chain control ---------------------------------------------------------------

def normalize_chain(record):
    location, status = record['location'], record['statusData']
    raw = status['status']
    valid = bool(CHAIN_STATUS.match(raw))
    return {
        'id': record['index'],
        'route': location['route'],
        'direction': location['direction'],
        'location': ' '.join(location['locationName'].split()),
        'county': location['county'],
        'elevation_ft': number(location['elevation']),
        'lat': number(location['latitude']),
        'lon': number(location['longitude']),
        'in_service': record['inService'] == 'true',
        # Some D7 records carry a longitude in the status field: keep it, flag it.
        'status': raw if valid else None,
        'raw_status': None if valid else raw,
        'description': status['statusDescription'] if valid else None,
        'status_changed_at': pacific_to_utc(status['statusTimestamp']['statusDate'],
                                            status['statusTimestamp']['statusTime']),
    }


def chain_events(previous, current, observed_at):
    """Status transitions between two snapshots, keyed by segment id."""
    before = {s['id']: s for s in previous or []}
    events = []
    for segment in current:
        old = before.get(segment['id'])
        if segment['status'] is None or (old and old['status'] == segment['status']):
            continue
        events.append({
            'observed_at': observed_at,
            'id': segment['id'],
            'route': segment['route'],
            'direction': segment['direction'],
            'location': segment['location'],
            'elevation_ft': segment['elevation_ft'],
            'from': old['status'] if old else None,
            'to': segment['status'],
            'status_changed_at': segment['status_changed_at'],
        })
    return events


def collect_chain_control(observed_at):
    path = DATA / 'current' / 'chain_control.json'
    segments = []
    for district in CHAIN_DISTRICTS:
        code = district.upper().replace('D', 'D0')
        payload = fetch_json(f'{CALTRANS}/{district}/cc/ccStatus{code}.json')
        segments += [normalize_chain(r) for r in caltrans_records(payload)]
    segments.sort(key=lambda s: s['id'])
    previous = read_json(path, {}).get('segments')
    events = chain_events(previous, segments, observed_at)
    if events:
        log = DATA / 'log' / 'chain_control' / f'{observed_at[:7]}.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a') as f:
            for event in events:
                f.write(json.dumps(event, sort_keys=True) + '\n')
    write_json(path, {'segments': segments})
    return f'{len(segments)} segments, {len(events)} changes'


# Lane closures ---------------------------------------------------------------

def closure_state(closure):
    if closure['code1022']['isCode1022'] == 'true':
        return 'cancelled'
    if closure['code1098']['isCode1098'] == 'true':
        return 'completed'
    if closure['code1097']['isCode1097'] == 'true':
        return 'in_effect'
    return 'scheduled'


def normalize_closure(record):
    begin, end = record['location']['begin'], record['location']['end']
    closure, times = record['closure'], record['closure']['closureTimestamp']
    return {
        'id': record['index'],
        'route': begin['beginRoute'],
        'direction': record['location']['travelFlowDirection'],
        'from': begin['beginLocationName'],
        'to': end['endLocationName'],
        'nearby': begin['beginNearbyPlace'],
        'begin_postmile': number(begin['beginPostmile']),
        'end_postmile': number(end['endPostmile']),
        'lat': number(begin['beginLatitude']),
        'lon': number(begin['beginLongitude']),
        'type': closure['typeOfClosure'],
        'work': closure['typeOfWork'],
        'lanes_closed': closure['lanesClosed'],
        'total_lanes': closure['totalExistingLanes'],
        'estimated_delay': closure['estimatedDelay'],
        'starts_at': pacific_to_utc(times['closureStartDate'], times['closureStartTime']),
        'ends_at': pacific_to_utc(times['closureEndDate'], times['closureEndTime']),
        'end_indefinite': times['isClosureEndIndefinite'] == 'true',
        'state': closure_state(closure),
    }


def collect_closures(observed_at):
    closures = []
    for district, routes in CLOSURE_ROUTES.items():
        code = district.upper().replace('D', 'D0')
        payload = fetch_json(f'{CALTRANS}/{district}/lcs/lcsStatus{code}.json')
        closures += [normalize_closure(r) for r in caltrans_records(payload)
                     if r['location']['begin']['beginRoute'] in routes]
    closures.sort(key=lambda c: c['id'])
    write_json(DATA / 'current' / 'lane_closures.json', {'closures': closures})
    in_effect = sum(c['state'] == 'in_effect' for c in closures)
    return f'{len(closures)} closures, {in_effect} in effect'


# Highway information text (roads.dot.ca.gov) ---------------------------------

class HighwayText(HTMLParser):
    """Collects <h3>route</h3> followed by <p> items until the next <h3>."""

    def __init__(self):
        super().__init__()
        self.routes, self.route, self.tag, self.buffer = {}, None, None, []

    def handle_starttag(self, tag, attrs):
        if tag in ('h3', 'p'):
            self.tag, self.buffer = tag, []

    def handle_endtag(self, tag):
        if tag != self.tag:
            return
        text = ' '.join(''.join(self.buffer).split())
        if tag == 'h3':
            self.route = text
            self.routes[text] = []
        elif self.route and text:
            area = re.match(r'^\[([^\]]+)\]\s*', text)
            if area:
                text = text[area.end():]
            self.routes[self.route].append({'area': area.group(1) if area else None, 'text': text})
        self.tag = None

    def handle_data(self, data):
        if self.tag:
            self.buffer.append(data)


def parse_highway_text(page):
    parser = HighwayText()
    parser.feed(page)
    return parser.routes


def collect_highway_text(observed_at):
    url = 'https://roads.dot.ca.gov/?roadnumber=' + ','.join(HIGHWAY_TEXT_ROUTES)
    routes = parse_highway_text(fetch(url, 'text/html').decode('utf-8', 'replace'))
    missing = [r for r in HIGHWAY_TEXT_ROUTES if not any(k.split()[-1] == r for k in routes)]
    if missing:
        raise ValueError(f'routes missing from page: {missing}')
    write_json(DATA / 'current' / 'highway_text.json', {'routes': routes})
    return f'{sum(map(len, routes.values()))} items on {len(routes)} routes'


# National Weather Service ----------------------------------------------------

def collect_nws(observed_at):
    accept = 'application/geo+json'
    summary = []
    for slug, (lat, lon) in NWS_LOCATIONS.items():
        point = fetch_json(f'https://api.weather.gov/points/{lat},{lon}', accept)['properties']
        grid = fetch_json(point['forecastGridData'], accept)['properties']
        forecast = fetch_json(point['forecast'], accept)['properties']
        zone = point['forecastZone'].rsplit('/', 1)[-1]
        alerts = fetch_json(f'https://api.weather.gov/alerts/active?zone={zone}', accept)['features']
        write_json(DATA / 'current' / 'nws' / f'{slug}.json', {
            'grid_id': f"{point['gridId']}/{point['gridX']},{point['gridY']}",
            'zone': zone,
            'grid_updated_at': grid['updateTime'],
            'grid_elevation_m': grid['elevation']['value'],
            'grid': {field: {'unit': grid[field]['uom'],
                             'values': [[v['validTime'], v['value']] for v in grid[field]['values']]}
                     for field in NWS_GRID_FIELDS if field in grid},
            'forecast_updated_at': forecast['updateTime'],
            'forecast': [{key: p[key] for key in ('name', 'startTime', 'endTime', 'temperature',
                                                   'temperatureUnit', 'shortForecast', 'detailedForecast')}
                         for p in forecast['periods']],
            'alerts': sorted(({key: a['properties'].get(key) for key in (
                'id', 'event', 'severity', 'headline', 'onset', 'ends', 'expires')}
                for a in alerts), key=lambda a: a['id']),
        })
        summary.append(f'{slug}: {len(alerts)} alerts')
    return ', '.join(summary)


# Resort operating status -----------------------------------------------------

# Lift and trail status is rendered client-side from MtnPowder with the resort's
# own bearer token, so it is not collected. The hours page is server-rendered and
# covers Snow Summit, Snow Valley and Bear Mountain (snow-valley.com redirects here).
RESORT_PAGES = {
    'big-bear-mountain-resort': 'https://www.bigbearmountainresort.com/hours',
}
BLOCK_TAGS = {'p', 'br', 'div', 'li', 'tr', 'td', 'th', 'section', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}


class MainText(HTMLParser):
    """Text lines inside <main>, without scripts or hidden component markers."""

    def __init__(self):
        super().__init__()
        self.depth, self.skip, self.lines, self.line = 0, 0, [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'main':
            self.depth += 1
        elif self.depth and tag in ('script', 'style', 'noscript', 'var'):
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.flush()

    def handle_endtag(self, tag):
        if tag == 'main' and self.depth:
            self.flush()
            self.depth -= 1
        elif self.depth and tag in ('script', 'style', 'noscript', 'var') and self.skip:
            self.skip -= 1
        elif tag in BLOCK_TAGS:
            self.flush()

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.line.append(data)

    def flush(self):
        text = ' '.join(''.join(self.line).split())
        if text:
            self.lines.append(text)
        self.line = []


def parse_main_text(page):
    parser = MainText()
    parser.feed(page)
    return parser.lines


def collect_resort_hours(observed_at):
    summary = []
    for slug, url in RESORT_PAGES.items():
        lines = parse_main_text(fetch(url, 'text/html').decode('utf-8', 'replace'))
        if not any('Snow Summit' in line for line in lines):
            raise ValueError(f'{slug}: page layout changed, Snow Summit not found in <main>')
        write_json(DATA / 'current' / 'resorts' / f'{slug}.json', {'url': url, 'lines': lines})
        summary.append(f'{slug}: {len(lines)} lines')
    return ', '.join(summary)


# Run -------------------------------------------------------------------------

SOURCES = {
    'chain_control': collect_chain_control,
    'lane_closures': collect_closures,
    'highway_text': collect_highway_text,
    'nws': collect_nws,
    'resort_hours': collect_resort_hours,
}


def main():
    observed_at = now()
    health_path = DATA / 'health.json'
    health = read_json(health_path, {})
    newly_failing = []
    for name, collect in SOURCES.items():
        previous = health.get(name, {})
        try:
            print(f'{name}: {collect(observed_at)}')
            ok, error = True, None
        except Exception as e:  # one bad source must not stop the others
            print(f'{name}: FAILED {type(e).__name__}: {e}', file=sys.stderr)
            ok, error = False, f'{type(e).__name__}: {e}'[:500]
        # Only state flips are written, so steady health does not cause commits.
        if previous.get('ok') != ok:
            health[name] = {'ok': ok, 'since': observed_at, 'error': error}
            if not ok:
                newly_failing.append(name)
    write_json(health_path, health)
    return 1 if newly_failing else 0


if __name__ == '__main__':
    sys.exit(main())
