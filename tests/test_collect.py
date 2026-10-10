import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def fixture(name):
    return collect.caltrans_records(json.loads((FIXTURES / name).read_text()))


class ChainControl(unittest.TestCase):
    def test_valid_and_malformed_status(self):
        segments = [collect.normalize_chain(r) for r in fixture('chain_d07.json')]
        valid, malformed = segments[:2], segments[2]
        self.assertTrue(all(s['status'] == 'R-0' for s in valid))
        self.assertIsNone(malformed['status'])
        self.assertTrue(malformed['raw_status'].startswith('-11'))
        self.assertIsNone(malformed['lat'])

    def test_pacific_time_converted_to_utc(self):
        self.assertEqual(collect.pacific_to_utc('2026-01-15', '08:00:00'), '2026-01-15T16:00:00+00:00')
        self.assertEqual(collect.pacific_to_utc('2026-07-15', '08:00:00'), '2026-07-15T15:00:00+00:00')
        self.assertIsNone(collect.pacific_to_utc('', ''))

    def test_events_only_on_change(self):
        segments = [collect.normalize_chain(r) for r in fixture('chain_d07.json')]
        first = collect.chain_events(None, segments, 't0')
        self.assertEqual([e['from'] for e in first], [None, None])  # malformed skipped
        self.assertEqual(collect.chain_events(segments, segments, 't1'), [])
        changed = [dict(segments[0], status='R-2')] + segments[1:]
        events = collect.chain_events(segments, changed, 't2')
        self.assertEqual([(e['from'], e['to']) for e in events], [('R-0', 'R-2')])


class Closures(unittest.TestCase):
    def test_normalize(self):
        closures = [collect.normalize_closure(r) for r in fixture('closures_d08.json')]
        self.assertEqual(closures[0]['route'], 'SR-18')
        self.assertIn(closures[0]['state'], {'scheduled', 'in_effect', 'completed', 'cancelled'})
        self.assertTrue(closures[0]['starts_at'].endswith('+00:00'))


class HighwayText(unittest.TestCase):
    def test_parse(self):
        routes = collect.parse_highway_text((FIXTURES / 'highway_text.html').read_text())
        self.assertEqual(list(routes), ['SR 2', 'SR 18', 'SR 38', 'SR 39', 'SR 173', 'SR 330'])
        self.assertEqual(routes['SR 330'], [{'area': 'IN THE SOUTHERN CALIFORNIA AREA',
                                             'text': 'No traffic restrictions are reported for this area.'}])
        self.assertTrue(routes['SR 2'][0]['text'].startswith('Is closed from 3.3 mi east'))
        self.assertIsNone(routes['SR 2'][1]['area'])


class ResortHours(unittest.TestCase):
    def test_parse_main_only(self):
        lines = collect.parse_main_text((FIXTURES / 'resort_hours.html').read_text())
        self.assertEqual(lines[0], 'Hours of Operation')
        self.assertIn('Snow Summit:Closed for summer operations as of Sunday, October 4.', lines)
        self.assertNotIn('MTB Lift Tickets', lines)  # navigation is outside <main>
        self.assertFalse(any('rich-text' in line for line in lines))  # hidden <var> markers


if __name__ == '__main__':
    unittest.main()
